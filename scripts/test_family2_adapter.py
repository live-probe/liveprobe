"""No-cluster KAN-123 transport and fail-closed boundary tests."""
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("family2_adapter", Path(__file__).with_name("family2_adapter.py"))
if spec is None or spec.loader is None:
    raise RuntimeError("adapter import failed")
import sys
sys.path.insert(0, str(Path(__file__).parent))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class API:
    def __init__(self):
        self.calls = []
        self.lease = None
        self.items = {}
        self.failure = None

    def read_namespaced_lease(self, name, ns):
        self.calls.append(("lease_get", name, ns))
        if self.lease is None:
            exc = RuntimeError("sensitive response")
            exc.status = 404
            raise exc
        return copy.deepcopy(self.lease)

    def create_namespaced_lease(self, ns, body):
        self.calls.append(("lease_create", ns))
        if self.failure:
            raise RuntimeError("sensitive response")
        self.lease = copy.deepcopy(body)
        self.lease["metadata"]["uid"] = "lease-uid"
        return copy.deepcopy(self.lease)

    def delete_namespaced_lease(self, name, ns, body):
        self.calls.append(("lease_delete", name, ns, copy.deepcopy(body)))
        if body["preconditions"]["uid"] != self.lease["metadata"]["uid"]:
            raise RuntimeError("UID conflict secret")
        self.lease = None

    def read_namespaced_deployment(self, name, ns):
        self.calls.append(("read_dep", name, ns))
        return copy.deepcopy(self.items[(ns, name)])

    def read_namespaced_config_map(self, name, ns):
        self.calls.append(("read_cm", name, ns))
        return copy.deepcopy(self.items[(ns, name)])

    def patch_namespaced_deployment(self, name, ns, body, _content_type):
        return self._patch(name, ns, body, _content_type)

    def patch_namespaced_config_map(self, name, ns, body, _content_type):
        return self._patch(name, ns, body, _content_type)

    def _patch(self, name, ns, body, content_type):
        self.calls.append(("patch", name, ns, content_type))
        item = self.items[(ns, name)]
        field = item["spec"]["template"] if name == "inventory" else item["data"]
        if (body[0]["value"] != item["metadata"]["uid"] or
                body[1]["value"] != item["metadata"]["resourceVersion"] or
                body[2]["value"] != field):
            raise RuntimeError("CAS conflict sensitive body")
        if name == "inventory":
            item["spec"]["template"] = copy.deepcopy(body[3]["value"])
        else:
            item["data"] = copy.deepcopy(body[3]["value"])
        item["metadata"]["resourceVersion"] = "2"
        return copy.deepcopy(item)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.transport = a.KubernetesTransport("rancher-desktop", lambda _: self.api)
        self.dep = {"metadata": {"uid": "d", "resourceVersion": "1", "name": "inventory",
                                 "namespace": a.tx.TARGET}, "spec": {"template": {"profile": None}}}
        self.cm = {"metadata": {"uid": "c", "resourceVersion": "1", "name": "prometheus-config",
                                "namespace": a.tx.OBS}, "data": {"alerts.yml": "for: 30s"}}
        self.api.items[(a.tx.TARGET, "inventory")] = copy.deepcopy(self.dep)
        self.api.items[(a.tx.OBS, "prometheus-config")] = copy.deepcopy(self.cm)

    def test_explicit_scope_and_create_only_uid_delete(self):
        with self.assertRaises(a.tx.UnsafeState):
            a.KubernetesTransport("other", lambda _: self.api)
        lease = a.tx.KubernetesLease(self.transport)
        lease.acquire()
        self.assertEqual(self.api.calls[:2], [("lease_get", a.tx.LEASE, a.tx.TARGET),
                                               ("lease_create", a.tx.TARGET)])
        with self.assertRaises(a.tx.UnsafeState):
            a.tx.KubernetesLease(self.transport).acquire()
        lease.release()
        self.assertEqual(self.api.calls[-1][-1]["preconditions"], {"uid": "lease-uid"})
        with self.assertRaises(a.tx.UnsafeState):
            self.transport.get("other")

    def test_uncertain_create_and_api_errors_are_sanitized(self):
        self.api.failure = True
        with self.assertRaises(a.tx.UnsafeState) as caught:
            a.tx.KubernetesLease(self.transport).acquire()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertFalse(any(call[0] == "lease_delete" for call in self.api.calls))

    def test_scoped_json_patch_cas_and_foreign_edit(self):
        desired = copy.deepcopy(self.dep)
        desired["spec"]["template"]["profile"] = "chaos"
        ops = a.tx.patch_body(self.dep, desired, "/spec/template")
        self.transport.patch(*a.tx.OBJECTS[0], ops)
        self.assertEqual(self.api.items[(a.tx.TARGET, "inventory")]["spec"]["template"],
                         desired["spec"]["template"])
        self.assertEqual(self.api.calls[-1][-1], "application/json-patch+json")
        with self.assertRaises(a.TransportError):
            self.transport.patch(*a.tx.OBJECTS[0], ops)
        with self.assertRaises(a.tx.UnsafeState):
            self.transport.patch(*a.tx.OBJECTS[1], ops)
        with self.assertRaises(a.tx.UnsafeState):
            self.transport.patch(a.tx.TARGET, "configmap", "some-other", ops)

    def test_no_fault_exclusivity_and_observer_denied_by_default(self):
        op = a.NoFaultOperator(self.transport)
        self.assertFalse(op.exclusive_window())
        self.assertFalse(a.IncidentObserver().ready())
        self.assertFalse(a.IncidentObserver().observe({}))
        with self.assertRaises(a.tx.UnsafeState):
            op.fault(mode="oom", duration_seconds=6, pod_uid="uid")
        self.assertTrue(a.IncidentObserver(lambda: True, lambda _: True).ready())
        self.assertFalse(a.IncidentObserver(lambda: 1, lambda _: "ok").ready())

    def test_bounded_verification_and_exact_restore(self):
        op = a.NoFaultOperator(self.transport, health=type("Health", (), {
            "rollout": lambda _, ns, name: True,
            "candidate": lambda _, report: True,
            "restored": lambda _, report: True})(), timeout=.01, interval=.001)
        op.rollout("inventory")
        op.verify_restored([self.dep, self.cm], {})
        self.api.items[(a.tx.OBS, "prometheus-config")]["data"]["alerts.yml"] = "foreign"
        with self.assertRaises(a.tx.UnsafeState):
            op.verify_restored([self.dep, self.cm], {})
        with self.assertRaises(a.tx.UnsafeState):
            op.rollout("other")
        op.health.rollout = lambda ns, name: False
        with self.assertRaisesRegex(a.tx.UnsafeState, "timed out"):
            op.rollout("inventory")

    def test_prepared_operator_requires_boundary_and_delegates_exact_fault(self):
        op = a.PreparedOperator(self.transport)
        self.assertFalse(op.exclusive_window())
        self.assertFalse(op.faults_clear())
        with self.assertRaises(a.tx.UnsafeState):
            op.fault(mode="oom", duration_seconds=6, pod_uid="uid")
        calls = []
        boundary = type("Boundary", (), {
            "faults_clear": lambda _: True,
            "fault": lambda _, **kw: calls.append(kw),
            "clear_fault": lambda _: calls.append("reset")})()
        op = a.PreparedOperator(self.transport, fault_boundary=boundary)
        self.assertTrue(op.faults_clear())
        op.fault(mode="oom", duration_seconds=6, pod_uid="uid")
        op.clear_fault()
        self.assertEqual(calls, [{"pod_uid": "uid", "mode": "oom", "duration_seconds": 6}, "reset"])

    def test_local_flock_contention(self):
        with patch.object(a.gate, "LOCK", Path(__file__).parent.parent / "backups/family2/operator.lock"):
            with a.exclusive_local_lock():
                with self.assertRaises(a.gate.GateError):
                    with a.exclusive_local_lock():
                        pass


if __name__ == "__main__":
    unittest.main()
