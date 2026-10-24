"""KAN-131 fake-only tests for the fail-closed live orchestrator. No cluster calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import family2_gate as gate  # noqa: E402
import family2_live as live  # noqa: E402
import family2_transaction as tx  # noqa: E402
import family2_webhook_audit as audit  # noqa: E402
from test_family2_adapter import API  # noqa: E402


def board(rows):
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


class ExclusiveTests(unittest.TestCase):
    def test_running_cards_read_only(self):
        path = board([("t_aaaaaaaa", "running"), ("t_bbbbbbbb", "done"), ("t_cccccccc", "running")])
        self.addCleanup(os.unlink, path)
        before = Path(path).read_bytes()
        self.assertEqual(live.running_cards(path), ["t_aaaaaaaa", "t_cccccccc"])
        self.assertEqual(Path(path).read_bytes(), before)

    def test_missing_board_fails_closed(self):
        with self.assertRaises(tx.UnsafeState):
            live.running_cards("/nonexistent/kanban.db")

    def test_declaration_required(self):
        for bad in ("", None, "t_1", "rm -rf"):
            with self.assertRaises(tx.UnsafeState):
                live.ExclusiveAuthority(bad)

    def test_only_declared_card_and_no_active_job(self):
        now = [0.0]
        cards = [["t_aaaaaaaa"]]
        jobs = [[]]
        auth = live.ExclusiveAuthority("t_aaaaaaaa", cards=lambda: cards[0], jobs=lambda: jobs[0],
                                       clock=lambda: now[0], window=1800)
        self.assertTrue(auth())
        cards[0] = ["t_aaaaaaaa", "t_bbbbbbbb"]
        self.assertFalse(auth())
        cards[0] = []
        self.assertFalse(auth())
        cards[0] = ["t_aaaaaaaa"]
        jobs[0] = ["ns/chaos"]
        self.assertFalse(auth())
        jobs[0] = []
        now[0] = 1800.0
        self.assertFalse(auth())  # 30 minute window is a hard bound

    def test_input_failure_is_false(self):
        def broken():
            raise RuntimeError("secret detail")
        auth = live.ExclusiveAuthority("t_aaaaaaaa", cards=broken, jobs=lambda: [])
        self.assertFalse(auth())
        self.assertNotIn("secret", json.dumps(auth.last))

    def test_active_jobs_filters_completed(self):
        payload = {"items": [
            {"metadata": {"namespace": "kube-system", "name": "done"}, "status": {"succeeded": 1}},
            {"metadata": {"namespace": "x", "name": "live"}, "status": {"active": 1}}]}
        self.assertEqual(live.active_jobs(lambda *a, **k: json.dumps(payload)), ["x/live"])


class PrometheusTests(unittest.TestCase):
    def test_get_only_allowlisted_paths(self):
        calls = []
        def runner(args, **kw):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout='{"status":"success"}')
        self.assertEqual(live.prometheus_get_json("prometheus", "/api/v1/rules", runner)["status"], "success")
        self.assertEqual(calls[0][:4], ["kubectl", "--context", "rancher-desktop", "get"])
        self.assertIn("--raw", calls[0])
        for target, path in (("prometheus", "/-/reload"), ("prometheus", "/api/v1/admin/tsdb"),
                             ("alertmanager", "/api/v1/rules")):
            with self.assertRaises(tx.UnsafeState):
                live.prometheus_get_json(target, path, runner)
        self.assertEqual(len(calls), 1)

    def test_failure_suppressed(self):
        runner = lambda *a, **k: SimpleNamespace(returncode=1, stdout="token=private")  # noqa: E731
        with self.assertRaises(tx.UnsafeState) as caught:
            live.prometheus_get_json("prometheus", "/api/v1/rules", runner)
        self.assertNotIn("private", str(caught.exception))


class DbIncidentTests(unittest.TestCase):
    def make(self, outputs):
        calls = []
        def runner(args, **kw):
            calls.append(args[-1])
            return SimpleNamespace(returncode=0, stdout=outputs[len(calls) - 1])
        return live.DbIncidents(audit.WebhookAuditObserver(runner)), calls

    def test_constant_select_projection(self):
        incidents, calls = self.make(["9\n7\n", "9|1|1|1\n7|1|0|0\n"])
        self.assertEqual(incidents.list_incidents(), {"items": [{"id": 9}, {"id": 7}]})
        self.assertEqual(incidents.get_incident(9)["signal_reasons"], ["TargetWorkloadOOMKilled"])
        for sql in calls:
            self.assertTrue(sql.startswith("SELECT"))
            self.assertNotIn("9", sql.replace("50", "").replace("500", ""))  # id never interpolated
            for word in ("INSERT", "UPDATE", "DELETE", "title", "summary", "detail"):
                self.assertNotIn(word, sql)

    def test_mismatch_and_missing(self):
        incidents, _ = self.make(["7|1|0|0\n"])
        self.assertEqual(incidents.get_incident(7)["workload"], "other")
        incidents, _ = self.make(["7|1|1|1\n"])
        with self.assertRaises(tx.UnsafeState):
            incidents.get_incident(8)
        with self.assertRaises(tx.UnsafeState):
            incidents.get_incident("8; DROP")


def dep(profile=None, volumes=None):
    env = [{"name": "SPRING_PROFILES_ACTIVE", "value": profile}] if profile else []
    return {"metadata": {"generation": 1},
            "spec": {"replicas": 1, "template": {"spec": {"volumes": volumes, "containers": [{
                "name": "inventory", "image": "inventory@sha256:" + "a" * 64,
                "resources": {"limits": {"memory": "512Mi"}}, "env": env}]}}},
            "status": {"observedGeneration": 1, "readyReplicas": 1, "updatedReplicas": 1,
                       "availableReplicas": 1}}


def pod(profile=None, uid="u1", ready=True, volumes=()):
    env = [{"name": "SPRING_PROFILES_ACTIVE", "value": profile}] if profile else []
    return {"metadata": {"namespace": tx.TARGET, "name": "inventory-abc", "uid": uid,
                         "labels": {"pod-template-hash": "h1"}},
            "spec": {"containers": [{"name": "inventory", "env": env}], "volumes": list(volumes)},
            "status": {"phase": "Running", "containerStatuses": [{"name": "inventory", "ready": ready}]}}


class FakeOpaque:
    def __init__(self):
        self.fault_uid = None
        self.gets = self.resets = 0
        self.off = True

    def faults_clear(self):
        self.gets += 1
        return self.off

    def fault(self, pod_uid, mode, duration_seconds):
        self.fault_uid = pod_uid

    def clear_fault(self):
        self.resets += 1
        self.fault_uid = None
        return True


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.d, self.p, self.o = dep(), pod(), FakeOpaque()
        self.b = live.LiveFaultBoundary(lambda: self.d, lambda: self.p, self.o, timeout=.05, interval=.001)

    def test_default_profile_is_structurally_clear_without_exec(self):
        self.assertTrue(self.b.faults_clear())
        self.assertEqual(self.o.gets, 0)

    def test_default_with_chaos_volume_or_not_ready_is_not_clear(self):
        self.p = pod(volumes=[{"name": "chaos-state"}])
        self.assertFalse(self.b.faults_clear())
        self.p = pod(ready=False)
        self.assertFalse(self.b.faults_clear())
        self.d = dep(volumes=[{"name": "chaos-state"}])
        self.p = pod()
        self.assertFalse(self.b.faults_clear())

    def test_chaos_uses_opaque_get(self):
        self.d, self.p = dep("chaos"), pod("chaos")
        self.assertTrue(self.b.faults_clear())
        self.o.off = False
        self.assertFalse(self.b.faults_clear())
        self.assertEqual(self.o.gets, 2)

    def test_profile_mismatch_stops(self):
        self.d, self.p = dep("chaos"), pod(None)
        with self.assertRaises(tx.UnsafeState):
            self.b.faults_clear()

    def test_owned_fault_never_clear(self):
        self.o.fault_uid = "u1"
        self.assertFalse(self.b.faults_clear())

    def test_reset_waits_for_same_uid_and_refuses_replacement(self):
        self.o.fault_uid = "u1"
        states = [pod(uid="u1", ready=False), pod(uid="u1", ready=True)]
        self.b.pod_reader = lambda: states.pop(0) if len(states) > 1 else states[0]
        self.assertTrue(self.b.clear_fault())
        self.assertEqual(self.o.resets, 1)
        self.o.fault_uid = "u1"
        self.b.pod_reader = lambda: pod(uid="replacement")
        with self.assertRaisesRegex(tx.UnsafeState, "replaced"):
            self.b.clear_fault()
        self.assertEqual(self.o.fault_uid, "u1")  # ownership retained for manual recovery
        self.b.pod_reader = lambda: pod(uid="u1", ready=False)
        with self.assertRaises(tx.UnsafeState):
            self.b.clear_fault()
        self.assertEqual(self.o.resets, 1)


class HelperTests(unittest.TestCase):
    def test_superset_only_additive_defaults(self):
        self.assertTrue(live.superset({"a": 1, "scheme": "HTTP"}, {"a": 1}))
        self.assertFalse(live.superset({"a": 2}, {"a": 1}))
        self.assertFalse(live.superset([1, 2], [1]))
        self.assertFalse(live.superset({}, {"a": None}))

    def test_cas_shape_builds_but_never_sends(self):
        before = [{"metadata": {"uid": "d", "resourceVersion": "1", "name": "inventory", "namespace": tx.TARGET},
                   "spec": {"template": {"x": 1}}},
                  {"metadata": {"uid": "c", "resourceVersion": "2", "name": "prometheus-config",
                                "namespace": tx.OBS}, "data": {"alerts.yml": "for: 30s"}}]
        desired = copy.deepcopy(before)
        desired[0]["spec"]["template"]["x"] = 2
        desired[1]["data"]["alerts.yml"] = "for: 0s"
        shapes = live.cas_shape(before, desired)
        self.assertEqual(shapes[0]["ops"], ["test /metadata/uid", "test /metadata/resourceVersion",
                                            "test /spec/template", "replace /spec/template"])
        self.assertEqual(shapes[1]["ops"][-1], "replace /data")
        self.assertNotIn("for: 0s", json.dumps(shapes))  # hashes only

    def test_oom_rule_for(self):
        cm = {"data": {"alerts.yml": "groups:\n- name: g\n  rules:\n  - alert: TargetWorkloadOOMKilled\n"
                                     "    expr: x\n    for: 30s\n"}}
        self.assertEqual(live.oom_rule_for(cm), "30s")
        cm["data"]["alerts.yml"] = cm["data"]["alerts.yml"].replace("30s", "5m")
        with self.assertRaises(tx.UnsafeState):
            live.oom_rule_for(cm)


class RestarterTests(unittest.TestCase):
    def test_uid_precondition_delete_in_scope_only(self):
        api = API()
        deleted = []
        api.delete_namespaced_pod = lambda name, ns, body: deleted.append((name, ns, body))
        transport = live.adapter.KubernetesTransport("rancher-desktop", lambda _: api)
        r = live.PrometheusRestarter(transport, run=lambda *a, **k: "{}")
        good = {"metadata": {"namespace": tx.OBS, "name": "prometheus-5d9-abc", "uid": "p1",
                             "ownerReferences": [{"kind": "ReplicaSet"}]}}
        r.restart(good)
        self.assertEqual(deleted[0][2]["preconditions"], {"uid": "p1"})
        for bad in ({"namespace": tx.TARGET}, {"name": "inventory-1"}, {"uid": ""},
                    {"ownerReferences": [{"kind": "StatefulSet"}]}):
            item = copy.deepcopy(good)
            item["metadata"].update(bad)
            with self.assertRaises(tx.UnsafeState):
                r.restart(item)
        self.assertEqual(len(deleted), 1)


class KubectlCasPatchTests(unittest.TestCase):
    """KAN-130: CAS writes go through kubectl (Go JSON escaping), scope-checked."""
    def operator(self, calls, uid="c"):
        def run(*args, **kwargs):
            calls.append((args, kwargs))
            return json.dumps({"metadata": {"uid": uid}})
        return live.LiveOperator(object(), None, None, run=run, health=object())

    def ops(self, field="/data"):
        return [{"op": "test", "path": "/metadata/uid", "value": "c"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "2"},
                {"op": "test", "path": field, "value": {"alerts.yml": "x > 0"}},
                {"op": "replace", "path": field, "value": {"alerts.yml": "x > 1"}}]

    def test_cas_patch_uses_kubectl_json_patch_in_scope(self):
        calls = []
        self.operator(calls).patch(tx.OBS, "configmap", "prometheus-config", self.ops())
        args, kwargs = calls[0]
        self.assertEqual(args[:4], ("patch", "configmap", "prometheus-config", "--type=json"))
        self.assertNotIn("--dry-run=server", args)
        self.assertEqual(kwargs["namespace"], tx.OBS)
        self.assertEqual(json.loads(args[5]), self.ops())

    def test_non_cas_or_out_of_scope_rejected_before_kubectl(self):
        calls = []
        op = self.operator(calls)
        with self.assertRaises(tx.UnsafeState):
            op.patch(tx.OBS, "configmap", "other", self.ops())
        with self.assertRaises(tx.UnsafeState):
            op.patch(tx.OBS, "configmap", "prometheus-config", self.ops()[2:])
        with self.assertRaises(tx.UnsafeState):
            op.patch(tx.OBS, "configmap", "prometheus-config", self.ops("/spec/template"))
        self.assertEqual(calls, [])

    def test_identity_mismatch_after_patch_is_unsafe(self):
        with self.assertRaises(tx.UnsafeState):
            self.operator([], uid="other").patch(tx.OBS, "configmap", "prometheus-config", self.ops())


class CliTests(unittest.TestCase):
    def run_main(self, argv, parts=None):
        built = []
        def build(card):
            built.append(card)
            return parts
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = live.main(argv, build_parts=build, lock=contextlib.nullcontext)
        return code, out.getvalue(), built

    def test_live_requires_confirmation_before_any_build(self):
        for argv in (["--context", "rancher-desktop", "--live"],
                     ["--context", "rancher-desktop", "--live", "--confirm-oom-injection", "yes",
                      "--exclusive-card", "t_aaaaaaaa", "--backup-dir", "/tmp/x"],
                     ["--context", "rancher-desktop", "--live", "--confirm-oom-injection", live.CONFIRM]):
            with self.assertRaises(tx.UnsafeState):
                self.run_main(argv)

    def test_wrong_context_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            live.parse(["--context", "prod"])

    def test_live_escalates_when_lease_retained(self):
        lease = SimpleNamespace(uid="lease-uid")
        boundary = SimpleNamespace(fault_uid=None)
        parts = {"lease": lease, "boundary": boundary}
        with mock.patch.object(live, "live", side_effect=tx.UnsafeState("rollback incomplete")):
            code, out, built = self.run_main(
                ["--context", "rancher-desktop", "--live", "--confirm-oom-injection", live.CONFIRM,
                 "--exclusive-card", "t_aaaaaaaa", "--backup-dir", "/nonexistent/new"], parts)
        self.assertEqual(code, 4)
        self.assertEqual(built, ["t_aaaaaaaa"])
        self.assertEqual(json.loads(out)["result"], "ESCALATE")
        self.assertTrue(json.loads(out)["lease_retained"])

    def test_live_stop_without_lease_is_plain_stop(self):
        parts = {"lease": SimpleNamespace(uid=None), "boundary": SimpleNamespace(fault_uid=None)}
        with mock.patch.object(live, "live", side_effect=RuntimeError("private body")):
            code, out, _ = self.run_main(
                ["--context", "rancher-desktop", "--live", "--confirm-oom-injection", live.CONFIRM,
                 "--exclusive-card", "t_aaaaaaaa", "--backup-dir", "/nonexistent/new"], parts)
        self.assertEqual(code, 3)
        self.assertNotIn("private", out)

    def test_live_refuses_without_exclusive_window(self):
        tmp = tempfile.mkdtemp()
        parts = {"authority": lambda: False}
        with self.assertRaisesRegex(tx.UnsafeState, "exclusive"):
            live.live(parts, Path(tmp) / "new")
        self.assertFalse((Path(tmp) / "new").exists())


class DryRunTests(unittest.TestCase):
    """Dry-run against fakes: evaluates every gate, performs zero writes."""
    def setUp(self):
        self.api = API()
        self.transport = live.adapter.KubernetesTransport("rancher-desktop", lambda _: self.api)
        before = [{"kind": "Deployment", "metadata": {"uid": "d", "resourceVersion": "1", "name": "inventory",
                                                      "namespace": tx.TARGET},
                   "spec": {"template": {"x": 1}}},
                  {"kind": "ConfigMap", "metadata": {"uid": "c", "resourceVersion": "2",
                                                     "name": "prometheus-config", "namespace": tx.OBS},
                   "data": {"alerts.yml": "groups:\n- name: g\n  rules:\n  - alert: TargetWorkloadOOMKilled\n"
                                          "    expr: x\n    for: 30s\n"}}]
        desired = copy.deepcopy(before)
        desired[0]["spec"]["template"]["x"] = 2
        desired[1]["data"]["alerts.yml"] = desired[1]["data"]["alerts.yml"].replace("30s", "0s")
        with mock.patch.object(gate, "profile", return_value=None):
            pass
        operator = SimpleNamespace(preflight=lambda: (before, desired, {"pod_uid": "u"}),
                                   dry_run_verified=True)
        self.parts = {
            "authority": live.ExclusiveAuthority("t_aaaaaaaa", cards=lambda: ["t_aaaaaaaa"], jobs=lambda: []),
            "transport": self.transport, "operator": operator,
            "signals": SimpleNamespace(rule_loaded=lambda d: d == "30s"),
            "boundary": SimpleNamespace(faults_clear=lambda: True),
            "audit": SimpleNamespace(ready=lambda: True),
            "incidents": SimpleNamespace(list_incidents=lambda: {"items": []})}

    def test_dry_run_ready_and_no_writes(self):
        with mock.patch.object(gate, "profile", return_value=None):
            report = live.dry_run(self.parts)
        self.assertTrue(report["ready_for_live"])
        self.assertEqual((report["writes"], report["lease_created"], report["fault_post"]), (0, False, False))
        kinds = {c[0] for c in self.api.calls}
        self.assertEqual(kinds, {"lease_get"})

    def test_dry_run_not_ready_when_other_card_running(self):
        self.parts["authority"] = live.ExclusiveAuthority("t_aaaaaaaa", cards=lambda: ["t_aaaaaaaa", "t_bbbbbbbb"],
                                                          jobs=lambda: [])
        with mock.patch.object(gate, "profile", return_value=None):
            self.assertFalse(live.dry_run(self.parts)["ready_for_live"])

    def test_dry_run_not_ready_with_existing_lease(self):
        self.api.lease = {"metadata": {"uid": "x"}, "spec": {}}
        with mock.patch.object(gate, "profile", return_value=None):
            self.assertFalse(live.dry_run(self.parts)["ready_for_live"])


class LeaseDurationTests(unittest.TestCase):
    def test_lease_is_1800s_and_bounded(self):
        api = API()
        transport = live.adapter.KubernetesTransport("rancher-desktop", lambda _: api)
        lease = tx.KubernetesLease(transport, duration_seconds=live.LEASE_SECONDS)
        lease.acquire()
        self.assertEqual(api.lease["spec"]["leaseDurationSeconds"], 1800)
        for bad in (0, 59, 3601, "1800"):
            with self.assertRaises(tx.UnsafeState):
                tx.KubernetesLease(transport, duration_seconds=bad)


class ExclusiveRecheckBeforePostTests(unittest.TestCase):
    def test_window_lost_before_post_restores_and_never_posts(self):
        from test_family2_integration import IntegratedTests
        case = IntegratedTests("test_success_exact_restore_and_uid_release")
        case.setUp()
        results = iter([True, False])
        case.operator.exclusive_authority = lambda: next(results)
        with self.assertRaisesRegex(tx.UnsafeState, "exclusive window lost"):
            case.run_transaction()
        self.assertEqual(case.calls, [])  # no fault, no reset
        self.assertIsNone(case.api.lease)  # restored + no uncertainty -> released
        for index, (ns, _, name) in enumerate(tx.OBJECTS):
            self.assertEqual(tx.field_value(case.api.items[(ns, name)], index),
                             tx.field_value(case.before[index], index))


class GateStopTests(unittest.TestCase):
    def test_family2_gate_execute_still_stops_before_kubectl(self):
        with mock.patch.object(sys, "argv", ["family2_gate.py", "--context", "rancher-desktop",
                                             "--backup-dir", "/nonexistent", "--execute"]), \
                mock.patch.object(gate.subprocess, "run") as run:
            with self.assertRaisesRegex(gate.GateError, "live execution disabled"):
                gate.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
