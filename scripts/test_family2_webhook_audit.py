"""KAN-128 fixed SQL read-only audit proof fake tests; no cluster mutation."""
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import family2_webhook_audit as a

BASE = "40|90|1000\n"
GOOD = "41|1001|91|1002|alertmanager|TargetWorkloadOOMKilled|incident_created\n"
MERGED = "41|1001|92|1003|alertmanager|TargetWorkloadOOMKilled|signal_merged\n"


class Fake:
    def __init__(self, baseline=BASE, proof=GOOD, count="27\n"):
        self.responses = {a.BASELINE_SQL: baseline, a.PROOF_SQL: proof, a.COUNT_SQL: count}
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=self.responses[args[-1]],
                               stderr="secret database diagnostic")


class AuditTests(unittest.TestCase):
    def test_fixed_command_projection_and_created_or_merged(self):
        fake = Fake()
        observer = a.WebhookAuditObserver(fake)
        self.assertTrue(observer.ready())
        self.assertTrue(observer.observe(41))
        self.assertEqual(observer.count(), 27)
        for args, kw in fake.calls:
            self.assertEqual(tuple(args[:-1]), a.COMMAND)
            self.assertTrue(kw["capture_output"])
            self.assertLessEqual(kw["timeout"], 12)
            self.assertIn(args[-1], (a.BASELINE_SQL, a.PROOF_SQL, a.COUNT_SQL))
            self.assertNotIn("detail,", args[-1])
            self.assertNotIn("summary", args[-1])
        self.assertTrue(a.WebhookAuditObserver(Fake(proof=MERGED)).ready())
        merged = a.WebhookAuditObserver(Fake(proof=MERGED))
        merged.ready()
        self.assertTrue(merged.observe(41))

    def test_reject_stale_event_only_empty_duplicates_and_partial(self):
        variants = ["", "40|1001|91|1002|alertmanager|TargetWorkloadOOMKilled|incident_created\n",
                    "41|999|91|1002|alertmanager|TargetWorkloadOOMKilled|incident_created\n",
                    "41|1001|90|1002|alertmanager|TargetWorkloadOOMKilled|incident_created\n",
                    "41|1001|91|999|alertmanager|TargetWorkloadOOMKilled|incident_created\n",
                    GOOD + GOOD, GOOD + MERGED,
                    "41|1001|91|1002|event_watch|TargetWorkloadOOMKilled|incident_created\n",
                    "41|1001|91|1002|alertmanager|TargetWorkloadOOMKilled|other\n",
                    "41|1001|91|1002|alertmanager|TargetWorkloadOOMKilled\n",
                    GOOD.rstrip("\n"),
                    GOOD + "42|1001|93|1002|alertmanager|TargetWorkloadOOMKilled|incident_created\n"]
        for result in variants:
            with self.subTest(result=repr(result)[:30]):
                obs = a.WebhookAuditObserver(Fake(proof=result))
                obs.ready()
                with self.assertRaises(a.UnsafeState):
                    obs.observe(41)
        with self.assertRaises(a.UnsafeState):
            a.WebhookAuditObserver(Fake()).observe(41)
        obs = a.WebhookAuditObserver(Fake())
        obs.ready()
        with self.assertRaises(a.UnsafeState):
            obs.observe(40)

    def test_bad_baseline_count_and_sensitive_error_suppressed(self):
        for baseline in ("", "40|90\n", "40|90|1000\n40|90|1000\n", "bad-secret|90|1000\n"):
            with self.subTest(baseline=baseline):
                with self.assertRaises(a.UnsafeState) as caught:
                    a.WebhookAuditObserver(Fake(baseline=baseline)).ready()
                self.assertNotIn("secret", str(caught.exception))
        for count in ("", "-1\n", "2\n3\n", "sensitive-secret\n"):
            with self.assertRaises(a.UnsafeState):
                a.WebhookAuditObserver(Fake(count=count)).count()
        def fail(*args, **kwargs):
            raise RuntimeError("token-sensitive-error")
        with self.assertRaises(a.UnsafeState) as caught:
            a.WebhookAuditObserver(fail).ready()
        self.assertNotIn("sensitive", str(caught.exception))
        def nonzero(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "token-sensitive-stderr")
        with self.assertRaises(a.UnsafeState) as caught:
            a.WebhookAuditObserver(nonzero).ready()
        self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
