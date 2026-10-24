"""No-cluster signal and loopback contract tests."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import family2_signals as s


class SignalsTests(unittest.TestCase):
    def test_strict_oom_requires_same_pod_and_actual_reason(self):
        pod = {"metadata": {"uid": "p"}, "status": {"containerStatuses": [
            {"name": "inventory", "restartCount": 2,
             "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
        self.assertTrue(s.terminated_oom(pod, "p", 1))
        self.assertFalse(s.terminated_oom(pod, "p", 2))
        pod["status"]["containerStatuses"][0]["lastState"]["terminated"]["reason"] = "Error"
        self.assertFalse(s.terminated_oom(pod, "p", 1))
        with self.assertRaises(s.UnsafeState):
            s.terminated_oom(pod, "other", 1)

    def test_metric_exact_labels_and_rule(self):
        paths = []
        def fetch(service, path):
            paths.append((service, path))
            if path == "/api/v1/rules":
                return {"status": "success", "data": {"groups": [{"rules": [
                    {"name": s.ALERT, "type": "alerting", "health": "ok", "duration": 0,
                     "query": 'x{reason="OOMKilled"}'}]}]}}
            return {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"namespace": s.NAMESPACE, "pod": "inventory-abc", "container": "inventory",
                            "reason": "OOMKilled", "alertname": s.ALERT, "alertstate": "firing"},
                 "value": [1, "1"]}]}}
        reader = s.SignalReader(fetch)
        self.assertTrue(reader.rule_loaded("0s"))
        self.assertFalse(reader.rule_loaded("30s"))
        self.assertTrue(reader.oom_for("inventory-abc"))
        self.assertTrue(reader.firing_for("inventory-abc"))
        self.assertFalse(reader.firing_for("inventory-other"))
        self.assertTrue(all(name == "prometheus" for name, _ in paths))

    def test_chain_observer_requires_new_correlated_incident(self):
        signals = type("Signals", (), {"rule_loaded": lambda _, d: d == "0s",
                                       "oom_for": lambda _, p: True,
                                       "firing_for": lambda _, p: True})()
        pod = {"metadata": {"uid": "p", "name": "inventory-abc"}, "status": {"containerStatuses": [
            {"name": "inventory", "ready": True, "restartCount": 0,
             "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
        listing = lambda: {"items": [{"id": 58}, {"id": 60}]}
        detail = lambda i: {"incident": {"id": i, "namespace": s.NAMESPACE, "workload": "inventory",
                                         "signal_reasons": [s.ALERT if i == 60 else "other"]}}
        audit = type("Audit", (), {"ready": lambda _: True, "observe": lambda _, i: i == 60})()
        observer = s.ChainObserver(signals, lambda: pod, listing, detail,
                                   timeout=.01, interval=.001, audit=audit)
        self.assertTrue(observer.ready())
        observer.bind("p")
        pod["status"]["containerStatuses"][0]["restartCount"] = 1
        self.assertFalse(observer.observe({"pod_uid": "pre-rollout"}))  # old incidents
        observer.existing_ids = {58}
        self.assertTrue(observer.observe({"pod_uid": "pre-rollout"}))
        self.assertEqual(observer.incident_id, 60)
        no_audit = s.ChainObserver(signals, lambda: pod, listing, detail,
                                   timeout=.01, interval=.001)
        self.assertFalse(no_audit.ready())
        denied = type("Audit", (), {"ready": lambda _: True, "observe": lambda _, i: False})()
        rejected = s.ChainObserver(signals, lambda: pod, listing, detail,
                                    timeout=.01, interval=.001, audit=denied)
        self.assertTrue(rejected.ready())
        rejected.bind("p")
        rejected.existing_ids = {58}
        self.assertFalse(rejected.observe({"pod_uid": "pre-rollout"}))
        self.assertFalse(s.ChainObserver(signals, lambda: pod,
                         lambda: {"error": "401"}, detail).ready())

    def test_host_token_boundary_disabled(self):
        with self.assertRaisesRegex(s.UnsafeState, "disabled"):
            s.LoopbackFault("http://127.0.0.1:18080", lambda: "fake",
                            lambda *args: (200, {"mode": "off"}), lambda: "uid")


if __name__ == "__main__":
    unittest.main()
