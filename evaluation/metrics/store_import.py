"""Read-only import and validation of a KAN-133/KAN-58 harness store.

Store layout (KAN-133): <store>/manifest.json and <store>/progress.jsonl with
records of kind start|done|stop. This module never writes to the store.

Row id: "L<line>:<case>/<arm>#a<attempt>" of the `done` record, plus the
sha256 of that exact line, so every aggregate can be traced to the raw bytes.

Accepted-record rule (explicit, versioned with the metric set): for each
(case, arm) the EARLIEST completed `done` record in file order is accepted;
later completed duplicates are retained and listed, never double-counted.
Pairs with only failed/interrupted attempts are `failed`; pairs with no
attempt are `missing`. Neither is ever zero-filled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from evaluation.harness.arms import ARMS

from .definitions import (CAUSE_CLASSES, COST_BASES, EVIDENCE_VERDICTS, MODEL_CLASS, OUTCOME_KINDS,
                          RUN_OUTPUT_SCHEMA, VIOLATION_KINDS, WORKFLOW_LEVEL)


class StoreInvalid(RuntimeError):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class Row:
    row_id: str
    line_sha256: str
    case_id: str
    arm_id: str
    attempt: int
    status: str
    cost_usd: float | None
    output: dict
    issues: list[str] = field(default_factory=list)  # schema problems -> unscorable fields


@dataclass
class ImportedStore:
    store_dir: str
    manifest: dict
    manifest_sha256: str
    progress_sha256: str
    mode: str
    rows: list[Row]                       # every done record
    accepted: dict[tuple[str, str], Row]  # (case, arm) -> accepted row
    pair_status: dict[tuple[str, str], str]
    duplicates: list[str]                 # row ids of completed duplicates not accepted
    stops: list[dict]
    open_attempts: list[str]
    issues: list[str]
    arm_strategy: dict[str, str | None]


def _validate_output(out) -> list[str]:
    """Schema check of a run's output. Problems make individual metrics unscorable."""
    if not isinstance(out, dict) or out.get("schema") != RUN_OUTPUT_SCHEMA:
        return [f"output.schema != {RUN_OUTPUT_SCHEMA}"]
    issues = []
    sw = out.get("selected_workflow")
    if sw is not None and sw not in WORKFLOW_LEVEL:
        issues.append("selected_workflow unmappable")
    calls = out.get("model_calls")
    if calls is not None:
        if not isinstance(calls, list) or not all(isinstance(c, dict) and isinstance(c.get("model"), str)
                                                  for c in calls):
            issues.append("model_calls malformed")
        elif any(c["model"] not in MODEL_CLASS for c in calls):
            issues.append("model_calls contain unmapped model")
    if out.get("cost_basis") is not None and out["cost_basis"] not in COST_BASES:
        issues.append("cost_basis invalid")
    cands = out.get("root_cause_candidates")
    if cands is not None:
        if not isinstance(cands, list) or not all(
                isinstance(c, dict) and isinstance(c.get("rank"), int) and isinstance(c.get("target"), str)
                and c.get("cause_class") in CAUSE_CLASSES for c in cands):
            issues.append("root_cause_candidates malformed")
    claims = out.get("evidence_claims")
    if claims is not None:
        if not isinstance(claims, list) or not all(
                isinstance(c, dict) and c.get("verdict") in EVIDENCE_VERDICTS for c in claims):
            issues.append("evidence_claims malformed")
    oc = out.get("outcome")
    if oc is not None:
        if not isinstance(oc, dict) or oc.get("kind") not in OUTCOME_KINDS or not isinstance(oc.get("verified"), bool):
            issues.append("outcome malformed")
    pv = out.get("policy_violations")
    if pv is not None:
        if not isinstance(pv, list) or not all(isinstance(v, dict) and v.get("kind") in VIOLATION_KINDS for v in pv):
            issues.append("policy_violations malformed")
    return issues


def load_store(store_dir: Path) -> ImportedStore:
    store_dir = Path(store_dir)
    mpath, ppath = store_dir / "manifest.json", store_dir / "progress.jsonl"
    if not mpath.exists() or not ppath.exists():
        raise StoreInvalid(f"store {store_dir} lacks manifest.json or progress.jsonl")
    mbytes, pbytes = mpath.read_bytes(), ppath.read_bytes()
    manifest = json.loads(mbytes.decode("utf-8"))
    body = {k: v for k, v in manifest.items() if k != "manifest_id"}
    recomputed = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if manifest.get("manifest_id") != recomputed:
        raise StoreInvalid("manifest_id does not match manifest body (manifest edited after run)")
    mid = manifest["manifest_id"]
    issues: list[str] = []

    # Arm specs: the metrics need each arm's strategy; bind it via the pinned spec hash.
    arm_strategy: dict[str, str | None] = {}
    from evaluation.harness.arms import P13_ARMS
    known = {a.arm_id: a for a in (*ARMS, *P13_ARMS)}
    for arm_id, h in sorted(manifest.get("arms", {}).items()):
        spec = known.get(arm_id)
        if spec is not None and spec.spec_hash() == h:
            arm_strategy[arm_id] = spec.strategy
        else:
            arm_strategy[arm_id] = None
            issues.append(f"arm {arm_id}: spec hash not matched by current evaluation/harness/arms.py; "
                          "strategy unknown")

    raw_lines = pbytes.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    starts: dict[tuple, int] = {}
    closed: set[tuple] = set()
    rows: list[Row] = []
    stops: list[dict] = []
    for n, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except ValueError:
            if n == len(raw_lines):
                issues.append(f"L{n}: torn tail line ignored (disclosed, not repaired)")
                break
            raise StoreInvalid(f"L{n}: corrupt JSON line inside store") from None
        if rec.get("manifest_id") != mid:
            raise StoreInvalid(f"L{n}: record belongs to another manifest")
        kind = rec.get("kind")
        if kind == "start":
            key = (rec["case_id"], rec["arm_id"], rec["attempt"])
            if key in starts:
                raise StoreInvalid(f"L{n}: duplicate start for {key}")
            starts[key] = n
        elif kind == "done":
            key = (rec["case_id"], rec["arm_id"], rec["attempt"])
            if key not in starts:
                raise StoreInvalid(f"L{n}: done without start for {key}")
            if key in closed:
                raise StoreInvalid(f"L{n}: second done for {key}")
            closed.add(key)
            if rec.get("status") not in ("completed", "failed", "interrupted"):
                raise StoreInvalid(f"L{n}: unknown status {rec.get('status')!r}")
            cost = rec.get("cost_usd")
            out = rec.get("output") or {}
            row = Row(row_id=f"L{n}:{rec['case_id']}/{rec['arm_id']}#a{rec['attempt']}",
                      line_sha256=sha256_bytes(raw), case_id=rec["case_id"], arm_id=rec["arm_id"],
                      attempt=rec["attempt"], status=rec["status"],
                      cost_usd=float(cost) if isinstance(cost, (int, float)) else None, output=out)
            if row.status == "completed":
                row.issues = _validate_output(out)
            rows.append(row)
        elif kind == "stop":
            stops.append({"line": n, "reason": rec.get("reason")})
        else:
            raise StoreInvalid(f"L{n}: unknown record kind {kind!r}")
    open_attempts = [f"L{starts[k]}:{k[0]}/{k[1]}#a{k[2]}" for k in sorted(starts, key=lambda k: starts[k]) if k not in closed]
    if open_attempts:
        issues.append(f"{len(open_attempts)} attempt(s) started but never closed (counted as failed)")

    accepted: dict[tuple[str, str], Row] = {}
    duplicates: list[str] = []
    pair_status: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row.case_id, row.arm_id)
        if row.status == "completed":
            if key in accepted:
                duplicates.append(row.row_id)
            else:
                accepted[key] = row
                pair_status[key] = "accepted"
        else:
            pair_status.setdefault(key, "failed")
    for k in starts:
        pair_status.setdefault((k[0], k[1]), "failed")
    for row in rows:
        if row.arm_id not in arm_strategy:
            issues.append(f"{row.row_id}: arm not in manifest")

    return ImportedStore(store_dir=str(store_dir), manifest=manifest, manifest_sha256=sha256_bytes(mbytes),
                         progress_sha256=sha256_bytes(pbytes), mode=manifest.get("mode", "unknown"), rows=rows,
                         accepted=accepted, pair_status=pair_status, duplicates=duplicates, stops=stops,
                         open_attempts=open_attempts, issues=issues, arm_strategy=arm_strategy)
