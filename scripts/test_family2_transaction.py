"""No-cluster fault and rollback simulation for KAN-122."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("family2_transaction", Path(__file__).with_name("family2_transaction.py"))
if spec is None or spec.loader is None:
    raise RuntimeError("transaction import failed")
tx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tx)


class FakeLeaseAPI:
    def __init__(self):
        self.item = None
        self.creates = 0
        self.deletes = 0

    def get(self, name):
        return copy.deepcopy(self.item)

    def create(self, body):
        if self.item is not None:
            raise RuntimeError("AlreadyExists")
        self.item = copy.deepcopy(body)
        self.item["metadata"]["uid"] = "lease-uid"
        self.creates += 1
        return copy.deepcopy(self.item)

    def delete(self, name, uid):
        if not self.item or self.item["metadata"]["uid"] != uid:
            raise RuntimeError("UID conflict")
        self.deletes += 1
        self.item = None


class FakeOperator:
    def __init__(self):
        self.before = [
            {"kind": "Deployment", "metadata": {"namespace": tx.TARGET, "name": "inventory",
             "uid": "deployment-uid", "resourceVersion": "1"},
             "spec": {"template": {"image": "baseline", "profile": None}}},
            {"kind": "ConfigMap", "metadata": {"namespace": tx.OBS, "name": "prometheus-config",
             "uid": "config-uid", "resourceVersion": "1"}, "data": {"alerts.yml": "for: 30s"}},
        ]
        self.desired = copy.deepcopy(self.before)
        self.desired[0]["spec"]["template"]["profile"] = "chaos"
        self.desired[1]["data"]["alerts.yml"] = "for: 0s"
        self.objects = copy.deepcopy(self.before)
        self.fail: str | None = None
        self.patches = []
        self.fault_count = 0
        self.clear_count = 0

    def exclusive_window(self):
        return self.fail != "window"

    def preflight(self):
        return copy.deepcopy(self.before), copy.deepcopy(self.desired), {"pod_uid": "pod-uid"}

    def faults_clear(self):
        return True

    def get(self, namespace, kind, name):
        index = 0 if kind == "deployment" else 1
        return copy.deepcopy(self.objects[index])

    def patch(self, namespace, kind, name, ops):
        index = 0 if kind == "deployment" else 1
        current = self.objects[index]
        for op in ops[:-1]:
            path = op["path"]
            value = current["metadata"][path.split("/")[-1]] if path.startswith("/metadata/") else tx.field_value(current, index)
            if op["value"] != value:
                raise RuntimeError("CAS conflict")
        if self.fail == "patch" and index == 1 and len(self.patches) == 1:
            raise RuntimeError("second patch failed")
        if index == 0:
            current["spec"]["template"] = copy.deepcopy(ops[-1]["value"])
        else:
            current["data"] = copy.deepcopy(ops[-1]["value"])
        current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
        self.patches.append((index, ops))

    def rollout(self, name):
        if self.fail == "rollout" and self.objects[0]["spec"]["template"]["profile"] == "chaos":
            raise RuntimeError("rollout failed")

    def verify_candidate(self, desired, report):
        if self.fail == "candidate":
            raise RuntimeError("candidate unhealthy")
        assert all(tx.field_value(self.objects[i], i) == tx.field_value(desired[i], i) for i in range(2))

    def current_pod_uid(self):
        return "pod-uid"

    def fault(self, **kwargs):
        assert kwargs == {"mode": "oom", "duration_seconds": 6, "pod_uid": "pod-uid"}
        self.fault_count += 1

    def clear_fault(self):
        self.clear_count += 1
        if self.fail == "reset":
            raise RuntimeError("reset unavailable")

    def verify_restored(self, before, report):
        if self.fail == "restored":
            raise RuntimeError("200 unavailable")
        assert all(tx.field_value(self.objects[i], i) == tx.field_value(before[i], i) for i in range(2))


class FakeObserver:
    def __init__(self):
        self.healthy = True
        self.proof = True

    def ready(self):
        return self.healthy

    def observe(self, report):
        return self.proof


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeLeaseAPI()
        self.lease = tx.KubernetesLease(self.api)
        self.operator = FakeOperator()
        self.observer = FakeObserver()
        self.backups = []

    def run_tx(self):
        return tx.transaction(self.operator, self.observer, self.lease,
                              lambda before, desired: self.backups.append(copy.deepcopy(before)))

    def test_success_restores_and_releases(self):
        self.assertEqual(self.run_tx()["result"], "OFFLINE_TRANSACTION_VERIFIED")
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["profile"], None)
        self.assertEqual(self.operator.objects[1]["data"]["alerts.yml"], "for: 30s")
        self.assertEqual((self.operator.fault_count, self.operator.clear_count, self.api.deletes), (1, 1, 1))
        self.assertEqual(len(self.backups), 1)

    def test_existing_lease_even_expired_is_stop(self):
        self.api.item = {"metadata": {"uid": "old"}, "spec": {"holderIdentity": "other", "leaseDurationSeconds": 1}}
        with self.assertRaises(tx.UnsafeState):
            self.run_tx()
        self.assertEqual((self.api.creates, self.api.deletes, len(self.operator.patches)), (0, 0, 0))

    def test_create_uncertain_never_retries_or_deletes(self):
        def accepted_then_timeout(body):
            self.api.item = copy.deepcopy(body)
            self.api.item["metadata"]["uid"] = "lease-uid"
            raise TimeoutError("server accepted before timeout")
        self.api.create = accepted_then_timeout
        with self.assertRaisesRegex(tx.UnsafeState, "uncertain"):
            self.run_tx()
        self.assertEqual(self.api.deletes, 0)
        self.assertIsNotNone(self.api.item)

    def test_second_holder_cannot_take_over(self):
        self.lease.acquire()
        contender = tx.KubernetesLease(self.api)
        with self.assertRaisesRegex(tx.UnsafeState, "existing Lease"):
            contender.acquire()
        self.assertEqual(self.api.creates, 1)
        self.lease.release()

    def test_preflight_denials_do_not_mutate(self):
        for mode in ("window", "observer", "drift"):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "window":
                    self.operator.fail = mode
                elif mode == "observer":
                    self.observer.healthy = False
                else:
                    self.operator.objects[0]["metadata"]["resourceVersion"] = "2"
                with self.assertRaises(tx.UnsafeState):
                    self.run_tx()
                self.assertEqual((len(self.operator.patches), self.operator.fault_count, self.api.deletes), (0, 0, 1))

    def test_second_patch_failure_restores_first(self):
        self.operator.fail = "patch"
        with self.assertRaises(RuntimeError):
            self.run_tx()
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["profile"], None)
        self.assertEqual((self.api.deletes, self.operator.fault_count), (1, 0))

    def test_partial_patch_does_not_reset_unattempted_fault(self):
        self.operator.fail = "patch"
        with self.assertRaises(RuntimeError):
            self.run_tx()
        self.assertEqual(self.operator.clear_count, 0)
        self.assertEqual(self.api.deletes, 1)

    def test_one_rollback_conflict_still_attempts_other_object(self):
        def conflict_after_fault(report):
            # Simulate a concurrent edit to inventory after both writes.
            self.operator.objects[0]["spec"]["template"]["image"] = "foreign"
            return False
        self.observer.observe = conflict_after_fault
        with self.assertRaisesRegex(tx.UnsafeState, "rollback incomplete"):
            self.run_tx()
        self.assertEqual(self.operator.objects[1]["data"]["alerts.yml"], "for: 30s")
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["image"], "foreign")
        self.assertEqual(self.api.deletes, 0)

    def test_observer_failure_restores_but_never_claims_proof(self):
        self.observer.proof = False
        with self.assertRaises(tx.UnsafeState):
            self.run_tx()
        self.assertEqual((self.operator.fault_count, self.api.deletes), (1, 1))

    def test_restore_failure_retains_lease(self):
        self.operator.fail = "restored"
        with self.assertRaises(RuntimeError):
            self.run_tx()
        self.assertEqual(self.api.deletes, 0)
        self.assertIsNotNone(self.api.item)

    def test_reset_failure_still_restores_but_retains_lease(self):
        self.operator.fail = "reset"
        with self.assertRaises(tx.UnsafeState):
            self.run_tx()
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["profile"], None)
        self.assertEqual(self.api.deletes, 0)

    def test_concurrent_change_never_blindly_restored(self):
        def foreign_change(report):
            self.operator.objects[0]["spec"]["template"]["image"] = "foreign"
            return False
        self.observer.observe = foreign_change
        with self.assertRaises(tx.UnsafeState):
            self.run_tx()
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["image"], "foreign")
        self.assertEqual(self.api.deletes, 0)


if __name__ == "__main__":
    unittest.main()
