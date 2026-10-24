"""Runner and fault-worker interfaces plus the fake (dry-run) implementations.

Real implementations (KAN-58) must subclass these. The fakes never touch the
network, an LLM, Kubernetes or a Secret: they are deterministic functions of
their inputs so dry-runs are reproducible byte-for-byte.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


@dataclass
class RunResult:
    ok: bool
    cost_usd: float
    output: dict
    error: str | None = None


class InferenceRunner:
    """Analyses one incident for one arm. Receives ONLY the public case view."""
    runner_id = "abstract"
    is_fake = False

    def run(self, arm, public_case: dict) -> RunResult:  # pragma: no cover - interface
        raise NotImplementedError


class FaultWorker:
    """The single worker allowed to touch the fault namespace.

    Contract: acquire() takes an exclusive lease, inject() applies one case,
    restore_baseline() returns the namespace to baseline and reports whether
    every baseline check passed. The scheduler calls restore_baseline() after
    every case, including failed ones, and stops the campaign if it is False.
    """
    worker_id = "abstract"
    is_fake = False

    def acquire(self) -> None: ...
    def release(self) -> None: ...

    def inject(self, public_case: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def restore_baseline(self, public_case: dict) -> tuple[bool, dict]:  # pragma: no cover
        raise NotImplementedError


def _h(*parts) -> int:
    return int(hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8], 16)


class FakeRunner(InferenceRunner):
    """Deterministic stand-in. Cost is a fraction of the arm's reservation; no spend."""
    runner_id = "fake-runner/1"
    is_fake = True

    def __init__(self, fail_on: set[tuple[str, str]] | None = None, cost_fraction: float = 0.5):
        self.fail_on = fail_on or set()
        self.cost_fraction = cost_fraction
        self.calls: list[tuple[str, str]] = []
        self.seen_payloads: list[dict] = []

    def run(self, arm, public_case: dict) -> RunResult:
        self.calls.append((public_case["id"], arm.arm_id))
        self.seen_payloads.append(public_case)
        if (public_case["id"], arm.arm_id) in self.fail_on:
            return RunResult(False, 0.0, {}, "fake failure")
        cost = round(arm.max_run_cost_usd * self.cost_fraction, 6)
        return RunResult(True, cost, {"fake": True, "workflow": arm.fixed_workflow or arm.strategy,
                                      "digest": _h(public_case, arm.arm_id)})


class FakeFaultWorker(FaultWorker):
    """Records calls instead of injecting anything."""
    worker_id = "fake-fault-worker/1"
    is_fake = True

    def __init__(self, baseline_fails_on: set[str] | None = None):
        self.baseline_fails_on = baseline_fails_on or set()
        self.events: list[tuple[str, str]] = []
        self.held = False

    def acquire(self):
        self.events.append(("acquire", ""))
        self.held = True

    def release(self):
        self.events.append(("release", ""))
        self.held = False

    def inject(self, public_case):
        assert self.held, "inject without lease"
        self.events.append(("inject", public_case["id"]))
        return {"fake": True}

    def restore_baseline(self, public_case):
        self.events.append(("restore", public_case["id"]))
        ok = public_case["id"] not in self.baseline_fails_on
        return ok, {"fake": True, "checks": public_case.get("baseline_checks", []), "ok": ok}
