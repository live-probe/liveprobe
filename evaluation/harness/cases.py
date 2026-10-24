"""Case loading with hidden-label isolation.

The operator-only catalog (chaos/cases) holds answer fields. Inference may only
ever receive `public_view(case)`; `assert_no_hidden` is the guard that every
payload crosses before a runner sees it. Answer fields themselves are only kept
as a sha256 digest (the same digest chaos/validate.py records in LABELS.json),
so the harness can pin label versions without holding the labels in memory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CASES_DIR = REPO / "chaos" / "cases"
HIDDEN_KEYS = frozenset({"ground_truth", "reference_workflow", "cause", "expected_response",
                         "decoy_signal", "evidence_sufficiency"})
# Everything an inference arm may see. Allow-list, not deny-list.
PUBLIC_KEYS = ("version", "id", "family", "target", "expected_signal", "baseline_checks")
# Truth strings shorter than this are too generic to scan for (e.g. "simple").
MIN_SCAN_LEN = 12


def case_pattern(cases_dir: Path) -> str:
    """The approved 60-case catalog uses F* ids; the P13 held-out catalog (chaos/cases-new) uses N* ids."""
    return "N*.yaml" if Path(cases_dir).name == "cases-new" else "F*.yaml"


class HiddenLabelLeak(RuntimeError):
    pass


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def label_digest(case: dict) -> str:
    return _digest({"reference_workflow": case.get("reference_workflow"), "ground_truth": case.get("ground_truth")})


def public_view(case: dict) -> dict:
    return {k: case[k] for k in PUBLIC_KEYS if k in case}


class Catalog:
    """Loaded catalog. Keeps public views, digests and a scan set of truth strings."""

    def __init__(self, cases_dir: Path = CASES_DIR, pattern: str | None = None):
        self.cases_dir = Path(cases_dir)
        pattern = pattern or case_pattern(self.cases_dir)
        files = sorted(self.cases_dir.glob(pattern))
        self.public: dict[str, dict] = {}
        self.label_digests: dict[str, str] = {}
        self.file_hashes: dict[str, str] = {}
        self._secrets: set[str] = set()
        for path in files:
            raw = path.read_bytes()
            case = json.loads(raw.decode("utf-8"))
            cid = case["id"]
            if cid in self.public:
                raise ValueError(f"duplicate case id {cid}")
            self.public[cid] = public_view(case)
            self.label_digests[cid] = label_digest(case)
            self.file_hashes[cid] = hashlib.sha256(raw).hexdigest()
            truth = case.get("ground_truth") or {}
            for key in ("cause", "expected_response", "decoy_signal"):
                value = truth.get(key)
                if isinstance(value, str) and len(value) >= MIN_SCAN_LEN:
                    self._secrets.add(value)
        labels_path = self.cases_dir / "LABELS.json"
        self.labels_manifest_hash = hashlib.sha256(labels_path.read_bytes()).hexdigest() if labels_path.exists() else None
        self.label_version = json.loads(labels_path.read_text("utf-8")).get("label_version") if labels_path.exists() else None

    @property
    def case_ids(self) -> list[str]:
        return list(self.public)

    def public_hash(self, cid: str) -> str:
        return _digest(self.public[cid])

    def verify_labels_pinned(self) -> list[str]:
        """Label digests must match the approved LABELS.json (no silent relabel)."""
        path = self.cases_dir / "LABELS.json"
        if not path.exists():
            return ["LABELS.json missing"]
        recorded = json.loads(path.read_text("utf-8")).get("cases", {})
        return [f"{cid}: label digest differs from LABELS.json" for cid, d in self.label_digests.items()
                if recorded.get(cid) != d] + [f"{cid}: in LABELS.json but no case" for cid in recorded
                                               if cid not in self.label_digests]

    def assert_no_hidden(self, payload) -> None:
        """Raise if a payload bound for inference contains an answer key or value."""
        def walk(obj, path="$"):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in HIDDEN_KEYS:
                        raise HiddenLabelLeak(f"hidden key {k!r} at {path}")
                    walk(v, f"{path}.{k}")
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")
        walk(payload)
        text = json.dumps(payload, ensure_ascii=False)
        for secret in self._secrets:
            if secret in text:
                # Never echo the value itself.
                raise HiddenLabelLeak(f"hidden label value (sha256 {hashlib.sha256(secret.encode()).hexdigest()[:12]}) in payload")
