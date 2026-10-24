"""Approved truth snapshot: the ONLY way the metrics engine touches labels.

It loads the operator catalog once, refuses unless LABELS.json is `approved`
and every case's label digest matches it, and then keeps only derived scoring
keys (reference level, (target, cause_class), handoff requirement, evidence
sufficiency flag). Label texts (cause, expected_response, decoy) are never
stored, returned, printed or written to a report; scorers receive booleans.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evaluation.harness.cases import CASES_DIR, Catalog

from .definitions import CAUSE_CLASS_BY_MODE, WORKFLOW_LEVEL


class TruthNotApproved(RuntimeError):
    pass


class ApprovedTruthSnapshot:
    __slots__ = ("cases_dir", "label_version", "labels_manifest_sha256", "_keys")

    def __init__(self, cases_dir: Path = CASES_DIR):
        self.cases_dir = Path(cases_dir)
        labels_path = self.cases_dir / "LABELS.json"
        if not labels_path.exists():
            raise TruthNotApproved("LABELS.json missing")
        labels = json.loads(labels_path.read_text("utf-8"))
        if labels.get("status") != "approved":
            raise TruthNotApproved(f"LABELS.json status is {labels.get('status')!r}, not 'approved'")
        problems = Catalog(self.cases_dir).verify_labels_pinned()
        if problems:
            raise TruthNotApproved("label digests not pinned: " + "; ".join(problems[:5]))
        self.label_version = labels.get("label_version")
        self.labels_manifest_sha256 = hashlib.sha256(labels_path.read_bytes()).hexdigest()
        keys = {}
        from evaluation.harness.cases import case_pattern
        for path in sorted(self.cases_dir.glob(case_pattern(self.cases_dir))):
            case = json.loads(path.read_text("utf-8"))
            ref = case.get("reference_workflow")
            truth = case.get("ground_truth") or {}
            mode = (case.get("injection") or {}).get("mode")
            if ref not in WORKFLOW_LEVEL:
                raise TruthNotApproved(f"{case['id']}: reference workflow not mappable")
            if mode not in CAUSE_CLASS_BY_MODE:
                raise TruthNotApproved(f"{case['id']}: fault mode not mapped to a cause class")
            keys[case["id"]] = (WORKFLOW_LEVEL[ref], case["target"], CAUSE_CLASS_BY_MODE[mode],
                                truth.get("evidence_sufficiency") == "insufficient")
        self._keys = keys

    def __repr__(self) -> str:  # never show label content
        return f"ApprovedTruthSnapshot(label_version={self.label_version}, cases={len(self._keys)})"

    @property
    def case_ids(self) -> list[str]:
        return sorted(self._keys)

    def has(self, cid: str) -> bool:
        return cid in self._keys

    # --- scoring accessors: return comparisons, not labels ---------------------
    def compare_level(self, cid: str, selected_level: int) -> int:
        """-1 under-configured, 0 match, +1 over-configured."""
        ref = self._keys[cid][0]
        return (selected_level > ref) - (selected_level < ref)

    def cause_hit(self, cid: str, target: str, cause_class: str) -> bool:
        _, t, c, _ = self._keys[cid]
        return target == t and cause_class == c

    def requires_handoff(self, cid: str) -> bool:
        return self._keys[cid][0] == WORKFLOW_LEVEL["high_risk_handoff"]

    def evidence_insufficient(self, cid: str) -> bool:
        return self._keys[cid][3]
