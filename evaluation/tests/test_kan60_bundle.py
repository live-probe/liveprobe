"""KAN-60 offline bundle gate; all inputs are synthetic fixtures, not real results."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from evaluation.tests.test_metrics import Base, baseline_runs, write_store
from evaluation.metrics.compute import compute
from evaluation.metrics.store_import import load_store
from evaluation.metrics.truth import ApprovedTruthSnapshot
from evaluation.bundle_guard import BundleInvalid, verify_bundle


class BundleGuardTests(Base):
    def prepare(self, *, mode="real", runner="fixture", fault_worker="fixture"):
        root = self.tmp / "bundle"
        root.mkdir()
        shutil.copytree(self.cases, root / "cases")
        store = write_store(root / "store", baseline_runs(), mode=mode)
        manifest = json.loads((store / "manifest.json").read_text())
        manifest["runner"] = runner
        manifest["fault_worker"] = fault_worker
        body = {k: v for k, v in manifest.items() if k != "manifest_id"}
        manifest["manifest_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        (store / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        records = [json.loads(line) for line in (store / "progress.jsonl").read_text().splitlines()]
        for record in records:
            record["manifest_id"] = manifest["manifest_id"]
        (store / "progress.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
        report = compute(load_store(store), ApprovedTruthSnapshot(root / "cases"))
        (root / "report.json").write_text(json.dumps(report, sort_keys=True))
        paths = ["store/manifest.json", "store/progress.jsonl", "report.json"]
        paths += [p.relative_to(root).as_posix() for p in sorted((root / "cases").iterdir())]
        files = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}
        (root / "bundle-manifest.json").write_text(json.dumps({"files": files}, sort_keys=True))
        return root

    def test_fake_real_mode_identity_cannot_pass_even_with_valid_hashes(self):
        root = self.prepare()
        with self.assertRaisesRegex(BundleInvalid, "provenance"):
            verify_bundle(root)

    def test_dry_run_cannot_be_submitted(self):
        root = self.prepare(mode="dry_run", runner="actual-runner", fault_worker="actual-worker")
        with self.assertRaisesRegex(BundleInvalid, "publishable"):
            verify_bundle(root)

    def test_hash_mismatch_rejected_before_recompute(self):
        root = self.prepare()
        with (root / "store/progress.jsonl").open("ab") as f:
            f.write(b"\n")
        with self.assertRaisesRegex(BundleInvalid, "sha256"):
            verify_bundle(root)

    def test_traversal_and_symlink_refused(self):
        root = self.prepare()
        manifest_file = root / "bundle-manifest.json"
        manifest = json.loads(manifest_file.read_text())
        manifest["files"]["../outside"] = "0" * 64
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BundleInvalid, "path"):
            verify_bundle(root)
        manifest["files"].pop("../outside")
        manifest_file.write_text(json.dumps(manifest))
        (root / "report.json").unlink()
        (root / "report.json").symlink_to(root / "store/manifest.json")
        with self.assertRaisesRegex(BundleInvalid, "symlink"):
            verify_bundle(root)

    def test_synthetic_four_case_report_fails_even_if_self_labeled_real(self):
        root = self.prepare(runner="runner-v1", fault_worker="worker-v1")
        with self.assertRaisesRegex(BundleInvalid, "60×4"):
            verify_bundle(root)

    def test_modified_report_with_updated_file_hash_still_fails_recompute(self):
        root = self.prepare(runner="runner-v1", fault_worker="worker-v1")
        report_file = root / "report.json"
        report = json.loads(report_file.read_text())
        report["metrics"][0]["value"] = 0.123
        report_file.write_text(json.dumps(report, sort_keys=True))
        manifest_file = root / "bundle-manifest.json"
        manifest = json.loads(manifest_file.read_text())
        manifest["files"]["report.json"] = hashlib.sha256(report_file.read_bytes()).hexdigest()
        manifest_file.write_text(json.dumps(manifest, sort_keys=True))
        with self.assertRaisesRegex(BundleInvalid, "recomputation"):
            verify_bundle(root)

    def test_unlisted_file_refused(self):
        root = self.prepare()
        (root / "unexpected.txt").write_text("not approved")
        with self.assertRaisesRegex(BundleInvalid, "unlisted"):
            verify_bundle(root)

    def test_missing_raw_file_refused(self):
        root = self.prepare()
        manifest_file = root / "bundle-manifest.json"
        manifest = json.loads(manifest_file.read_text())
        manifest["files"].pop("store/progress.jsonl")
        manifest_file.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BundleInvalid, "required"):
            verify_bundle(root)
