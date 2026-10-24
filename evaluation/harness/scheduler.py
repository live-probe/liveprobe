"""Serial one-fault-worker scheduler with budget ceiling and baseline gate.

Order is deterministic (case-major, arm order fixed). For every pending
(case, arm) pair: reserve budget -> inject -> infer on public view -> ALWAYS
restore baseline -> close attempt. The campaign STOPs (no next pair) when:
  - the next reservation would exceed the cost ceiling,
  - a run reports spend above its reservation (ceiling can no longer be trusted),
  - baseline restore fails (namespace quarantined),
  - an inference payload trips the hidden-label guard.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path

from .arms import ARMS, validate_arms
from .cases import Catalog, HiddenLabelLeak
from .manifest import build_manifest, load_or_init
from .runners import FaultWorker, InferenceRunner
from .store import ProgressStore


class Stop(RuntimeError):
    pass


class NotAuthorized(RuntimeError):
    pass


class NamespaceLease:
    """Exclusive O_EXCL lock file: at most one fault worker per store/namespace."""

    def __init__(self, path: Path, owner: str):
        self.path, self.owner = Path(path), owner

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Stop(f"lease held: {self.path} ({self.path.read_text(errors='replace')[:200]}); "
                       "inspect and verify baseline before removing it") from None
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"owner": self.owner, "pid": os.getpid()}))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


@dataclass
class Outcome:
    stopped: bool
    reason: str
    tally: dict


PROTOCOL_FILE = "docs/plans/revamp/P10-evaluation-protocol.md"


def authorization_problem(authorization: dict | None) -> str | None:
    """None when `authorization` = {"protocol_sha256": <sha256 of PROTOCOL_FILE>, "approved_by": str} is valid."""
    import hashlib
    from .cases import REPO
    if not isinstance(authorization, dict) or not authorization.get("approved_by"):
        return "real execution needs an authorization with approved_by and protocol_sha256"
    path = REPO / PROTOCOL_FILE
    if not path.exists():
        return f"approved protocol {PROTOCOL_FILE} is missing"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if authorization.get("protocol_sha256") != actual:
        return "protocol changed since it was approved (sha256 mismatch); re-approve before running"
    if any(a.approval == "draft_pending_KAN-58" for a in ARMS):
        return "arms are still draft"
    return None


def run_campaign(store_dir: Path, catalog: Catalog, runner: InferenceRunner, worker: FaultWorker, *,
                 cost_ceiling_usd: float, dry_run: bool, arms=ARMS, case_ids: list[str] | None = None,
                 max_pairs: int | None = None, authorization: dict | None = None,
                 arm_ids: list[str] | None = None) -> Outcome:
    if not (isinstance(cost_ceiling_usd, (int, float)) and math.isfinite(cost_ceiling_usd)
            and cost_ceiling_usd > 0):
        raise ValueError("an explicit positive cost ceiling is mandatory")
    if dry_run:
        if not (runner.is_fake and worker.is_fake):
            raise NotAuthorized("dry_run requires the fake runner and fake fault worker")
    else:
        # Real execution needs the approved protocol: the pinned file hash must match what was approved,
        # and neither side may be a fake (a fake result must never be stored as a real one).
        if runner.is_fake or worker.is_fake:
            raise NotAuthorized("real execution with a fake runner/worker is refused")
        problem = authorization_problem(authorization)
        if problem:
            raise NotAuthorized(problem)
    problems = validate_arms(arms) + catalog.verify_labels_pinned()
    if problems:
        raise Stop("pre-flight failed: " + "; ".join(problems))
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    quarantine = store_dir / "baseline-quarantine.json"
    if quarantine.exists():
        raise Stop(f"baseline is quarantined at {quarantine}; an operator must verify restoration before any resume")
    manifest = build_manifest(catalog, mode="dry_run" if dry_run else "real", cost_ceiling_usd=cost_ceiling_usd,
                              runner_id=runner.runner_id, fault_worker_id=worker.worker_id, arms=arms,
                              extra={"authorization": authorization} if authorization else None)
    run_arms = tuple(a for a in arms if not arm_ids or a.arm_id in arm_ids)
    load_or_init(store_dir / "manifest.json", manifest)
    ids = case_ids or catalog.case_ids
    unknown = [c for c in ids if c not in catalog.public]
    if unknown:
        raise ValueError(f"unknown case ids {unknown}")

    with NamespaceLease(store_dir / "fault-namespace.lease", worker.worker_id):
        store = ProgressStore(store_dir / "progress.jsonl", manifest["manifest_id"])
        if store.torn_tail:
            with quarantine.open("x", encoding="utf-8") as fh:
                json.dump({"reason": "torn progress record; baseline and cost unknown"}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            raise Stop("torn progress record: namespace quarantined; inspect baseline and cost before recovery")
        if store.open_attempts():
            with quarantine.open("x", encoding="utf-8") as fh:
                json.dump({"reason": "unclosed attempt; baseline restoration unverified",
                           "open_attempts": [[r["case_id"], r["arm_id"], r["attempt"]]
                                             for r in store.open_attempts()]}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            raise Stop("unclosed attempt: baseline unverified; namespace quarantined")
        if store.unverified_completions():
            with quarantine.open("x", encoding="utf-8") as fh:
                json.dump({"reason": "completed attempt lacks verified baseline restoration",
                           "unverified_attempts": [[r["case_id"], r["arm_id"], r["attempt"]]
                                                   for r in store.unverified_completions()]}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            raise Stop("completed attempt without baseline proof: namespace quarantined")
        done = {k for k, s in store.status().items() if s == "completed"}
        worker.acquire()
        try:
            executed = 0
            for cid in ids:
                case = catalog.public[cid]
                for arm in run_arms:
                    if (cid, arm.arm_id) in done:
                        continue  # idempotent resume: verified completions are skipped
                    if max_pairs is not None and executed >= max_pairs:
                        return Outcome(False, "max_pairs reached", store.tally())
                    reserve = arm.max_run_cost_usd
                    spent = store.spent_usd()
                    if spent + reserve > cost_ceiling_usd + 1e-9:
                        store.stop("cost_ceiling", spent_usd=spent, next_reserve_usd=reserve,
                                   ceiling_usd=cost_ceiling_usd, next_pair=[cid, arm.arm_id])
                        return Outcome(True, "cost ceiling would be exceeded", store.tally())
                    try:
                        catalog.assert_no_hidden(case)
                    except HiddenLabelLeak as exc:
                        store.stop("hidden_label_guard", error=str(exc), next_pair=[cid, arm.arm_id])
                        return Outcome(True, f"hidden label guard: {exc}", store.tally())
                    start = store.start(cid, arm.arm_id, reserve, {"public_sha256": catalog.public_hash(cid),
                                                                    "arm_sha256": arm.spec_hash()})
                    executed += 1
                    status, cost, err, output = "failed", 0.0, None, {}
                    interrupted = None
                    try:
                        worker.inject(case)
                        res = runner.run(arm, case)
                        cost, output, err = res.cost_usd, res.output, res.error
                        status = "completed" if res.ok else "failed"
                    except Exception as exc:  # noqa: BLE001 - any runner failure stops safely below
                        err = f"{type(exc).__name__}: {exc}"
                    except BaseException as exc:
                        interrupted = exc
                        status, cost = "interrupted", reserve
                        err = f"interrupted by {type(exc).__name__}; cost unknown, charged at reservation"
                    finally:
                        try:
                            ok, baseline = worker.restore_baseline(case)
                        except BaseException as exc:
                            # Unknown restoration is not a recoverable run failure. Persist a
                            # quarantine before releasing the local lease; never start another
                            # case (or resume) without an operator's independent baseline check.
                            ok = False
                            baseline = {"ok": False, "error_type": type(exc).__name__}
                            err = f"baseline restore raised {type(exc).__name__}"
                        if not ok:
                            with quarantine.open("x", encoding="utf-8") as fh:
                                json.dump({"case_id": cid, "arm_id": arm.arm_id,
                                           "attempt": start["attempt"],
                                           "reason": "baseline restoration unverified"}, fh)
                                fh.flush()
                                os.fsync(fh.fileno())
                    invalid_cost = not (isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0)
                    if invalid_cost:
                        # Unknown/invalid usage must not poison the ledger or count as success.
                        # Reserve the entire arm amount and require independent cost/baseline review.
                        cost, status, err = reserve, "failed", "invalid runner cost; charged at reservation"
                        if not quarantine.exists():
                            with quarantine.open("x", encoding="utf-8") as fh:
                                json.dump({"case_id": cid, "arm_id": arm.arm_id,
                                           "attempt": start["attempt"],
                                           "reason": "invalid runner cost; actual cost unknown"}, fh)
                                fh.flush()
                                os.fsync(fh.fileno())
                    if not ok:
                        status = "failed"
                    store.done(start, status, cost, output=output, error=err, baseline=baseline)
                    if invalid_cost:
                        store.stop("invalid_runner_cost", case_id=cid, arm_id=arm.arm_id)
                        return Outcome(True, "invalid runner cost; namespace quarantined", store.tally())
                    if cost > reserve + 1e-9:
                        store.stop("reservation_exceeded", cost_usd=cost, reserve_usd=reserve)
                        return Outcome(True, "run spent more than its reservation", store.tally())
                    if not ok:
                        store.stop("baseline_restore_failed", case_id=cid, arm_id=arm.arm_id)
                        return Outcome(True, f"baseline restore failed after {cid}/{arm.arm_id}; namespace quarantined",
                                       store.tally())
                    if interrupted is not None:
                        raise interrupted
                    if status != "completed":
                        store.stop("run_failed", case_id=cid, arm_id=arm.arm_id, error=err)
                        return Outcome(True, f"run failed {cid}/{arm.arm_id}: {err}", store.tally())
                    done.add((cid, arm.arm_id))
            return Outcome(False, "all pairs completed", store.tally())
        finally:
            worker.release()
