"""KAN-124: transport/operator/transaction integration against fake Kubernetes only."""
import copy
import unittest

from family2_adapter import KubernetesTransport, PreparedOperator, IncidentObserver
from family2_transaction import KubernetesLease, UnsafeState, transaction, OBJECTS, field_value
from test_family2_adapter import API


class IntegratedTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.api.items = {
            (OBJECTS[0][0], OBJECTS[0][2]): {
                "kind": "Deployment", "metadata": {"name": "inventory", "namespace": OBJECTS[0][0],
                "uid": "deployment-uid", "resourceVersion": "1"},
                "spec": {"template": {"profile": None}}},
            (OBJECTS[1][0], OBJECTS[1][2]): {
                "kind": "ConfigMap", "metadata": {"name": "prometheus-config", "namespace": OBJECTS[1][0],
                "uid": "config-uid", "resourceVersion": "1"},
                "data": {"alerts.yml": "for: 30s"}},
        }
        self.transport = KubernetesTransport("rancher-desktop", lambda _: self.api)
        self.lease = KubernetesLease(self.transport)
        self.before = [copy.deepcopy(self.api.items[(ns, name)]) for ns, _, name in OBJECTS]
        self.desired = copy.deepcopy(self.before)
        self.desired[0]["spec"]["template"]["profile"] = "chaos"
        self.desired[1]["data"]["alerts.yml"] = "for: 0s"
        self.calls = []
        self.boundary = type("Boundary", (), {
            "faults_clear": lambda _: True,
            "fault": lambda _, **kw: self.calls.append(("fault", kw)),
            "clear_fault": lambda _: self.calls.append(("reset",))})()
        self.health = type("Health", (), {
            "rollout": lambda _, ns, name: True,
            "candidate": lambda _, report: True,
            "restored": lambda _, report: True})()
        self.operator = PreparedOperator(self.transport, fault_boundary=self.boundary,
                                         exclusive_authority=lambda: True, health=self.health,
                                         timeout=.01, interval=.001)
        self.operator.preflight = lambda: (tuple(copy.deepcopy(self.before)),
                                           tuple(copy.deepcopy(self.desired)),
                                           {"pod_uid": "old-pod"})  # type: ignore[method-assign]
        self.operator.current_pod_uid = lambda: "new-pod"  # type: ignore[method-assign]
        self.backups = []
        self.observer = IncidentObserver(lambda: True, lambda report: True)

    def run_transaction(self):
        return transaction(self.operator, self.observer, self.lease,
                           lambda before, desired: self.backups.append(copy.deepcopy(before)))

    def test_success_exact_restore_and_uid_release(self):
        result = self.run_transaction()
        self.assertTrue(result["restored"])
        self.assertEqual([field_value(self.api.items[(ns, name)], i)
                          for i, (ns, _, name) in enumerate(OBJECTS)],
                         [field_value(item, i) for i, item in enumerate(self.before)])
        self.assertEqual([c[0] for c in self.calls], ["fault", "reset"])
        self.assertEqual(len(self.backups), 1)
        self.assertIsNone(self.api.lease)
        self.assertEqual(len([c for c in self.api.calls if c[0] == "patch"]), 4)
        self.assertEqual(len([c for c in self.api.calls if c[0] == "lease_delete"]), 1)

    def test_missing_independent_authority_denies_before_snapshot_or_patch(self):
        self.operator.exclusive_authority = None
        with self.assertRaisesRegex(UnsafeState, "another fault worker"):
            self.run_transaction()
        self.assertEqual(self.backups, [])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.api.lease)

    def test_missing_authenticated_observer_denies_before_snapshot_or_patch(self):
        self.observer = IncidentObserver()
        with self.assertRaisesRegex(UnsafeState, "authenticated"):
            self.run_transaction()
        self.assertEqual(self.backups, [])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.api.lease)

    def test_second_patch_failure_restores_first_without_fault(self):
        original = self.api.patch_namespaced_config_map
        def reject_candidate(name, ns, body, _content_type):
            if body[-1]["value"]["alerts.yml"] == "for: 0s":
                raise RuntimeError("sensitive API response")
            return original(name, ns, body, _content_type)
        self.api.patch_namespaced_config_map = reject_candidate
        with self.assertRaisesRegex(UnsafeState, "suppressed"):
            self.run_transaction()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.api.lease)
        self.assertEqual(self.api.items[(OBJECTS[0][0], "inventory")]["spec"]["template"],
                         self.before[0]["spec"]["template"])

    def test_observer_failure_resets_restores_and_releases_without_proof(self):
        self.observer = IncidentObserver(lambda: True, lambda report: False)
        with self.assertRaisesRegex(UnsafeState, "unverified"):
            self.run_transaction()
        self.assertEqual([c[0] for c in self.calls], ["fault", "reset"])
        self.assertIsNone(self.api.lease)
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"],
                         self.before[1]["data"])

    def test_reset_failure_restores_both_but_retains_lease(self):
        def failed_reset():
            raise RuntimeError("sensitive token")
        self.boundary.clear_fault = failed_reset  # type: ignore[attr-defined]
        with self.assertRaisesRegex(UnsafeState, "Lease retained") as error:
            self.run_transaction()
        self.assertNotIn("sensitive", str(error.exception))
        self.assertIsNotNone(self.api.lease)
        self.assertEqual(self.api.items[(OBJECTS[0][0], "inventory")]["spec"]["template"],
                         self.before[0]["spec"]["template"])
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"],
                         self.before[1]["data"])

    def test_foreign_edit_after_fault_retains_lease_and_restores_other_object(self):
        def foreign_edit(report):
            self.api.items[(OBJECTS[0][0], "inventory")]["spec"]["template"]["foreign"] = True
            return False
        self.observer.proof_check = foreign_edit
        with self.assertRaisesRegex(UnsafeState, "rollback incomplete"):
            self.run_transaction()
        self.assertIsNotNone(self.api.lease)
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"],
                         self.before[1]["data"])
        self.assertTrue(self.api.items[(OBJECTS[0][0], "inventory")]["spec"]["template"]["foreign"])


if __name__ == "__main__":
    unittest.main()
