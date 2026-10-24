"""KAN-134 fake-only tests: F2 preflight accepts a live OOM rule already at 0s.

(a) live 30s -> scoped ConfigMap patch to 0s (unchanged behaviour);
(b) live already 0s with the exact contract (KSM last-terminated reason
    OOMKilled, /data identical to the local manifest) -> no-op: no ConfigMap
    write, no server dry-run for it, restore also a no-op;
anything else -> fail-closed. No cluster calls.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import family2_adapter as adapter  # noqa: E402
import family2_gate as gate  # noqa: E402
import family2_live as live  # noqa: E402
import family2_transaction as tx  # noqa: E402
from test_family2_adapter import API  # noqa: E402
from test_family2_transaction import FakeLeaseAPI, FakeObserver, FakeOperator  # noqa: E402

EXPR = ('max by (namespace, pod, container) (\n'
        '  kube_pod_container_status_last_terminated_reason{namespace="liveprobe-target", reason="OOMKilled"}\n'
        ') > 0\n')


def alerts(duration, expr=EXPR, extra=""):
    return yaml_dump({"groups": [{"name": "liveprobe-target", "rules": [
        {"alert": "TargetWorkloadOOMKilled", "expr": expr, "for": duration,
         "labels": {"severity": "critical"}}] + ([{"alert": "Other", "expr": extra, "for": "1m"}]
                                                  if extra else [])}]})


def yaml_dump(value):
    return gate.yaml.safe_dump(value, sort_keys=False)


class GateFixture(unittest.TestCase):
    def setUp(self):
        image = "liveprobe/inventory@sha256:" + "a" * 64
        container = {"name": "inventory", "image": image, "resources": {"limits": {"memory": "512Mi"}},
                     "readinessProbe": {"httpGet": {"path": "/actuator/health/readiness", "port": 8080}}}
        self.dep = {"kind": "Deployment", "metadata": {"name": "inventory", "namespace": gate.TARGET},
                    "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": "inventory"}},
                                                          "spec": {"containers": [container]}}}}
        self.chaos = copy.deepcopy(self.dep)
        c = self.chaos["spec"]["template"]["spec"]["containers"][0]
        c["env"] = [{"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"},
                    {"name": "CHAOS_OPERATOR_TOKEN", "valueFrom": {"secretKeyRef": {
                        "name": "inventory-chaos-operator", "key": "token"}}},
                    {"name": "CHAOS_STATE_DIR", "value": "/var/run/liveprobe-chaos"}]
        c["volumeMounts"] = [{"name": "chaos-state", "mountPath": "/var/run/liveprobe-chaos"}]
        self.chaos["spec"]["template"]["spec"]["volumes"] = [
            {"name": "chaos-state", "emptyDir": {"sizeLimit": "1Mi"}}]
        self.local = {"kind": "ConfigMap", "metadata": {"name": "prometheus-config", "namespace": gate.OBS},
                      "data": {"alerts.yml": alerts("0s"), "prometheus.yml": "scrape_configs: []",
                               "recording.yml": "groups: []\n"}}

    def live_cm(self, duration="0s", **data):
        cm = copy.deepcopy(self.local)
        cm["metadata"].update(uid="cm-uid", resourceVersion="7")
        cm["data"]["alerts.yml"] = alerts(duration)
        cm["data"].update(data)
        return cm

    def scoped(self, cm):
        return gate.scoped_candidates((self.dep, cm), self.chaos, self.local)


class GateModeTests(GateFixture):
    def test_a_30s_live_is_patched_to_0s(self):
        cm = self.live_cm("30s")
        dep, new_cm = self.scoped(cm)
        self.assertEqual(new_cm["data"], self.local["data"])
        self.assertEqual(gate.oom_rule_mode(cm, new_cm), ("patch", "30s"))
        self.assertEqual(gate.profile(dep), "chaos")

    def test_b_already_0s_exact_contract_is_noop(self):
        cm = self.live_cm("0s")
        dep, new_cm = self.scoped(cm)
        self.assertEqual(new_cm, cm)
        self.assertIsNot(new_cm, cm)
        self.assertEqual(gate.oom_rule_mode(cm, new_cm), ("noop", "0s"))
        self.assertEqual(gate.profile(dep), "chaos")  # inventory candidate unaffected

    def test_b_rejects_0s_with_wrong_reason(self):
        cm = self.live_cm("0s")
        cm["data"]["alerts.yml"] = alerts("0s", EXPR.replace('reason="OOMKilled"', 'reason="Error"'))
        with self.assertRaisesRegex(gate.GateError, "contract mismatch"):
            self.scoped(cm)

    def test_b_rejects_0s_without_ksm_metric(self):
        cm = self.live_cm("0s")
        cm["data"]["alerts.yml"] = alerts("0s", 'up{reason="OOMKilled"} > 0\n')
        with self.assertRaisesRegex(gate.GateError, "contract mismatch"):
            self.scoped(cm)

    def test_b_rejects_0s_with_other_rule_drift(self):
        cm = self.live_cm("0s")
        cm["data"]["alerts.yml"] = alerts("0s", extra="vector(1)")
        with self.assertRaisesRegex(gate.GateError, "contract mismatch"):
            self.scoped(cm)

    def test_b_rejects_0s_with_other_config_key_drift(self):
        for key, value in (("prometheus.yml", "scrape_configs: [x]"), ("recording.yml", "groups: [x]\n")):
            with self.subTest(key=key):
                with self.assertRaisesRegex(gate.GateError, "contract mismatch"):
                    self.scoped(self.live_cm("0s", **{key: value}))

    def test_b_rejects_0s_semantic_equal_but_bytes_differ(self):
        cm = self.live_cm("0s")
        cm["data"]["alerts.yml"] = "# reformatted\n" + cm["data"]["alerts.yml"]
        with self.assertRaisesRegex(gate.GateError, "contract mismatch"):
            self.scoped(cm)

    def test_other_live_durations_fail_closed(self):
        for duration in ("5m", "1s", "0", None):
            with self.subTest(duration=duration):
                cm = self.live_cm("0s")
                cm["data"]["alerts.yml"] = alerts(duration)
                with self.assertRaises(gate.GateError):
                    self.scoped(cm)

    def test_local_manifest_not_0s_fails_closed_even_if_live_0s(self):
        self.local["data"]["alerts.yml"] = alerts("30s")
        with self.assertRaisesRegex(gate.GateError, "duration/selector"):
            self.scoped(self.live_cm("0s"))

    def test_missing_rule_fails_closed(self):
        cm = self.live_cm("0s")
        cm["data"]["alerts.yml"] = yaml_dump({"groups": [{"name": "g", "rules": []}]})
        with self.assertRaisesRegex(gate.GateError, "missing"):
            self.scoped(cm)

    def test_oom_rule_mode_rejects_ambiguous_plans(self):
        cm30, cm0 = self.live_cm("30s"), self.live_cm("0s")
        drift0 = self.live_cm("0s", **{"prometheus.yml": "scrape_configs: [x]"})
        for before, desired in ((cm0, cm30), (cm30, cm30), (cm0, drift0)):
            with self.subTest(before=gate.oom_rule_for(before), desired=gate.oom_rule_for(desired)):
                with self.assertRaises(gate.GateError):
                    gate.oom_rule_mode(before, desired)


class TransactionNoopTests(unittest.TestCase):
    """ConfigMap already desired: no ConfigMap CAS, restore touches only inventory."""
    def setUp(self):
        self.api = FakeLeaseAPI()
        self.lease = tx.KubernetesLease(self.api)
        self.operator = FakeOperator()
        self.operator.before[1]["data"]["alerts.yml"] = "for: 0s"
        self.operator.desired[1]["data"]["alerts.yml"] = "for: 0s"
        self.operator.objects = copy.deepcopy(self.operator.before)
        self.observer = FakeObserver()

    def run_tx(self):
        return tx.transaction(self.operator, self.observer, self.lease, lambda before, desired: None)

    def test_success_patches_only_inventory_both_ways(self):
        self.assertEqual(self.run_tx()["result"], "OFFLINE_TRANSACTION_VERIFIED")
        self.assertEqual([i for i, _ in self.operator.patches], [0, 0])  # apply + restore inventory
        self.assertEqual(self.operator.objects[1]["metadata"]["resourceVersion"], "1")
        self.assertEqual(self.operator.objects[1]["data"]["alerts.yml"], "for: 0s")
        self.assertEqual((self.operator.fault_count, self.operator.clear_count, self.api.deletes), (1, 1, 1))

    def test_observer_failure_restores_inventory_leaves_configmap(self):
        self.observer.proof = False
        with self.assertRaises(tx.UnsafeState):
            self.run_tx()
        self.assertEqual(self.operator.objects[0]["spec"]["template"]["profile"], None)
        self.assertEqual(self.operator.objects[1]["metadata"]["resourceVersion"], "1")
        self.assertTrue(all(i == 0 for i, _ in self.operator.patches))
        self.assertEqual(self.api.deletes, 1)

    def test_foreign_configmap_edit_during_noop_is_escalated(self):
        def foreign(report):
            self.operator.objects[1]["data"]["alerts.yml"] = "for: 5m"
            return True
        self.observer.observe = foreign
        with self.assertRaisesRegex(tx.UnsafeState, "rollback incomplete"):
            self.run_tx()
        self.assertEqual(self.api.deletes, 0)  # Lease retained

    def test_30s_path_unchanged_patches_both(self):
        self.setUp()
        op = FakeOperator()  # default fixture: 30s -> 0s
        self.operator = op
        self.run_tx()
        self.assertEqual([i for i, _ in op.patches], [0, 1, 1, 0])
        self.assertEqual(op.objects[1]["data"]["alerts.yml"], "for: 30s")


def cm_item(duration, rv="2"):
    return {"kind": "ConfigMap", "metadata": {"uid": "c", "resourceVersion": rv, "name": "prometheus-config",
                                              "namespace": tx.OBS},
            "data": {"alerts.yml": "groups:\n- name: g\n  rules:\n  - alert: TargetWorkloadOOMKilled\n"
                                   "    expr: x\n    for: " + duration + "\n"}}


def dep_item(x=1):
    return {"kind": "Deployment", "metadata": {"uid": "d", "resourceVersion": "1", "name": "inventory",
                                               "namespace": tx.TARGET}, "spec": {"template": {"x": x}}}


class LiveDryRunNoopTests(unittest.TestCase):
    def parts(self, before, desired, loaded, pre=None):
        api = API()
        self.api = api
        operator = SimpleNamespace(preflight=lambda: (before, desired, pre or {"pod_uid": "u"}),
                                   dry_run_verified=True)
        return {"authority": live.ExclusiveAuthority("t_aaaaaaaa", cards=lambda: ["t_aaaaaaaa"], jobs=lambda: []),
                "transport": adapter.KubernetesTransport("rancher-desktop", lambda _: api),
                "operator": operator, "signals": SimpleNamespace(rule_loaded=lambda d: d == loaded),
                "boundary": SimpleNamespace(faults_clear=lambda: True),
                "audit": SimpleNamespace(ready=lambda: True),
                "incidents": SimpleNamespace(list_incidents=lambda: {"items": []})}

    def dry(self, parts):
        with mock.patch.object(gate, "profile", return_value=None):
            return live.dry_run(parts)

    def test_noop_ready_and_no_configmap_cas(self):
        report = self.dry(self.parts([dep_item(1), cm_item("0s")], [dep_item(2), cm_item("0s")], "0s",
                                     {"pod_uid": "u", "oom_rule_mode": "noop"}))
        self.assertTrue(report["ready_for_live"])
        self.assertEqual((report["oom_rule_mode"], report["baseline_oom_rule_for"]), ("noop", "0s"))
        self.assertTrue(report["prometheus_rule_loaded_baseline"])
        self.assertEqual(report["cas"][1]["ops"], [])
        self.assertTrue(report["cas"][1]["noop"])
        self.assertEqual(report["cas"][1]["old_sha256"], report["cas"][1]["new_sha256"])
        self.assertEqual(report["cas"][0]["ops"][-1], "replace /spec/template")
        self.assertEqual((report["writes"], report["lease_created"], report["fault_post"]), (0, False, False))
        self.assertEqual({c[0] for c in self.api.calls}, {"lease_get"})

    def test_patch_mode_still_ready(self):
        report = self.dry(self.parts([dep_item(1), cm_item("30s")], [dep_item(2), cm_item("0s")], "30s"))
        self.assertTrue(report["ready_for_live"])
        self.assertEqual((report["oom_rule_mode"], report["baseline_oom_rule_for"]), ("patch", "30s"))
        self.assertEqual(report["cas"][1]["ops"][-1], "replace /data")

    def test_noop_not_ready_when_prometheus_has_not_loaded_0s(self):
        report = self.dry(self.parts([dep_item(1), cm_item("0s")], [dep_item(2), cm_item("0s")], "30s"))
        self.assertFalse(report["prometheus_rule_loaded_baseline"])
        self.assertFalse(report["ready_for_live"])

    def test_ambiguous_plan_stops(self):
        with self.assertRaises(gate.GateError):
            self.dry(self.parts([dep_item(1), cm_item("0s")], [dep_item(2), cm_item("30s")], "0s"))

    def test_gate_orchestrator_plan_mismatch_stops(self):
        with self.assertRaisesRegex(tx.UnsafeState, "plan mismatch"):
            self.dry(self.parts([dep_item(1), cm_item("0s")], [dep_item(2), cm_item("0s")], "0s",
                                {"pod_uid": "u", "oom_rule_mode": "patch"}))


class LiveOperatorNoopTests(unittest.TestCase):
    def test_server_normalize_skips_noop_configmap(self):
        before = (dep_item(1), cm_item("0s"))
        desired = (dep_item(2), cm_item("0s"))
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            if args[0] == "patch":
                out = copy.deepcopy(before[0])
                out["spec"]["template"] = {"x": 2}
                return json.dumps(out)
            raise AssertionError("unexpected kubectl call")
        op = live.LiveOperator(object(), None, None, run=run, health=object())
        with mock.patch.object(gate, "obj", side_effect=lambda ns, kind, name:
                               copy.deepcopy(before[0] if kind == "deployment" else before[1])):
            normalized = op.server_normalize(before, desired)
        self.assertEqual([a[1] for a in calls], ["deployment"])  # no ConfigMap dry-run patch
        self.assertEqual(normalized[1], desired[1])
        self.assertTrue(op.dry_run_verified)

    def test_verify_restored_uses_report_baseline_rule(self):
        op = live.LiveOperator(object(), None, None, run=lambda *a, **k: "{}", health=object())
        pod = {"metadata": {"labels": {"pod-template-hash": "h"}}}
        cm0 = cm_item("0s")
        cm0["metadata"]["uid"] = "p"
        def obj(ns, kind, name):
            if kind == "configmap":
                return copy.deepcopy(cm0)
            return {"kind": "Deployment", "metadata": {"uid": "p"}, "spec": {}}
        report = {"prometheus_uid": "p", "prometheus_spec": live.spec_hash({"kind": "Deployment", "spec": {}}),
                  "pod_template_hash": "h"}
        with mock.patch.object(adapter.PreparedOperator, "verify_restored"), \
                mock.patch.object(gate, "obj", side_effect=obj), \
                mock.patch.object(live, "spec_hash", side_effect=lambda item: "same"), \
                mock.patch.object(live, "inventory_pod", return_value=pod), \
                mock.patch.object(live, "pod_ready", return_value=True), \
                mock.patch.object(live, "pod_profile", return_value=None):
            report["prometheus_spec"] = "same"
            op.verify_restored([{"metadata": {"uid": "p"}}] * 2, dict(report, baseline_oom_rule_for="0s"))
            with self.assertRaisesRegex(tx.UnsafeState, "baseline OOM rule"):
                op.verify_restored([{"metadata": {"uid": "p"}}] * 2, report)  # legacy default 30s


class HealthRestoredBaselineTests(unittest.TestCase):
    def test_restored_checks_rule_at_report_baseline(self):
        seen = []
        health = adapter.ReadOnlyHealth(rule_loaded=lambda d: seen.append(d) or d == "0s",
                                        faults_clear=lambda: True)
        dep = {"spec": {"template": {"spec": {}}}}
        with mock.patch.object(gate, "obj", return_value=dep), \
                mock.patch.object(gate, "pod", return_value=({}, {"imageID": "x@sha256:1"})), \
                mock.patch.object(gate, "inventory_container", return_value={"image": "i@sha256:1"}), \
                mock.patch.object(gate, "deployment_ready", return_value=True), \
                mock.patch.object(gate, "profile", return_value=None), \
                mock.patch.object(gate, "node_gate"), mock.patch.object(gate, "baseline"):
            self.assertTrue(health.restored({"baseline_oom_rule_for": "0s"}))
            with self.assertRaises(gate.GateError):
                health.restored({})
        self.assertEqual(seen, ["0s", "30s"])


if __name__ == "__main__":
    unittest.main()
