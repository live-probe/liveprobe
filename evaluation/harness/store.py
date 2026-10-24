"""Append-only, fsynced JSONL progress store with idempotent resume.

Record kinds: start, done (status completed|failed|interrupted), stop.
Run key = (case_id, arm_id); a pair is accepted once it has one completed
attempt. Attempts are never rewritten; duplicates stay but count once.
"""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import time


class ProgressStore:
    def __init__(self, path: Path, manifest_id: str):
        self.path = Path(path)
        self.manifest_id = manifest_id
        self.records: list[dict] = []
        self.torn_tail = False
        if self.path.exists():
            lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for n, line in enumerate(lines, 1):
                try:
                    rec = json.loads(line)
                except ValueError:
                    # A torn final line from a crash is dropped; anything earlier is corruption.
                    if n == len(lines):
                        self.torn_tail = True
                        break
                    raise
                if rec.get("manifest_id") != manifest_id:
                    raise ValueError(f"record {n} belongs to another manifest")
                self.records.append(rec)
            # A torn record might have contained a start, done or restore result.
            # Never discard it and resume fault injection on an unknown baseline.
            # The scheduler quarantines this store for independent inspection.

    def _append(self, rec: dict) -> dict:
        if self.torn_tail:
            raise ValueError("progress has a torn final record; verify baseline before recovery")
        rec = {"manifest_id": self.manifest_id, "ts": time.time(), **rec}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)
        return rec

    def attempts(self, case_id: str, arm_id: str) -> int:
        return sum(1 for r in self.records if r["kind"] == "start" and r["case_id"] == case_id and r["arm_id"] == arm_id)

    def start(self, case_id: str, arm_id: str, reserve_usd: float, inputs: dict) -> dict:
        return self._append({"kind": "start", "case_id": case_id, "arm_id": arm_id,
                             "attempt": self.attempts(case_id, arm_id) + 1, "reserve_usd": reserve_usd,
                             "inputs": inputs})

    def done(self, start: dict, status: str, cost_usd: float, **fields) -> dict:
        assert status in ("completed", "failed", "interrupted")
        return self._append({"kind": "done", "case_id": start["case_id"], "arm_id": start["arm_id"],
                             "attempt": start["attempt"], "status": status, "cost_usd": cost_usd, **fields})

    def stop(self, reason: str, **fields) -> dict:
        return self._append({"kind": "stop", "reason": reason, **fields})

    def open_attempts(self) -> list[dict]:
        """Starts with no matching done: the process died mid-run."""
        closed = {(r["case_id"], r["arm_id"], r["attempt"]) for r in self.records if r["kind"] == "done"}
        return [r for r in self.records if r["kind"] == "start"
                and (r["case_id"], r["arm_id"], r["attempt"]) not in closed]

    def close_interrupted(self) -> int:
        """Mark dangling attempts interrupted, charging their full reservation (unknown spend)."""
        dangling = self.open_attempts()
        for s in dangling:
            self.done(s, "interrupted", s["reserve_usd"], error="process ended before attempt closed; "
                      "cost unknown, charged at reservation")
        return len(dangling)

    def unverified_completions(self) -> list[dict]:
        """Success records lacking positive post-fault baseline evidence."""
        return [r for r in self.records if r["kind"] == "done" and r["status"] == "completed"
                and not (isinstance(r.get("baseline"), dict) and r["baseline"].get("ok") is True)]

    def status(self) -> dict[tuple[str, str], str]:
        """Best status per pair: completed > failed/interrupted."""
        best: dict[tuple[str, str], str] = {}
        for r in self.records:
            if r["kind"] != "done":
                continue
            key = (r["case_id"], r["arm_id"])
            # An inference result alone is not a verified completion: the
            # baseline restoration proof must be present and explicitly true.
            baseline = r.get("baseline")
            status = r["status"]
            if status == "completed" and not (isinstance(baseline, dict) and baseline.get("ok") is True):
                status = "failed"
            if best.get(key) != "completed":
                best[key] = status
        return best

    def spent_usd(self) -> float:
        return round(sum(r["cost_usd"] for r in self.records if r["kind"] == "done"), 6)

    def tally(self) -> dict:
        st = self.status()
        per_arm: dict[str, int] = defaultdict(int)
        for (_, arm), s in st.items():
            if s == "completed":
                per_arm[arm] += 1
        return {"accepted_pairs": sum(per_arm.values()), "per_arm": dict(per_arm),
                "attempts": sum(1 for r in self.records if r["kind"] == "start"),
                "failed_or_interrupted_pairs": sum(1 for s in st.values() if s != "completed"),
                "spent_usd": self.spent_usd()}
