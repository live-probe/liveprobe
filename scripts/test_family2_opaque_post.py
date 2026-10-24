"""Fake-only opaque POST/reset plus transaction integration; no cluster calls."""
import copy
import os
import subprocess
import unittest
from types import SimpleNamespace

from family2_opaque import OpaquePodFault, _GET_SCRIPT, _POST_SCRIPT, _RESET_SCRIPT
from family2_transaction import KubernetesLease, UnsafeState, transaction, OBJECTS
from test_family2_integration import IntegratedTests


class OpaquePostTests(unittest.TestCase):
    def setUp(self):
        IntegratedTests.setUp(self)  # type: ignore[arg-type]
        self.dep = {"metadata": {"generation": 1},
                    "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                        "name": "inventory", "image": "inventory@sha256:" + "a" * 64,
                        "resources": {"limits": {"memory": "512Mi"}},
                        "env": [{"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"}]}]}}},
                    "status": {"observedGeneration": 1, "readyReplicas": 1,
                               "updatedReplicas": 1, "availableReplicas": 1}}
        self.pod = {"metadata": {"namespace": OBJECTS[0][0], "name": "inventory-abc", "uid": "new-pod"},
                    "status": {"phase": "Running", "containerStatuses": [
                        {"name": "inventory", "ready": True}]}}
        self.exec_calls = []
        self.fail_post = False
        self.fail_reset = False
        self.reset_uid_drift = False
        self.boundary = OpaquePodFault(lambda: self.dep, lambda: self.pod, self.runner)
        self.operator.fault_boundary = self.boundary

    def run_transaction(self):
        return transaction(self.operator, self.observer, self.lease,
                           lambda before, desired: self.backups.append(copy.deepcopy(before)))

    def runner(self, args, **kwargs):
        script = kwargs["input"]
        self.exec_calls.append((args, kwargs))
        if script.find("/v1/reset") != -1:
            if self.reset_uid_drift:
                self.pod["metadata"]["uid"] = "replacement"
            if self.fail_reset:
                raise TimeoutError("sensitive-token")
            output = '{"mode":"off","intent_cleared":true}\n200'
        elif "--data-binary" in script:
            if self.fail_post:
                raise TimeoutError("sensitive-token")
            output = '{"mode":"oom","effect":"pending"}\n202'
        else:
            output = '{"mode":"off"}\n200'
        return SimpleNamespace(returncode=0, stdout=output, stderr="sensitive-token")

    def test_fixed_scripts_pass_header_only_to_mock_curl_stdin(self):
        # Execute the actual POSIX shell, with curl replaced by a shell function.
        # Synthetic token never enters a real process argv or any network request.
        mock = '''curl() {
  [ "$1" = "-q" ] || exit 31
  IFS= read -r header || exit 32
  [ "$header" = "Authorization: Bearer $CHAOS_OPERATOR_TOKEN" ] || exit 33
  printf '{"mode":"off"}\\n200'
}
'''
        env = dict(os.environ, CHAOS_OPERATOR_TOKEN="synthetic-test-only")
        for script in (_GET_SCRIPT, _POST_SCRIPT, _RESET_SCRIPT):
            syntax = subprocess.run(["sh", "-n"], input=script, text=True,
                                    capture_output=True, timeout=3)
            self.assertEqual(syntax.returncode, 0)
            result = subprocess.run(["sh", "-s"], input=mock + script, text=True,
                                    capture_output=True, timeout=3, env=env)
            self.assertEqual(result.returncode, 0, result.returncode)
            self.assertNotIn(env["CHAOS_OPERATOR_TOKEN"], result.stdout + result.stderr)

    def test_opaque_full_fake_path(self):
        self.assertTrue(self.run_transaction()["restored"])
        self.assertEqual(len(self.exec_calls), 6)  # repeated fail-closed GETs around POST/reset
        for args, kw in self.exec_calls:
            self.assertEqual(args, ["kubectl", "--context", "rancher-desktop", "-n", "liveprobe-target",
                                    "exec", "-i", "inventory-abc", "-c", "inventory", "--", "sh", "-s"])
            self.assertEqual(kw["timeout"], 12)
            self.assertTrue(kw["capture_output"])
            self.assertIn("--noproxy '*'", kw["input"])
            self.assertIn("-H @-", kw["input"])
            self.assertIn("set +x", kw["input"])
            self.assertNotIn("sensitive-token", str(args) + kw["input"])
        self.assertTrue(any('"duration_seconds":6' in kw["input"] for _, kw in self.exec_calls))
        self.assertEqual(len([c for c in self.api.calls if c[0] == "patch"]), 4)
        self.assertEqual(len([c for c in self.api.calls if c[0] == "lease_create"]), 1)
        self.assertIsNone(self.api.lease)

    def test_uncertain_post_attempts_reset_and_retains_lease(self):
        self.fail_post = True
        with self.assertRaises(UnsafeState) as caught:
            self.run_transaction()
        self.assertNotIn("sensitive-token", str(caught.exception))
        self.assertIsNotNone(self.api.lease)
        self.assertIsNone(self.boundary.fault_uid)
        self.assertTrue(any("/v1/reset" in kw["input"] for _, kw in self.exec_calls))
        self.assertEqual(self.api.items[(OBJECTS[0][0], "inventory")]["spec"]["template"],
                         self.before[0]["spec"]["template"])
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"],
                         self.before[1]["data"])

    def test_uncertain_post_and_reset_retains_lease_and_ownership(self):
        self.fail_post = self.fail_reset = True
        with self.assertRaisesRegex(UnsafeState, "Lease retained"):
            self.run_transaction()
        self.assertIsNotNone(self.api.lease)
        self.assertEqual(self.boundary.fault_uid, "new-pod")
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"], self.before[1]["data"])

    def test_uid_drift_on_reset_retains_lease(self):
        self.reset_uid_drift = True
        with self.assertRaisesRegex(UnsafeState, "Lease retained"):
            self.run_transaction()
        self.assertIsNotNone(self.api.lease)
        self.assertEqual(self.boundary.fault_uid, "new-pod")

    def test_wrong_profile_and_duration_stop_before_post(self):
        self.dep["spec"]["template"]["spec"]["containers"][0]["env"] = []
        with self.assertRaises(UnsafeState):
            self.boundary.fault("new-pod")
        self.assertEqual(self.exec_calls, [])
        with self.assertRaises(UnsafeState):
            self.boundary.fault("new-pod", duration_seconds=7)
        self.assertEqual(self.exec_calls, [])

    def test_rejection_never_disowns_fault(self):
        def rejected(args, **kwargs):
            if "--data-binary" in kwargs["input"]:
                return SimpleNamespace(returncode=0, stdout='{"error":"private"}\n401', stderr="private")
            return self.runner(args, **kwargs)
        self.boundary.runner = rejected
        with self.assertRaises(UnsafeState) as caught:
            self.boundary.fault("new-pod")
        self.assertEqual(self.boundary.fault_uid, "new-pod")
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
