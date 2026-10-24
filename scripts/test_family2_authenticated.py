"""KAN-126 fake-only authenticated interfaces; no kubectl or HTTP performed."""
import unittest

from family2_adapter import AuthenticatedIncidents, PodLoopbackForward
from family2_transaction import UnsafeState, TARGET


class FakeProcess:
    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout):
        return 0

    def kill(self):
        self.stopped = True


class AuthenticatedTests(unittest.TestCase):
    def setUp(self):
        self.pod = {"metadata": {"namespace": TARGET, "name": "inventory-abc", "uid": "uid-1"}}
        self.process = FakeProcess()
        self.args = []
        self.probes = iter([False, True])
        self.calls = []

    def forward(self, callback=None):
        def launch(args):
            self.args.append(args)
            return self.process
        return PodLoopbackForward(lambda: self.pod, "inventory-abc", "uid-1", 18080,
                                  authenticated_request=callback, launcher=launch,
                                  probe=lambda port: next(self.probes), timeout=.1)

    def test_forward_requires_callback_before_launch(self):
        with self.assertRaisesRegex(UnsafeState, "callback"):
            with self.forward():
                pass
        self.assertEqual(self.args, [])

    def test_bound_loopback_and_cleanup(self):
        def callback(url, method, body):
            self.calls.append((url, method, body))
            return 200, {"mode": "off"}
        forward = self.forward(callback)
        with forward as active:
            self.assertEqual(active.request("GET", "/internal/chaos/v1/faults"),
                             (200, {"mode": "off"}))
            with self.assertRaises(UnsafeState):
                active.request("GET", "/api/secrets")
        self.assertTrue(self.process.stopped)
        self.assertEqual(self.args[0], ["kubectl", "--context", "rancher-desktop", "-n", TARGET,
                                        "port-forward", "--address", "127.0.0.1",
                                        "pod/inventory-abc", "18080:8080"])
        self.assertEqual(self.calls, [("http://127.0.0.1:18080/internal/chaos/v1/faults", "GET", None)])
        with self.assertRaises(UnsafeState):
            forward.request("GET", "/internal/chaos/v1/faults")

    def test_uid_change_and_callback_failure_are_sanitized(self):
        def fail(*args):
            raise RuntimeError("secret-cookie-sensitive")
        with self.forward(fail) as active:
            with self.assertRaises(UnsafeState) as caught:
                active.request("GET", "/internal/chaos/v1/faults")
            self.assertNotIn("sensitive", str(caught.exception))
            self.pod["metadata"]["uid"] = "replacement"
            with self.assertRaisesRegex(UnsafeState, "identity"):
                active.request("GET", "/internal/chaos/v1/faults")
        self.assertTrue(self.process.stopped)

    def test_preexisting_listener_or_failed_process_never_requests(self):
        self.probes = iter([True])
        with self.assertRaisesRegex(UnsafeState, "occupied"):
            with self.forward(lambda *args: (200, {})):
                pass
        self.assertEqual(self.args, [])
        self.probes = iter([False])
        self.process.stopped = True
        with self.assertRaisesRegex(UnsafeState, "unavailable"):
            with self.forward(lambda *args: (200, {})):
                pass

    def test_incident_auth_and_full_baseline(self):
        paths = []
        def callback(path):
            paths.append(path)
            if path.startswith("/api/incidents?"):
                return 200, {"items": [{"id": 59}], "total": 1}
            return 200, {"incident": {"id": 59}}
        observer = AuthenticatedIncidents(callback)
        self.assertTrue(observer.ready())
        self.assertEqual(observer.get_incident(59)["incident"]["id"], 59)
        self.assertEqual(paths, ["/api/incidents?limit=500&offset=0", "/api/incidents/59"])
        with self.assertRaises(UnsafeState):
            observer.get_incident("../../secret")

    def test_incident_fail_closed_without_callback_or_truncated_baseline(self):
        with self.assertRaisesRegex(UnsafeState, "callback"):
            AuthenticatedIncidents().ready()
        for response in [(401, {}), (200, {"items": [{"id": 1}], "total": 60})]:
            with self.subTest(response=response[0]):
                with self.assertRaises(UnsafeState):
                    AuthenticatedIncidents(lambda _: response).ready()
        def fail(_):
            raise RuntimeError("secret-cookie-sensitive")
        with self.assertRaises(UnsafeState) as caught:
            AuthenticatedIncidents(fail).ready()
        self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
