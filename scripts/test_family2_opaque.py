"""Fake-only KAN-127 tests. No Kubernetes, token, or HTTP calls."""
import unittest
from types import SimpleNamespace

from family2_opaque import OpaquePodFaultGet
from family2_transaction import UnsafeState, TARGET


class OpaqueGetTests(unittest.TestCase):
    def setUp(self):
        self.deployment = {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                "name": "inventory", "image": "inventory@sha256:" + "a" * 64,
                "resources": {"limits": {"memory": "512Mi"}}, "env": []}]}}},
            "status": {"observedGeneration": 1, "readyReplicas": 1,
                       "updatedReplicas": 1, "availableReplicas": 1},
        }
        self.pod = {"metadata": {"namespace": TARGET, "name": "inventory-abc", "uid": "uid-1"},
                    "status": {"phase": "Running", "containerStatuses": [
                        {"name": "inventory", "ready": True}]}}
        self.calls = []
        self.output = '{"mode":"off","operation_id":null}\n200'

    def probe(self):
        def runner(args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=self.output,
                                   stderr="private-error-must-not-surface")
        return OpaquePodFaultGet(lambda: self.deployment, lambda: self.pod, runner)

    def test_default_profile_stops_before_exec(self):
        with self.assertRaisesRegex(UnsafeState, "STOP"):
            self.probe().get_off("inventory-abc", "uid-1")
        self.assertEqual(self.calls, [])

    def chaos(self):
        self.deployment["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"}]

    def test_opaque_in_pod_stdin_only(self):
        self.chaos()
        self.assertTrue(self.probe().get_off("inventory-abc", "uid-1"))
        args, kwargs = self.calls[0]
        self.assertEqual(args, ["kubectl", "--context", "rancher-desktop", "-n", TARGET,
                                "exec", "-i", "inventory-abc", "-c", "inventory", "--", "sh", "-s"])
        self.assertEqual(kwargs["timeout"], 12)
        self.assertTrue(kwargs["capture_output"])
        self.assertIn("printf 'Authorization: Bearer %s", kwargs["input"])
        self.assertIn("-H @-", kwargs["input"])
        self.assertNotIn("POST", kwargs["input"])

    def test_uid_drift_and_wrong_namespace_fail_closed(self):
        self.chaos()
        probe = self.probe()
        self.pod["metadata"]["uid"] = "replacement"
        with self.assertRaises(UnsafeState):
            probe.get_off("inventory-abc", "uid-1")
        self.assertEqual(self.calls, [])
        self.pod["metadata"]["uid"] = "uid-1"
        self.pod["metadata"]["namespace"] = "other"
        with self.assertRaises(UnsafeState):
            probe.get_off("inventory-abc", "uid-1")
        self.assertEqual(self.calls, [])

    def test_post_exec_drift_and_errors_never_expose_output(self):
        self.chaos()
        def runner(*args, **kwargs):
            self.pod["metadata"]["uid"] = "replacement"
            return SimpleNamespace(returncode=0, stdout="private-token", stderr="private-token")
        probe = OpaquePodFaultGet(lambda: self.deployment, lambda: self.pod, runner)
        with self.assertRaises(UnsafeState) as caught:
            probe.get_off("inventory-abc", "uid-1")
        self.assertNotIn("private-token", str(caught.exception))
        self.pod["metadata"]["uid"] = "uid-1"
        for output, rc in (("private-token", 0), ("{\"mode\":\"off\"}\n401", 0),
                           ("private-token", 1), ("a" * 4097, 0)):
            self.output = output
            with self.subTest(rc=rc, output_length=len(output)):
                def failed(args, **kwargs):
                    return SimpleNamespace(returncode=rc, stdout=output, stderr="private-token")
                probe = OpaquePodFaultGet(lambda: self.deployment, lambda: self.pod, failed)
                with self.assertRaises(UnsafeState) as caught:
                    probe.get_off("inventory-abc", "uid-1")
                self.assertNotIn("private-token", str(caught.exception))

    def test_missing_runner_or_profile_change_stops(self):
        self.chaos()
        def failed(*args, **kwargs):
            raise RuntimeError("private-token")
        probe = OpaquePodFaultGet(lambda: self.deployment, lambda: self.pod, failed)
        with self.assertRaises(UnsafeState) as caught:
            probe.get_off("inventory-abc", "uid-1")
        self.assertNotIn("private-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
