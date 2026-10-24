"""Run manifest: pins versions and input hashes; the manifest id binds a store."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess

from . import HARNESS_VERSION
from .arms import ARMS
from .cases import REPO, Catalog

# Files whose content defines behaviour of a run. Hashes, not contents, are stored.
PINNED_FILES = (
    "evaluation/harness/__init__.py", "evaluation/harness/arms.py", "evaluation/harness/cases.py",
    "evaluation/harness/manifest.py", "evaluation/harness/store.py", "evaluation/harness/scheduler.py",
    "evaluation/harness/runners.py", "chaos/validate.py", "liveprobe/app/config.py",
    "liveprobe/app/router.py", "liveprobe/app/workflows.py", "liveprobe/app/engine.py",
)


def _file_hash(rel: str) -> str | None:
    p = REPO / rel
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _git_head() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def build_manifest(catalog: Catalog, *, mode: str, cost_ceiling_usd: float, runner_id: str,
                   fault_worker_id: str, arms=ARMS, extra: dict | None = None) -> dict:
    if mode not in ("dry_run", "real"):
        raise ValueError("mode must be dry_run or real")
    body = {
        "harness_version": HARNESS_VERSION,
        "mode": mode,
        "cost_ceiling_usd": cost_ceiling_usd,
        "runner": runner_id,
        "fault_worker": fault_worker_id,
        "python": platform.python_version(),
        "git_head": _git_head(),  # None when the repo has no commit yet; file hashes still pin code.
        "files": {rel: _file_hash(rel) for rel in PINNED_FILES},
        "arms": {a.arm_id: a.spec_hash() for a in arms},
        "catalog": {
            "label_version": catalog.label_version,
            "labels_manifest_sha256": catalog.labels_manifest_hash,
            "cases": {cid: {"public_sha256": catalog.public_hash(cid), "file_sha256": catalog.file_hashes[cid]}
                      for cid in catalog.case_ids},
        },
        "extra": extra or {},
    }
    body["manifest_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def load_or_init(path: Path, manifest: dict) -> dict:
    """First run writes the manifest; a resume must present an identical one."""
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text("utf-8"))
        if existing.get("manifest_id") != manifest["manifest_id"]:
            diff = sorted(k for k in manifest if existing.get(k) != manifest[k])
            raise ManifestMismatch(f"store belongs to manifest {existing.get('manifest_id', '?')[:12]}, "
                                   f"current {manifest['manifest_id'][:12]}; differing: {diff}")
        return existing
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return manifest


class ManifestMismatch(RuntimeError):
    pass
