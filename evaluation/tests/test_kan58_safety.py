"""Offline KAN-58 resume gates; synthetic fixtures only, no external calls."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evaluation.harness.cases import Catalog
from evaluation.harness.runners import FakeFaultWorker, FakeRunner
from evaluation.harness.scheduler import Stop, run_campaign
from evaluation.harness.store import ProgressStore
from evaluation.tests.test_harness import FULL, synthetic_catalog


class ResumeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = Catalog(synthetic_catalog(self.root))
        self.store_dir = self.root / "progress"

    def campaign(self, *, runner=None, worker=None, case_ids=None):
        runner = runner or FakeRunner()
        worker = worker or FakeFaultWorker()
        result = run_campaign(self.store_dir, self.catalog, runner, worker,
                              cost_ceiling_usd=FULL, dry_run=True, case_ids=case_ids)
        return result, runner, worker

    def progress(self):
        manifest = json.loads((self.store_dir / "manifest.json").read_text())
        return ProgressStore(self.store_dir / "progress.jsonl", manifest["manifest_id"])

    def test_duplicate_case_selection_never_reinjects_same_pair(self):
        cid = self.catalog.case_ids[0]
        outcome, runner, worker = self.campaign(case_ids=[cid, cid])
        self.assertEqual(outcome.tally["attempts"], 4)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(sum(event[0] == "inject" for event in worker.events), 4)

    def test_unverified_completion_cannot_be_skipped_on_resume(self):
        cid = self.catalog.case_ids[0]
        self.campaign(case_ids=[cid])
        progress = self.progress()
        # Simulate a persisted success with no restoration proof for a second case.
        second = self.catalog.case_ids[1]
        start = progress.start(second, "single_small", 0.05, {})
        progress.done(start, "completed", 0.025, output={"fake": True})
        runner = FakeRunner()
        with self.assertRaises(Stop):
            self.campaign(runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertTrue((self.store_dir / "baseline-quarantine.json").exists())
        self.assertNotEqual(self.progress().status().get((second, "single_small")), "completed")

    def test_failed_baseline_proof_cannot_count_as_accepted(self):
        cid = self.catalog.case_ids[0]
        self.campaign(case_ids=[cid])
        progress = self.progress()
        second = self.catalog.case_ids[1]
        start = progress.start(second, "single_small", 0.05, {})
        progress.done(start, "completed", 0.025, baseline={"ok": False})
        self.assertNotEqual(progress.status().get((second, "single_small")), "completed")
        self.assertEqual(progress.tally()["accepted_pairs"], 4)


if __name__ == "__main__":
    unittest.main()
