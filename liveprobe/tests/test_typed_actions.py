"""KAN-135 (KAN-55/84/85): typed safe actions -- policy matrix, bounds, OCC, verification, rollback, lock."""
from __future__ import annotations

import copy
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from app import db
from app.actions import (TYPED, Denied, SafeActionKube, TargetConflict, data_hash, execute_typed, propose_typed,
                         stricter, typed_policy, validate)
from app.config import AppConfig, ApprovedConfigVersion
from app.models import Action, AuditLog, Incident, Setting
from app.safety import execute as safety_execute
from conftest import FakeKube, mk_pod, needs_db

NS = "liveprobe-target"
OWN = {"app.kubernetes.io/part-of": "liveprobe-target"}
ROOT = Path(__file__).resolve().parents[2]
V1 = "inventory-config-v1"
V1_DATA = {"INVENTORY_DB_POOL_MAX": "value-approved-five", "SERVER_PORT": "value-port-eighty"}
LIVE_BAD = {"INVENTORY_DB_POOL_MAX": "value-broken-one", "SERVER_PORT": "value-port-eighty"}
VALUE_MARK = "value-"


def cfg(**kw) -> AppConfig:
    c = AppConfig(allowlist=["rollout_undo", *TYPED])
    c.safe_actions.configmap_versions = {"inventory-config": [ApprovedConfigVersion(name=V1, sha256=data_hash(V1_DATA))]}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class Conflict(Exception):
    pass


def _ptr(path):
    return [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]


def json_patch(obj, ops):
    """Minimal RFC 6902 (test/add/replace/remove); failed test -> Conflict (API server 422)."""
    obj = copy.deepcopy(obj)
    for op in ops:
        parts = _ptr(op["path"])
        parent = obj
        for p in parts[:-1]:
            parent = parent[int(p)] if isinstance(parent, list) else parent.get(p)
            if parent is None:
                raise Conflict(op["path"])
        last = parts[-1]
        key = int(last) if isinstance(parent, list) else last
        if op["op"] == "test":
            try:
                cur = parent[key]
            except (KeyError, IndexError):
                raise Conflict(op["path"]) from None
            if cur != op["value"]:
                raise Conflict(op["path"])
        elif op["op"] in ("add", "replace"):
            if op["op"] == "replace" and (key not in parent if isinstance(parent, dict) else key >= len(parent)):
                raise Conflict(op["path"])
            parent[key] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            if key not in parent:
                raise Conflict(op["path"])
            del parent[key]
    return obj


def ready_dep(name, replicas=1, limit="512Mi", rv="100"):
    return {"metadata": {"name": name, "namespace": NS, "uid": f"uid-{name}", "resourceVersion": rv,
                         "generation": 3, "labels": {"app": name, **OWN},
                         "annotations": {"deployment.kubernetes.io/revision": "2"}},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name, **OWN}},
                                  "spec": {"containers": [{"name": name, "image": f"liveprobe/{name}:dev",
                                                           "resources": {"requests": {"memory": "256Mi"},
                                                                         "limits": {"memory": limit}}}]}}},
            "status": {"observedGeneration": 3, "replicas": replicas, "updatedReplicas": replicas,
                       "availableReplicas": replicas, "readyReplicas": replicas}}


class ExecKube(FakeKube):
    """In-memory API server double with resourceVersion OCC, delete preconditions and healthy reconcile."""
    typed_actions_enabled = True

    def __init__(self):
        super().__init__()
        self.configmaps: dict[str, dict] = {}
        self.writes: list[tuple] = []
        self.rv = 100
        self.healthy = True             # controller reconciles to Ready after a write
        self.fail_patch_after = None    # int: number of successful patches before raising
        self.raise_on_patch = None      # exception instance for the next patch
        self.lock = threading.Lock()
        for n in ("inventory", "order"):
            self.deployments[n] = ready_dep(n)
        self.replicasets = [{"metadata": {"name": "inventory-aaa", "ownerReferences": [
            {"kind": "Deployment", "name": "inventory", "controller": True}]}}]
        p = mk_pod("inventory-aaa-1", "inventory", "inventory-aaa")
        p["metadata"].update(uid="pod-uid-1", resourceVersion="50", labels={"app": "inventory", **OWN})
        self.pods = [p]
        self.configmaps["inventory-config"] = {"metadata": {"name": "inventory-config", "uid": "cm-uid",
                                                            "resourceVersion": "70",
                                                            "labels": {"app": "inventory", **OWN}},
                                               "data": dict(LIVE_BAD)}
        self.configmaps[V1] = {"metadata": {"name": V1, "uid": "cmv-uid", "resourceVersion": "5",
                                            "labels": {"app": "inventory", **OWN}},
                               "immutable": True, "data": dict(V1_DATA)}

    def _bump(self):
        self.rv += 1
        return str(self.rv)

    def get_configmap(self, ns, name):
        return copy.deepcopy(self.configmaps.get(name))

    def _reconcile(self, name):
        d = self.deployments[name]
        r = d["spec"]["replicas"]
        d["metadata"]["generation"] += 1
        st = d["status"]
        st["observedGeneration"] = d["metadata"]["generation"]
        if self.healthy:
            st.update(replicas=r, updatedReplicas=r, availableReplicas=r, readyReplicas=r)
        else:
            st.update(replicas=r, updatedReplicas=r, availableReplicas=0, readyReplicas=0)

    def patch_deployment_json(self, ns, name, ops):
        with self.lock:
            if self.raise_on_patch is not None:
                e, self.raise_on_patch = self.raise_on_patch, None
                raise e
            if self.fail_patch_after is not None:
                if self.fail_patch_after == 0:
                    raise RuntimeError("apiserver unavailable")
                self.fail_patch_after -= 1
            try:
                new = json_patch(self.deployments[name], ops)
            except Conflict as e:
                raise TargetConflict(str(e)) from None
            new["metadata"]["resourceVersion"] = self._bump()
            self.deployments[name] = new
            self.writes.append(("patch", "Deployment", name, ops))
            self._reconcile(name)
            if self.after_patch:
                self.after_patch(self)
            return copy.deepcopy(new)

    def patch_configmap_json(self, ns, name, ops):
        with self.lock:
            if self.fail_patch_after is not None:
                if self.fail_patch_after == 0:
                    raise RuntimeError("apiserver unavailable")
                self.fail_patch_after -= 1
            try:
                new = json_patch(self.configmaps[name], ops)
            except Conflict as e:
                raise TargetConflict(str(e)) from None
            new["metadata"]["resourceVersion"] = self._bump()
            self.configmaps[name] = new
            self.writes.append(("patch", "ConfigMap", name, ops))
            return copy.deepcopy(new)

    def delete_pod(self, ns, name, *, uid, resource_version):
        with self.lock:
            pod = next((p for p in self.pods if p["metadata"]["name"] == name), None)
            if not pod or pod["metadata"]["uid"] != uid or pod["metadata"]["resourceVersion"] != resource_version:
                raise TargetConflict("precondition failed")
            self.pods.remove(pod)
            self.writes.append(("delete", "Pod", name, uid))
            if self.healthy:
                np = copy.deepcopy(pod)
                np["metadata"].update(name=name + "-new", uid="pod-uid-2", resourceVersion=self._bump())
                self.pods.append(np)


# ------------------------------------------------------------------ pure policy / validation matrix
POSITIVE = [
    ("pod_delete", "Pod", "inventory-aaa-1", {}),
    ("rollout_restart", "Deployment", "inventory", {}),
    ("scale_replicas", "Deployment", "inventory", {"replicas": 2}),
    ("set_memory_limit", "Deployment", "inventory", {"memory_limit_mi": 640}),
    ("configmap_restore", "ConfigMap", "inventory-config", {"version": V1}),
]


@pytest.mark.parametrize("action_type,kind,name,params", POSITIVE)
def test_positive_validation_takes_sanitized_snapshot(action_type, kind, name, params):
    v = validate(cfg(), ExecKube(), action_type, NS, kind, name, params, TYPED[action_type].api_group)
    assert v.workload == "inventory" and v.lock_key == f"{NS}/workload/inventory"
    dumped = repr(v.snapshot)
    assert "resourceVersion" in dumped and VALUE_MARK not in dumped  # ConfigMap values never snapshotted
    assert "env" not in dumped


NEGATIVE = [
    ("delete_deployment", NS, "Deployment", "inventory", {}, "not a known"),
    ("scale_replicas", "liveprobe-system", "Deployment", "inventory", {"replicas": 2}, "target namespace"),
    ("scale_replicas", "kube-system", "Deployment", "coredns", {"replicas": 2}, "target namespace"),
    ("scale_replicas", NS, "StatefulSet", "inventory", {"replicas": 2}, "only supports kind"),
    ("rollout_restart", NS, "Deployment", "loadgen", {}, "not an allowlisted"),
    ("rollout_restart", NS, "Deployment", "Inventory;rm", {}, "DNS-1123"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": 0}, "outside approved bounds"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": 50}, "outside approved bounds"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": 1}, "no-op"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": "2"}, "must be int"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": True}, "must be int"),
    ("scale_replicas", NS, "Deployment", "inventory", {"replicas": 2, "image": "x"}, "exactly"),
    ("rollout_restart", NS, "Deployment", "inventory", {"patch": {"spec": {}}}, "exactly"),
    ("set_memory_limit", NS, "Deployment", "inventory", {"memory_limit_mi": 64}, "outside approved bounds"),
    ("set_memory_limit", NS, "Deployment", "inventory", {"memory_limit_mi": 4096}, "outside approved bounds"),
    ("configmap_restore", NS, "ConfigMap", "inventory-config", {"version": "inventory-config-v9"}, "not an approved"),
    ("configmap_restore", NS, "ConfigMap", "order-config", {"version": V1}, "no approved immutable version history"),
    ("configmap_restore", NS, "Secret", "inventory-db", {"version": V1}, "only supports kind"),
    ("pod_delete", NS, "Pod", "ghost", {}, "does not exist"),
    ("pod_delete", NS, "Node", "lima-rancher-desktop", {}, "only supports kind"),
]


@pytest.mark.parametrize("action_type,ns,kind,name,params,match", NEGATIVE)
def test_negative_matrix_denied(action_type, ns, kind, name, params, match):
    k = ExecKube()
    with pytest.raises(Denied, match=match):
        validate(cfg(), k, action_type, ns, kind, name, params)
    assert not k.writes


def test_not_allowlisted_and_wrong_api_group_denied():
    with pytest.raises(Denied, match="allowlist"):
        validate(AppConfig(), ExecKube(), "configmap_restore", NS, "ConfigMap", "inventory-config", {"version": "v1"})
    with pytest.raises(Denied, match="API group"):
        validate(cfg(), ExecKube(), "rollout_restart", NS, "Deployment", "inventory", {}, "extensions")


def test_ownership_labels_required():
    k = ExecKube()
    del k.deployments["inventory"]["metadata"]["labels"]["app.kubernetes.io/part-of"]
    with pytest.raises(Denied, match="ownership labels"):
        validate(cfg(), k, "rollout_restart", NS, "Deployment", "inventory", {})
    k = ExecKube()
    k.pods[0]["metadata"]["labels"] = {"app": "inventory"}
    with pytest.raises(Denied, match="ownership labels"):
        validate(cfg(), k, "pod_delete", NS, "Pod", "inventory-aaa-1", {})


def test_bare_or_terminating_pod_denied():
    k = ExecKube()
    k.pods[0]["metadata"]["ownerReferences"] = []
    with pytest.raises(Denied, match="controller-owned"):
        validate(cfg(), k, "pod_delete", NS, "Pod", "inventory-aaa-1", {})
    k = ExecKube()
    k.pods[0]["metadata"]["deletionTimestamp"] = "2026-09-29T00:00:00Z"
    with pytest.raises(Denied, match="terminating"):
        validate(cfg(), k, "pod_delete", NS, "Pod", "inventory-aaa-1", {})


def test_unknown_memory_units_and_unbounded_current_denied():
    k = ExecKube()
    k.deployments["inventory"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "536870912"
    with pytest.raises(Denied, match="unknown units"):
        validate(cfg(), k, "set_memory_limit", NS, "Deployment", "inventory", {"memory_limit_mi": 640})
    k = ExecKube()
    del k.deployments["inventory"]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    with pytest.raises(Denied, match="unset"):
        validate(cfg(), k, "set_memory_limit", NS, "Deployment", "inventory", {"memory_limit_mi": 640})
    k = ExecKube()
    k.deployments["inventory"]["spec"]["replicas"] = 5
    with pytest.raises(Denied, match="current replicas"):
        validate(cfg(), k, "scale_replicas", NS, "Deployment", "inventory", {"replicas": 2})
    k = ExecKube()
    k.deployments["inventory"]["spec"]["template"]["spec"]["containers"].append({"name": "sidecar"})
    with pytest.raises(Denied, match="container layout"):
        validate(cfg(), k, "set_memory_limit", NS, "Deployment", "inventory", {"memory_limit_mi": 640})


@pytest.mark.parametrize("mutate,match", [
    (lambda k: k.configmaps.pop(V1), "does not exist; refusing to reconstruct"),
    (lambda k: k.configmaps[V1].pop("immutable"), "not immutable"),
    (lambda k: k.configmaps[V1]["data"].update(SERVER_PORT="value-tampered"), "hash does not match"),
    (lambda k: k.configmaps["inventory-config"].update(data=dict(V1_DATA)), "no-op"),
    (lambda k: k.configmaps["inventory-config"].update(immutable=True), "immutable"),
    (lambda k: k.configmaps["inventory-config"].update(binaryData={"x": "AA=="}), "binaryData"),
])
def test_configmap_version_integrity(mutate, match):
    k = ExecKube()
    mutate(k)
    with pytest.raises(Denied, match=match):
        validate(cfg(), k, "configmap_restore", NS, "ConfigMap", "inventory-config", {"version": V1})


def test_configmap_restore_without_history_denied_by_default():
    with pytest.raises(Denied, match="no approved immutable version history"):
        validate(AppConfig(allowlist=list(TYPED)), ExecKube(), "configmap_restore", NS, "ConfigMap",
                 "inventory-config", {"version": V1})


def test_policy_only_tightens():
    assert stricter("auto", "approval") == "approval"
    assert stricter("approval", "auto") == "approval"
    assert stricter("auto", "bogus") == "bogus"  # unknown fails closed (ranked strictest)
    # policy C: per-type floor is auto; the strictness comes from level policy, global/incident modes and the bounds
    assert all(TYPED[t].min_policy == "auto" for t in TYPED)
    assert stricter(TYPED["set_memory_limit"].min_policy, "approval") == "approval"   # a global approval mode still wins


# ------------------------------------------------------------------ DB-backed execution
def mk_incident(s, level="SIMPLE", workload="inventory"):
    i = Incident(fingerprint="f", namespace=NS, workload=workload, severity_level=level, title="t",
                 status="open", signal_reasons=[])
    s.add(i)
    s.flush()
    return i


def events(s, aid):
    return [a.event for a in s.query(AuditLog).order_by(AuditLog.id)
            if (a.detail or {}).get("action_id") == aid]


def approve(aid):
    with db.session() as s:
        a = s.get(Action, aid)
        db.audit(s, "human", "action_approved", a.incident_id, action_id=aid)


def run(k, aid, c=None):
    return execute_typed(db.session, c or cfg(), k, aid, "test", sleep=lambda _: None, verify_timeout_s=0,
                         now=lambda: "2026-09-29T12:00:00Z")


@pytest.mark.parametrize("action_type,kind,name,params", POSITIVE)
@needs_db
def test_six_action_matrix_positive_with_policy(db_url, action_type, kind, name, params):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        act = propose_typed(s, cfg(), k, inc, "SIMPLE", action_type, kind, name, params)
        aid, status = act.id, act.status
    expected = "proposed" if TYPED[action_type].min_policy == "auto" else "pending_approval"
    assert status == expected
    if status == "pending_approval":
        assert run(k, aid).status == "denied" and not k.writes   # no approval -> no write
        with db.session() as s:
            assert s.get(Action, aid).status == "denied"
        # re-propose and approve
        with db.session() as s:
            inc = s.get(Incident, s.get(Action, aid).incident_id)
            aid = propose_typed(s, cfg(), k, inc, "SIMPLE", action_type, kind, name, params).id
        approve(aid)
    r = run(k, aid)
    assert r.status == "succeeded", r.reason
    assert len(k.writes) == (2 if action_type == "configmap_restore" else 1)
    with db.session() as s:
        a = s.get(Action, aid)
        assert a.executed and a.before_snapshot and a.after_state["checks"]
        assert all(a.after_state["checks"].values())
        ev = events(s, aid)
        assert ev[-3:] == ["action_executing", "action_applied", "action_verified"]
        assert VALUE_MARK not in repr(a.before_snapshot) + repr(a.params) + repr(a.after_state)
        assert VALUE_MARK not in repr([x.detail for x in s.query(AuditLog)])


@pytest.mark.parametrize("level", ["HIGH_RISK"])
@pytest.mark.parametrize("action_type,kind,name,params", POSITIVE)
@needs_db
def test_high_risk_never_executes_even_if_approved(db_url, level, action_type, kind, name, params):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s, level=level)
        act = propose_typed(s, cfg(), k, inc, level, action_type, kind, name, params)
        aid = act.id
        assert act.status == "denied" and "HIGH_RISK" in act.reason
        # force a forged pending/approved state; the executor must still refuse
        act.status, act.policy_mode = "pending_approval", "approval"
    approve(aid)
    assert run(k, aid).status == "denied" and not k.writes
    with db.session() as s:
        assert "action_denied" in events(s, aid)


@needs_db
def test_global_and_incident_overrides_only_tighten(db_url):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        s.add(Setting(key=f"incident_policy_mode:{inc.id}", value={"mode": "approval"}))
        s.flush()
        assert typed_policy(s, "SIMPLE", "rollout_restart", inc.id) == "approval"
        act = propose_typed(s, cfg(), k, inc, "SIMPLE", "rollout_restart", "Deployment", "inventory")
        assert act.status == "pending_approval"
        s.get(Setting, f"incident_policy_mode:{inc.id}").value = {"mode": "suggest_only"}
        s.add(Setting(key="policy_mode", value={"mode": "auto"}))
        aid = act.id
    approve(aid)
    r = run(k, aid)   # policy tightened after approval -> refused at pre-execution
    assert r.status == "denied" and "policy" in r.reason and not k.writes


@needs_db
def test_forged_proposed_row_is_reevaluated_at_execution(db_url):
    """Policy C: a row cannot be auto-run just because it says so. Execution re-derives the policy from the incident's
    CURRENT level and global mode, so a tampered `auto` row on a HIGH_RISK incident (or under a global approval mode) is denied."""
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        act = propose_typed(s, cfg(), k, inc, "SIMPLE", "scale_replicas", "Deployment", "inventory", {"replicas": 2})
        act.status, act.policy_mode = "proposed", "auto"   # tampered row
        inc.severity_level = "HIGH_RISK"                   # the incident is (now) high risk
        aid = act.id
    assert run(k, aid).status == "denied" and not k.writes


@needs_db
def test_forged_auto_row_is_denied_under_global_approval_mode(db_url):
    from app.models import Setting
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        act = propose_typed(s, cfg(), k, inc, "SIMPLE", "set_memory_limit", "Deployment", "inventory", {"memory_limit_mi": 768})
        act.status, act.policy_mode = "proposed", "auto"
        s.merge(Setting(key="policy_mode", value={"mode": "approval"}))
        aid = act.id
    assert run(k, aid).status == "denied" and not k.writes


@needs_db
def test_rejections_at_proposal_are_audited(db_url):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        bad = [propose_typed(s, cfg(), k, inc, "SIMPLE", "exec_shell", "Pod", "inventory-aaa-1"),
               propose_typed(s, cfg(), k, inc, "SIMPLE", "scale_replicas", "Deployment", "inventory", {"replicas": 9}),
               propose_typed(s, cfg(), k, inc, "SIMPLE", "rollout_restart", "Deployment", "order"),  # other workload
               propose_typed(s, cfg(), k, inc, "SIMPLE", "configmap_restore", "ConfigMap", "inventory-config",
                             {"version": "made-up"})]
        for a in bad:
            assert a.status == "denied"
            assert events(s, a.id) == ["action_proposed", "action_denied"]
    assert not k.writes


@needs_db
def test_observe_only_client_is_refused_and_audited(db_url):
    k = ExecKube()
    k.typed_actions_enabled = False
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "rollout_restart", "Deployment", "inventory").id
    r = run(k, aid)
    assert r.status == "denied" and "observe-only" in r.reason and not k.writes


@needs_db
def test_target_changed_between_proposal_and_execution(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "scale_replicas", "Deployment", "inventory",
                            {"replicas": 2}).id
    approve(aid)
    k.deployments["inventory"]["metadata"]["generation"] = 9   # someone else rolled it
    r = run(k, aid)
    assert r.status == "denied" and "changed since proposal" in r.reason and not k.writes


@needs_db
def test_resource_version_conflict_after_snapshot_fails_closed(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "rollout_restart", "Deployment", "inventory").id
    orig_validate_get = k.get_deployment
    calls = {"n": 0}

    def racing_get(ns, name):
        calls["n"] += 1
        d = orig_validate_get(ns, name)
        if calls["n"] == 1:  # first read = pre-execution snapshot; then a concurrent writer bumps rv
            k.deployments[name]["metadata"]["resourceVersion"] = "999"
        return d
    k.get_deployment = racing_get
    r = run(k, aid)
    assert r.status == "failed" and "conflict" in r.reason and not k.writes
    with db.session() as s:
        assert s.get(Action, aid).executed is False
        assert "action_conflict" in events(s, aid)


@needs_db
def test_pod_delete_precondition_conflict(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "pod_delete", "Pod", "inventory-aaa-1").id
    real = k.delete_pod

    def stale(ns, name, *, uid, resource_version):
        k.pods[0]["metadata"]["resourceVersion"] = "51"
        return real(ns, name, uid=uid, resource_version=resource_version)
    k.delete_pod = stale
    r = run(k, aid)
    assert r.status == "failed" and "conflict" in r.reason and not k.writes


@pytest.mark.parametrize("action_type,kind,name,params", POSITIVE[1:])
@needs_db
def test_verification_failure_rolls_back_and_verifies(db_url, action_type, kind, name, params):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", action_type, kind, name, params).id
    approve(aid)
    before = copy.deepcopy((k.deployments["inventory"]["spec"], k.configmaps["inventory-config"]["data"]))
    k.healthy = False

    def heal_on_compensation(fake):  # the compensating write brings the workload back
        if len(fake.writes) >= (3 if action_type == "configmap_restore" else 2):
            fake.healthy = True
            fake._reconcile("inventory")
    k.after_patch = heal_on_compensation
    r = run(k, aid)
    assert r.status == "rolled_back", r.reason
    spec, data = k.deployments["inventory"]["spec"], k.configmaps["inventory-config"]["data"]
    assert spec["replicas"] == before[0]["replicas"]
    c0 = spec["template"]["spec"]["containers"][0]
    assert c0["resources"] == before[0]["template"]["spec"]["containers"][0]["resources"]
    assert data == before[1]
    if action_type == "rollout_restart":
        assert "kubectl.kubernetes.io/restartedAt" not in (spec["template"]["metadata"].get("annotations") or {})
    with db.session() as s:
        ev = events(s, aid)
        assert "action_verification_failed" in ev and ev[-1] == "action_rolled_back"


@pytest.mark.parametrize("action_type,kind,name,params", POSITIVE[1:])
@needs_db
def test_rollback_failure_is_indeterminate_and_keeps_lock(db_url, action_type, kind, name, params):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        aid = propose_typed(s, cfg(), k, inc, "SIMPLE", action_type, kind, name, params).id
        iid = inc.id
    approve(aid)
    k.healthy = False
    k.fail_patch_after = 2 if action_type == "configmap_restore" else 1   # forward write(s) ok, compensation fails
    r = run(k, aid)
    assert r.status == "indeterminate" and "indeterminate" in r.reason
    with db.session() as s:
        assert events(s, aid)[-1] == "action_rollback_failed"
        # the workload stays locked: a second action on the same workload is refused
        inc = s.get(Incident, iid)
        aid2 = propose_typed(s, cfg(), ExecKube(), inc, "SIMPLE", "rollout_restart", "Deployment", "inventory").id
    k2 = ExecKube()
    r2 = run(k2, aid2)
    assert r2.status == "denied" and "already executing" in r2.reason and not k2.writes


@needs_db
def test_pod_delete_verification_failure_is_not_claimed_success(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "pod_delete", "Pod", "inventory-aaa-1").id
    k.healthy = False
    k.deployments["inventory"]["status"]["availableReplicas"] = 0
    r = run(k, aid)
    assert r.status == "failed" and "not reversible" in r.reason
    with db.session() as s:
        assert events(s, aid)[-1] == "action_rollback_not_applicable"


@needs_db
def test_unknown_write_outcome_is_indeterminate(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "rollout_restart", "Deployment", "inventory").id
    k.raise_on_patch = TimeoutError("read timeout")
    r = run(k, aid)
    assert r.status == "indeterminate"
    with db.session() as s:
        assert "action_indeterminate" in events(s, aid)


@needs_db
def test_workload_lock_blocks_concurrent_actions_on_different_objects(db_url):
    """pod delete and rollout restart touch different objects of one workload -> only one may execute."""
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        a1 = propose_typed(s, cfg(), k, inc, "SIMPLE", "pod_delete", "Pod", "inventory-aaa-1").id
        a2 = propose_typed(s, cfg(), k, inc, "SIMPLE", "rollout_restart", "Deployment", "inventory").id
        s.get(Action, a1).status = "executing"   # a1 holds the workload lock (e.g. still verifying)
    r2 = run(k, a2)
    assert r2.status == "denied" and "already executing" in r2.reason and not k.writes
    with db.session() as s:
        assert "action_denied" in events(s, a2)


@needs_db
def test_parallel_execution_single_mutation(db_url):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        ids = [propose_typed(s, cfg(), k, inc, "SIMPLE", "rollout_restart", "Deployment", "inventory").id
               for _ in range(4)]
    barrier = threading.Barrier(len(ids))
    real_get = k.get_deployment

    def slow_get(ns, name):
        return real_get(ns, name)
    k.get_deployment = slow_get
    results = []

    def go(aid):
        barrier.wait()
        results.append(run(k, aid))
    ts = [threading.Thread(target=go, args=(a,)) for a in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # at most one mutation reached the (fake) API server; others denied or OCC-conflicted
    assert len(k.writes) <= 1
    statuses = sorted(r.status for r in results)
    assert statuses.count("succeeded") <= 1
    assert all(st in ("succeeded", "denied", "failed") for st in statuses)


@needs_db
def test_rollout_undo_and_typed_action_share_workload_lock(db_url, monkeypatch):
    k = ExecKube()
    with db.session() as s:
        inc = mk_incident(s)
        s.add(Action(incident_id=inc.id, type="rollout_undo", target=f"{NS}/Deployment/inventory", params={},
                     policy_mode="auto", status="executing", lock_key=f"{NS}/workload/inventory"))
        aid = propose_typed(s, cfg(), k, inc, "SIMPLE", "pod_delete", "Pod", "inventory-aaa-1").id
    r = safety_execute(db.session, cfg(), k, aid, "test")   # dispatch through the existing entry point
    assert r.status == "denied" and "already executing" in r.reason and not k.writes


@needs_db
def test_executed_action_cannot_be_replayed(db_url):
    k = ExecKube()
    with db.session() as s:
        aid = propose_typed(s, cfg(), k, mk_incident(s), "SIMPLE", "rollout_restart", "Deployment", "inventory").id
    assert run(k, aid).status == "succeeded"
    n = len(k.writes)
    assert run(k, aid).status == "denied" and len(k.writes) == n


# ------------------------------------------------------------------ executor client guard (no network)
def test_safe_action_kube_refuses_non_allowlisted_objects():
    class Api:
        def __getattr__(self, n):
            raise AssertionError(f"must not call {n}")
    c = SafeActionKube(object(), Api(), Api(), Exception)
    with pytest.raises(Denied):
        c.patch_deployment_json("liveprobe-system", "liveprobe-api", [])
    with pytest.raises(Denied):
        c.patch_deployment_json(NS, "loadgen", [])
    with pytest.raises(Denied):
        c.patch_configmap_json(NS, "inventory-db", [])
    with pytest.raises(Denied):
        c.delete_pod("kube-system", "x", uid="u", resource_version="1")
    assert not hasattr(c, "read_namespaced_secret")


def test_safe_action_kube_maps_conflict():
    class ApiErr(Exception):
        def __init__(self, status):
            self.status = status

    class Apps:
        def patch_namespaced_deployment(self, *a):
            raise ApiErr(422)
    c = SafeActionKube(object(), object(), Apps(), ApiErr)
    with pytest.raises(TargetConflict):
        c.patch_deployment_json(NS, "inventory", [])


# ------------------------------------------------------------------ RBAC render check (no apply)
def _render(path):
    out = subprocess.run(["kubectl", "kustomize", str(path)], check=True, capture_output=True, text=True).stdout
    return [o for o in yaml.safe_load_all(out) if o]


def test_executor_rbac_is_least_privilege_and_not_in_base():
    objs = _render(ROOT / "deploy/optional/safe-action-executor")
    roles = {o["metadata"]["name"]: o for o in objs if o["kind"] == "Role"}
    assert set(roles) == {"safe-action-pod-delete", "safe-action-deployment-patch", "safe-action-configmap-restore"}
    assert not [o for o in objs if o["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    for r in roles.values():
        assert r["metadata"]["namespace"] == NS
        for rule in r["rules"]:
            assert not set(rule["verbs"]) & {"*", "create", "update", "deletecollection", "list", "watch", "escalate",
                                              "bind", "impersonate"}
            assert not set(rule["resources"]) & {"*", "secrets", "nodes", "pods/exec", "pods/log",
                                                  "serviceaccounts/token", "deployments/scale", "roles", "rolebindings"}
    dep = roles["safe-action-deployment-patch"]["rules"]
    assert dep == [{"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["inventory", "order"],
                    "verbs": ["get", "patch"]}]
    cm_patch = [r for r in roles["safe-action-configmap-restore"]["rules"] if "patch" in r["verbs"]]
    assert cm_patch == [{"apiGroups": [""], "resources": ["configmaps"],
                         "resourceNames": ["inventory-config", "order-config"], "verbs": ["patch"]}]
    for b in (o for o in objs if o["kind"] == "RoleBinding"):
        assert b["subjects"] == [{"kind": "ServiceAccount", "namespace": "liveprobe-system",
                                  "name": "safe-action-executor"}]
    vap = next(o for o in objs if o["kind"] == "ValidatingAdmissionPolicy")
    assert vap["spec"]["failurePolicy"] == "Fail"
    base = _render(ROOT / "deploy/base")
    assert not any(o["metadata"].get("name", "").startswith("safe-action") for o in base)
    # P14 permits exactly one ConfigMap read binding; all writes remain forbidden.
    bindings = [o for o in base if o["kind"] == "RoleBinding"
                and any(sj.get("name") == "liveprobe" for sj in o.get("subjects", []))]
    assert [b["metadata"]["name"] for b in bindings] == ["liveprobe-config-reader"]
    binding = bindings[0]
    assert binding["metadata"]["namespace"] == NS and binding["roleRef"]["kind"] == "Role"
    reader = next(o for o in base if o["kind"] == "Role" and o["metadata"]["namespace"] == NS
                  and o["metadata"]["name"] == binding["roleRef"]["name"])
    assert reader["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list"]}]
