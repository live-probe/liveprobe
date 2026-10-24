"""Fault runner safety tests; no Kubernetes calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / "run-case.py"
sys.path.insert(0, str(RUNNER.parent))
spec = importlib.util.spec_from_file_location("case_runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_only_operator_owned_cases(self):
        self.assertEqual(runner.case_file("F1-01")["injection"]["mode"], "bad_image")
        self.assertEqual(runner.case_file("F4-15")["injection"]["mode"], "selector_mismatch")
        for cid in ("F1-02", "F2-01", "F3-01", "F4-02", "F1-99", "../../x"):
            with self.subTest(cid=cid), self.assertRaises(runner.Stop):
                runner.case_file(cid)

    def test_signal_requires_new_pod_and_real_waiting_reason(self):
        with patch.object(runner, "pod_status", side_effect=[
                [("old", "uid-old", {"state": {"waiting": {"reason": "ImagePullBackOff"}}})],
                [("new", "uid-new", {"state": {"waiting": {"reason": "Running"}}})],
                [("new", "uid-new", {"state": {"waiting": {"reason": "ErrImagePull"}}})]]), \
                patch.object(runner.time, "sleep"):
            self.assertEqual(runner.signal("inventory", "bad_image", {"uid-old"}),
                             {"new_pod": "new", "reason": "ErrImagePull"})

    def test_selector_requires_zero_ready_endpoints(self):
        with patch.object(runner, "endpoints", side_effect=[1, 0]), patch.object(runner.time, "sleep"):
            self.assertEqual(runner.signal("order", "selector_mismatch", set()), {"ready_endpoints": 0})

    def test_preflight_failure_never_injects_or_creates_case_journal(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", side_effect=runner.Stop("node not ready")), \
                patch.object(runner, "kubectl") as kubectl, \
                patch.object(runner, "restore") as restore:
            with self.assertRaisesRegex(runner.Stop, "node not ready"):
                runner.run("F1-01", Path(folder))
            self.assertFalse((Path(folder) / "active.json").exists())
            self.assertFalse((Path(folder) / "results.jsonl").exists())
            kubectl.assert_not_called()
            restore.assert_not_called()

    def test_failure_restores_and_stops_campaign(self):
        base = {t: {"image": "liveprobe/" + t + "@sha256:" + "a" * 64, "selector": {"app": t}}
                for t in runner.TARGETS}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", return_value={}), \
                patch.object(runner, "snapshot", return_value=base), \
                patch.object(runner, "healthy") as health, \
                patch.object(runner, "pg_sessions", return_value=1), \
                patch.object(runner, "pod_status", return_value=[]), \
                patch.object(runner, "kubectl") as kubectl, \
                patch.object(runner, "signal", side_effect=runner.Stop("no signal")), \
                patch.object(runner, "restore") as restore:
            with self.assertRaises(runner.Stop):
                runner.run("F1-01", Path(folder))
            restore.assert_called_once_with(base, "inventory", "bad_image")
            self.assertEqual(health.call_count, 2)
            self.assertFalse((Path(folder) / "active.json").exists())
            result = json.loads((Path(folder) / "results.jsonl").read_text())
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(result["baseline_restored"])
            self.assertFalse("ground_truth" in str(result))
            kubectl.assert_called()

    def test_failed_restore_retains_journal(self):
        base = {t: {"image": "digest", "selector": {"app": t}} for t in runner.TARGETS}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runner, "node_gate", return_value={}), patch.object(runner, "snapshot", return_value=base), \
                patch.object(runner, "healthy"), patch.object(runner, "pg_sessions", return_value=1), \
                patch.object(runner, "pod_status", return_value=[]), patch.object(runner, "kubectl"), \
                patch.object(runner, "signal", return_value={"ready_endpoints": 0}), \
                patch.object(runner, "restore", side_effect=runner.Stop("failed")):
            with self.assertRaises(runner.Stop):
                runner.run("F4-01", Path(folder))
            self.assertTrue((Path(folder) / "active.json").exists())
            self.assertFalse(json.loads((Path(folder) / "results.jsonl").read_text())["baseline_restored"])


if __name__ == "__main__":
    unittest.main()
