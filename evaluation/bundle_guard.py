"""Read-only KAN-60 offline integrity gate; never creates or publishes a bundle.

A passing check is not a privacy review or proof of genuine runner provenance.
The latter requires independent attestations beyond a self-declared manifest.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from evaluation.harness.arms import ARM_IDS
from evaluation.metrics.compute import compute, verify_report
from evaluation.metrics.store_import import load_store
from evaluation.metrics.truth import ApprovedTruthSnapshot


class BundleInvalid(ValueError):
    pass


def verify_bundle(root: Path) -> dict:
    """Check a closed file manifest, raw hashes, recomputation and acceptance gate.

    Does not certify truth of a store's mode/runner identity: those are
    self-reported and must be attested independently before publication.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise BundleInvalid("bundle root must be a real directory")
    manifest_path = root / "bundle-manifest.json"
    if manifest_path.is_symlink():
        raise BundleInvalid("manifest symlink refused")
    try:
        files = json.loads(manifest_path.read_text("utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BundleInvalid("invalid bundle manifest") from exc
    if not isinstance(files, dict):
        raise BundleInvalid("files must be a path-to-sha256 mapping")
    required = {"report.json", "store/manifest.json", "store/progress.jsonl", "cases/LABELS.json"}
    if not required.issubset(files):
        raise BundleInvalid("required raw/report files absent from manifest")
    if not any(name.startswith("cases/") and name.endswith(".yaml") for name in files):
        raise BundleInvalid("required case files absent")
    for name, expected in files.items():
        if not isinstance(name, str):
            raise BundleInvalid("unsafe path in manifest")
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or path.as_posix() != name
                or not name or "\\" in name):
            raise BundleInvalid("unsafe path in manifest")
        if (not isinstance(expected, str) or len(expected) != 64
                or any(ch not in "0123456789abcdef" for ch in expected)):
            raise BundleInvalid("invalid sha256 in manifest")
    actual = set()
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise BundleInvalid("symlink refused")
        if entry.is_file():
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(files) | {"bundle-manifest.json"}:
        raise BundleInvalid("unlisted or missing file in bundle")
    for name, expected in files.items():
        entry = root / name
        if not entry.is_file():
            raise BundleInvalid("missing file in manifest")
        if hashlib.sha256(entry.read_bytes()).hexdigest() != expected:
            raise BundleInvalid(f"sha256 mismatch: {name}")
    store = load_store(root / "store")
    if store.mode != "real":
        raise BundleInvalid("store not publishable: not a real run")
    runner = store.manifest.get("runner", "")
    worker = store.manifest.get("fault_worker", "")
    if (not isinstance(runner, str) or not isinstance(worker, str)
            or any(x in value.lower() for value in (runner, worker) for x in ("fake", "fixture", "dry"))
            or not runner or not worker):
        raise BundleInvalid("unacceptable self-reported provenance")
    truth = ApprovedTruthSnapshot(root / "cases")
    recomputed = compute(store, truth)
    report = json.loads((root / "report.json").read_text("utf-8"))
    problems = verify_report(report, recomputed)
    if problems:
        raise BundleInvalid("report recomputation mismatch: " + "; ".join(problems))
    counts = recomputed["completeness"]
    if (not recomputed["publishable"] or len(truth.case_ids) != 60
            or set(counts) != set(ARM_IDS)
            or any(row["accepted"] != 60 for row in counts.values())):
        raise BundleInvalid("report not publishable or not 60×4 actual accepted pairs")
    return {"report_digest": recomputed["report_digest"], "accepted_pairs": 240}
