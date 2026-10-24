"""KAN-132 fake-only tests: post-OOM same-UID reset acknowledgement semantics.

After an OOMKilled restart of the SAME Pod UID the in-JVM fault state and the
bootstrap intent are gone, so the reset may answer intent_cleared:false or hit
the restart window. A read-only opaque GET showing mode off counts as clear
only when the restart is verified; every other case stays fail-closed.
No cluster calls, no real kubectl, no token.
"""
import copy
from types import SimpleNamespace
import unittest

import family2_live as live
from family2_opaque import OpaquePodFault
from family2_transaction import UnsafeState, OBJECTS, transaction
from test_family2_integration import IntegratedTests

BASE_CID = "containerd://base"
NEW_CID = "containerd://after-oom"


def container(restarts=0, cid=BASE_CID, ready=True, reason=None, exit_code=None, running=True):
    status = {"name": "inventory", "ready": ready, "restartCount": restarts, "containerID": cid,
              "state": {"running": {}} if running else {"waiting": {"reason": "CrashLoopBackOff"}},
              "lastState": {}}
    if reason is not None:
        status["lastState"] = {"terminated": {"reason": reason, "exitCode": exit_code}}
    return status


class Harness:
    """Fake chaos Pod + scripted kubectl exec runner (no process spawned)."""

    def __init__(self):
        self.dep = {"metadata": {"generation": 1},
                    "spec": {"replicas": 1, "template": {"spec": {"containers": [{
                        "name": "inventory", "image": "inventory@sha256:" + "a" * 64,
                        "resources": {"limits": {"memory": "512Mi"}},
                        "env": [{"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"}]}]}}},
                    "status": {"observedGeneration": 1, "readyReplicas": 1,
                               "updatedReplicas": 1, "availableReplicas": 1}}
        self.pod = {"metadata": {"namespace": OBJECTS[0][0], "name": "inventory-abc", "uid": "new-pod"},
                    "status": {"phase": "Running", "containerStatuses": [container()]}}
        self.calls = []
        self.oom_on_post = True        # POST -> OOMKilled restart of the same Pod
        self.oom_status = container(1, NEW_CID, reason="OOMKilled", exit_code=137)
        self.reset_output = '{"mode":"off","previous_mode":"off","intent_cleared":false}\n200'
        self.reset_raises = False
        self.get_output = '{"mode":"off"}\n200'
        self.on_get = None  # type: ignore[assignment]  # hook run during GETs after the POST
        self.posted = False
        self.fault = OpaquePodFault(lambda: self.dep, lambda: self.pod, self.runner)

    def kinds(self):
        return [k for k, _ in self.calls]

    def runner(self, args, **kwargs):
        script = kwargs["input"]
        if "/v1/reset" in script:
            self.calls.append(("reset", args))
            if self.reset_raises:
                raise TimeoutError("sensitive-token")
            out = self.reset_output
        elif "--data-binary" in script:
            self.calls.append(("post", args))
            self.posted = True
            if self.oom_on_post:
                self.pod["status"]["containerStatuses"] = [copy.deepcopy(self.oom_status)]
            out = '{"mode":"oom","effect":"pending"}\n202'
        else:
            self.calls.append(("get", args))
            if self.posted and self.on_get:
                self.on_get()
            out = self.get_output
        return SimpleNamespace(returncode=0, stdout=out, stderr="sensitive-token")


class OomResetSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.f = self.h.fault

    def inject(self):
        self.assertTrue(self.f.fault("new-pod"))
        self.assertEqual(self.f.fault_uid, "new-pod")
        self.assertEqual(self.f.fault_container, (0, BASE_CID))

    def test_strict_ack_still_clears_without_restart(self):
        self.h.oom_on_post = False
        self.h.reset_output = '{"mode":"off","intent_cleared":true}\n200'
        self.inject()
        self.assertTrue(self.f.clear_fault())
        self.assertIsNone(self.f.fault_uid)
        self.assertEqual(self.h.kinds().count("reset"), 1)

    def test_intent_cleared_false_after_oom_restart_and_get_off_is_clear(self):
        self.inject()
        self.assertTrue(self.f.clear_fault())
        self.assertIsNone(self.f.fault_uid)
        self.assertIsNone(self.f.fault_container)
        kinds = self.h.kinds()
        self.assertEqual(kinds.count("post"), 1)  # no second fault POST
        self.assertEqual(kinds.count("reset"), 1)
        self.assertEqual(kinds[-1], "get")  # clearance came from the read-only GET

    def test_reset_exec_lost_in_restart_window_then_get_off_is_clear(self):
        self.h.reset_raises = True
        self.inject()
        self.assertTrue(self.f.clear_fault())
        self.assertIsNone(self.f.fault_uid)

    def test_no_restart_and_unvalidated_ack_stays_owned(self):
        self.h.oom_on_post = False
        self.inject()
        with self.assertRaises(UnsafeState) as caught:
            self.f.clear_fault()
        self.assertNotIn("sensitive-token", str(caught.exception))
        self.assertEqual(self.f.fault_uid, "new-pod")
        self.assertEqual(self.h.kinds()[-1], "reset")  # no GET fallback attempted

    def test_restart_without_oomkilled_137_stays_owned(self):
        for reason, code in (("Error", 1), ("OOMKilled", 1), (None, None)):
            with self.subTest(reason=reason, code=code):
                self.setUp()
                self.h.oom_status = container(1, NEW_CID, reason=reason, exit_code=code)
                self.inject()
                with self.assertRaises(UnsafeState):
                    self.f.clear_fault()
                self.assertEqual(self.f.fault_uid, "new-pod")

    def test_restart_count_or_container_id_unchanged_stays_owned(self):
        for status in (container(0, NEW_CID, reason="OOMKilled", exit_code=137),
                       container(1, BASE_CID, reason="OOMKilled", exit_code=137)):
            with self.subTest(status=status["restartCount"]):
                self.setUp()
                self.h.oom_status = status
                self.inject()
                with self.assertRaises(UnsafeState):
                    self.f.clear_fault()
                self.assertEqual(self.f.fault_uid, "new-pod")

    def test_not_running_or_not_ready_after_restart_stays_owned(self):
        for status in (container(1, NEW_CID, ready=False, reason="OOMKilled", exit_code=137),
                       container(1, NEW_CID, running=False, reason="OOMKilled", exit_code=137)):
            with self.subTest():
                self.setUp()
                self.h.oom_status = status
                # The post-POST identity check may already fail (ack lost):
                # ownership is recorded before the exec either way.
                try:
                    self.f.fault("new-pod")
                except UnsafeState:
                    pass
                self.assertEqual(self.f.fault_uid, "new-pod")
                with self.assertRaises(UnsafeState):
                    self.f.clear_fault()
                self.assertEqual(self.f.fault_uid, "new-pod")

    def test_get_not_off_or_invalid_after_restart_stays_owned(self):
        for out in ('{"mode":"latency"}\n200', '{"mode":"off"}\n401', "garbage", '{"mode":"off"}\n500'):
            with self.subTest(out=out):
                self.setUp()
                self.inject()
                self.h.get_output = out
                with self.assertRaises(UnsafeState):
                    self.f.clear_fault()
                self.assertEqual(self.f.fault_uid, "new-pod")

    def test_second_restart_during_get_stays_owned(self):
        self.inject()

        def restart_again():
            self.h.pod["status"]["containerStatuses"] = [
                container(2, "containerd://third", reason="OOMKilled", exit_code=137)]
        self.h.on_get = restart_again
        with self.assertRaisesRegex(UnsafeState, "uncertain"):
            self.f.clear_fault()
        self.assertEqual(self.f.fault_uid, "new-pod")

    def test_replacement_pod_never_accepted(self):
        self.inject()
        self.h.pod["metadata"]["uid"] = "replacement"
        with self.assertRaises(UnsafeState):
            self.f.clear_fault()
        self.assertEqual(self.f.fault_uid, "new-pod")
        self.assertNotIn("reset", self.h.kinds())  # identity check blocks the exec itself

    def test_unobservable_baseline_container_disables_get_fallback(self):
        self.h.pod["status"]["containerStatuses"] = [
            {"name": "inventory", "ready": True, "restartCount": 0}]  # no containerID
        self.assertTrue(self.f.fault("new-pod"))
        self.assertIsNone(self.f.fault_container)
        with self.assertRaises(UnsafeState):
            self.f.clear_fault()
        self.assertEqual(self.f.fault_uid, "new-pod")

    def test_confirm_requires_owned_fault(self):
        with self.assertRaisesRegex(UnsafeState, "no owned fault"):
            self.f.confirm_clear_after_oom()
        self.assertFalse(self.f.restarted_after_oom(self.h.pod))


class OomResetTransactionTests(unittest.TestCase):
    """Transaction integration: OOM restart during the run, reset ack false."""

    def setUp(self):
        IntegratedTests.setUp(self)  # type: ignore[arg-type]
        self.h = Harness()
        self.boundary = self.h.fault
        self.operator.fault_boundary = self.boundary  # type: ignore[attr-defined]

    def run_transaction(self):
        return transaction(self.operator, self.observer, self.lease,  # type: ignore[attr-defined]
                           lambda before, desired: self.backups.append(  # type: ignore[attr-defined]
                               copy.deepcopy(before)))

    def test_post_oom_restart_is_restored_and_lease_released(self):
        result = self.run_transaction()
        self.assertTrue(result["restored"])
        self.assertIsNone(self.api.lease)
        self.assertIsNone(self.boundary.fault_uid)
        self.assertEqual(self.h.kinds().count("post"), 1)

    def test_no_restart_and_false_ack_retains_lease_and_ownership(self):
        self.h.oom_on_post = False
        with self.assertRaisesRegex(UnsafeState, "Lease retained"):
            self.run_transaction()
        self.assertIsNotNone(self.api.lease)
        self.assertEqual(self.boundary.fault_uid, "new-pod")
        # Baseline is still restored even though ownership is retained.
        self.assertEqual(self.api.items[(OBJECTS[1][0], "prometheus-config")]["data"], self.before[1]["data"])


class LiveBoundaryOomTests(unittest.TestCase):
    """LiveFaultBoundary waits for the same UID, then uses the KAN-132 semantics."""

    def test_boundary_clear_after_oom_restart(self):
        h = Harness()
        b = live.LiveFaultBoundary(lambda: h.dep, lambda: h.pod, h.fault, timeout=.05, interval=.001)
        self.assertTrue(h.fault.fault("new-pod"))
        self.assertTrue(b.clear_fault())
        self.assertIsNone(b.fault_uid)

    def test_boundary_keeps_ownership_without_verified_restart(self):
        h = Harness()
        h.oom_on_post = False
        b = live.LiveFaultBoundary(lambda: h.dep, lambda: h.pod, h.fault, timeout=.05, interval=.001)
        self.assertTrue(h.fault.fault("new-pod"))
        with self.assertRaises(UnsafeState):
            b.clear_fault()
        self.assertEqual(b.fault_uid, "new-pod")


if __name__ == "__main__":
    unittest.main()
