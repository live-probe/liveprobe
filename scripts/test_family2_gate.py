"""Isolated, no-cluster safety fixtures for KAN-122."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("family2_gate", Path(__file__).with_name("family2_gate.py"))
if spec is None or spec.loader is None:
    raise RuntimeError("family2 gate import failed")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def setUp(self):
        image = "liveprobe/inventory@sha256:" + "a" * 64
        self.container = {"name": "inventory", "image": image,
                          "resources": {"limits": {"memory": "512Mi"}},
                          "readinessProbe": {"httpGet": {"path": "/actuator/health/readiness", "port": 8080}}}
        self.dep = {"kind": "Deployment", "metadata": {"name": "inventory", "namespace": gate.TARGET},
                    "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "inventory"}},
                                                   "spec": {"containers": [self.container]}}}}
        self.chaos = copy.deepcopy(self.dep)
        c = self.chaos["spec"]["template"]["spec"]["containers"][0]
        c["env"] = [{"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"},
                    {"name": "CHAOS_OPERATOR_TOKEN", "valueFrom": {"secretKeyRef": {
                        "name": "inventory-chaos-operator", "key": "token"}}},
                    {"name": "CHAOS_STATE_DIR", "value": "/var/run/liveprobe-chaos"}]
        c["volumeMounts"] = [{"name": "chaos-state", "mountPath": "/var/run/liveprobe-chaos"}]
        self.chaos["spec"]["template"]["spec"]["volumes"] = [
            {"name": "chaos-state", "emptyDir": {"sizeLimit": "1Mi"}}]
        self.cm = {"kind": "ConfigMap", "metadata": {"name": "prometheus-config",
                  "namespace": gate.OBS}, "data": {"alerts.yml": "groups:\n- rules:\n  - alert: TargetWorkloadOOMKilled\n    expr: 'reason=\"OOMKilled\"'\n    for: 30s\n", "prometheus.yml": "scrape_configs: []"}}
        self.local = copy.deepcopy(self.cm)
        self.local["data"]["alerts.yml"] = self.cm["data"]["alerts.yml"].replace("30s", "0s")

    def test_scoped_candidate_and_profile(self):
        dep, cm = gate.scoped_candidates((self.dep, self.cm), self.chaos, self.local)
        self.assertEqual(gate.profile(dep), "chaos")
        self.assertEqual(gate.profile(self.dep), None)
        self.assertEqual(cm["data"], self.local["data"])
        self.assertEqual(self.cm["data"]["alerts.yml"].splitlines()[-1], "    for: 30s")

    def test_reject_extra_inventory_change(self):
        self.chaos["spec"]["template"]["spec"]["containers"][0]["image"] = "other@sha256:" + "b" * 64
        with self.assertRaises(gate.GateError):
            gate.scoped_candidates((self.dep, self.cm), self.chaos, self.local)

    def test_reject_extra_prometheus_change(self):
        self.local["data"]["prometheus.yml"] = "scrape_configs: [bad]"
        with self.assertRaises(gate.GateError):
            gate.scoped_candidates((self.dep, self.cm), self.chaos, self.local)

    def test_reject_wrong_oom_reason_and_duration(self):
        self.local["data"]["alerts.yml"] = self.local["data"]["alerts.yml"].replace("OOMKilled\"", "Error\"")
        with self.assertRaises(gate.GateError):
            gate.scoped_candidates((self.dep, self.cm), self.chaos, self.local)
        self.local["data"]["alerts.yml"] = self.cm["data"]["alerts.yml"]
        with self.assertRaises(gate.GateError):
            gate.scoped_candidates((self.dep, self.cm), self.chaos, self.local)

    def test_node_budget_and_pressure(self):
        node = {"items": [{"status": {"conditions": [
            {"type": t, "status": "True" if t == "Ready" else "False"}
            for t in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure")],
            "allocatable": {"memory": "10485760Ki"}}}]}
        with patch.object(gate, "run", side_effect=[json.dumps(node), "a 1399m 70% 6900Mi 67%"]):
            self.assertEqual(gate.node_gate()["cpu_m"], 1399)
        with patch.object(gate, "run", side_effect=[json.dumps(node), "a 1400m 70% 6900Mi 67%"]):
            with self.assertRaises(gate.GateError):
                gate.node_gate()
        node["items"][0]["status"]["conditions"][1]["status"] = "True"
        with patch.object(gate, "run", side_effect=[json.dumps(node), "a 300m 15% 3000Mi 29%"]):
            with self.assertRaises(gate.GateError):
                gate.node_gate()

    def test_private_backup_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            directory.chmod(0o700)
            saved = gate.snapshot(directory, (self.dep, self.cm), (self.chaos, self.local))
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(gate.GateError):
                gate.snapshot(directory, (self.dep, self.cm), (self.chaos, self.local))

    def test_loopback_only(self):
        with self.assertRaises(gate.GateError):
            gate.http_code("http://inventory.liveprobe-target.svc:8080/api/stock/a")


if __name__ == "__main__":
    unittest.main()
