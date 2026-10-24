"""P15 보안 회귀: 실제 PostgreSQL과 합성 Kubernetes 경계만 사용한다."""
from __future__ import annotations

import copy
import json

import pytest

from app import commands
from conftest import db_url  # noqa: F401  (fixture)
from test_p15_commands import cm, operation, planned, setup, workload
from test_p15_operator_kube import http_api  # noqa: F401  (fixture)


def manifest_operation(obj):
    return {"op": "apply", "apiVersion": obj["apiVersion"], "kind": obj["kind"],
            "namespace": obj["metadata"].get("namespace"), "name": obj["metadata"]["name"], "manifest": obj}


def storage_workload(source=None):
    obj = workload()
    pod = obj["spec"]["template"]["spec"]
    pod["volumes"] = [{"name": "capture", **(source or {"persistentVolumeClaim": {"claimName": "existing-local"}})}]
    pod["containers"][0]["volumeMounts"] = [{"name": "capture", "mountPath": "/capture", "readOnly": True}]
    return obj


@pytest.mark.parametrize("source", [
    {"local": {"path": "/var/lib/kubelet/pods/synthetic/volumes/kubernetes.io~secret/db"}},
    {"nfs": {"server": "synthetic", "path": "/private"}},
    {"csi": {"driver": "synthetic.csi.example", "volumeHandle": "private"}},
])
def test_unreviewed_pv_source_is_rejected(source):
    obj = {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "capture"},
           "spec": {"capacity": {"storage": "1Gi"}, "accessModes": ["ReadWriteOnce"], **source}}
    with pytest.raises(commands.CommandError, match="unreviewed_storage_resource"):
        commands.validate_operation(manifest_operation(obj))


def test_new_pvc_binding_is_rejected():
    obj = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"namespace": "sandbox", "name": "capture"},
           "spec": {"volumeName": "existing-local", "storageClassName": "", "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Gi"}}}}
    with pytest.raises(commands.CommandError, match="unreviewed_storage_resource"):
        commands.validate_operation(manifest_operation(obj))


@pytest.mark.parametrize("kind", ["Pod", "Deployment"])
@pytest.mark.parametrize("source", [
    {"persistentVolumeClaim": {"claimName": "existing-local"}},
    {"nfs": {"server": "synthetic", "path": "/private"}},
    {"csi": {"driver": "synthetic.csi.example"}},
    {"ephemeral": {"volumeClaimTemplate": {"spec": {"storageClassName": "unreviewed"}}}},
])
def test_new_workload_cannot_mount_unreviewed_storage(source, kind):
    obj = commands.content(storage_workload(source))
    assert obj is not None
    if kind == "Pod":
        obj = {"apiVersion": "v1", "kind": "Pod", "metadata": obj["metadata"], "spec": obj["spec"]["template"]["spec"]}
    with pytest.raises(commands.CommandError, match="unreviewed_storage_access"):
        commands.validate_delta(manifest_operation(obj), None, obj)


@pytest.mark.parametrize("change", ["image", "command", "mount", "claim"])
def test_existing_pvc_workload_cannot_change_execution_or_access(change):
    before = storage_workload()
    after = copy.deepcopy(before)
    pod = after["spec"]["template"]["spec"]
    if change == "image":
        pod["containers"][0]["image"] = "reader:v2"
    elif change == "command":
        pod["containers"][0]["command"] = ["cat", "/capture/private"]
    elif change == "mount":
        pod["containers"][0]["volumeMounts"][0]["mountPath"] = "/private"
    else:
        pod["volumes"][0]["persistentVolumeClaim"]["claimName"] = "another-local"
    op = operation(apiVersion="apps/v1", kind="Deployment", name="web", patch={"spec": after["spec"]})
    with pytest.raises(commands.CommandError, match="unreviewed_storage_access"):
        commands.validate_delta(op, before, after)


def test_adding_existing_pvc_to_existing_workload_is_rejected():
    before, after = workload(), storage_workload()
    op = operation(apiVersion="apps/v1", kind="Deployment", name="web", patch={"spec": after["spec"]})
    with pytest.raises(commands.CommandError, match="unreviewed_storage_access"):
        commands.validate_delta(op, before, after)


@pytest.mark.parametrize("change", ["memory", "cpu", "scale", "restart"])
def test_existing_pvc_resource_only_changes_execute_and_restore(setup, change):
    x = setup
    obj = storage_workload()
    obj["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "100m"
    x.kube.objects = {x.kube.key(obj): copy.deepcopy(obj)}
    if change in ("memory", "cpu"):
        op = operation(apiVersion="apps/v1", kind="Deployment", name="web", patch=[{
            "op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/" + change,
            "value": "256Mi" if change == "memory" else "200m"}])
    else:
        op = operation(change, apiVersion="apps/v1", kind="Deployment", name="web", **({"replicas": 2} if change == "scale" else {}))
    x.planner.operations = [op]
    c = planned(x)
    assert c["status"] == "awaiting_confirmation", c["error"]
    x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.execute(c["id"])["status"] == "succeeded"
    assert x.service.rollback(c["id"], c["plan_sha256"])["status"] == "rolled_back"
    assert commands.content(x.kube.get(op)) == commands.content(obj)


def test_new_stateful_claim_template_is_rejected():
    obj = commands.content(workload())
    assert obj is not None
    obj["kind"] = "StatefulSet"
    obj["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "capture"}, "spec": {"storageClassName": "private"}}]
    with pytest.raises(commands.CommandError, match="unreviewed_storage_access"):
        commands.validate_delta(manifest_operation(obj), None, obj)


def test_new_emptydir_and_configmap_volumes_remain_allowed():
    obj = commands.content(storage_workload({"emptyDir": {}}))
    assert obj is not None
    obj["spec"]["template"]["spec"]["volumes"].append({"name": "settings", "configMap": {"name": "settings"}})
    assert commands.validate_delta(manifest_operation(obj), None, obj)


@pytest.mark.parametrize("parameters", [{}, {"type": "plain"}, {
    "csi.storage.k8s.io/node-publish-secret-name": "private",
    "csi.storage.k8s.io/node-publish-secret-namespace": "sandbox"}])
def test_storageclass_without_reviewed_provisioner_policy_is_rejected(parameters):
    obj = {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": "capture"},
           "provisioner": "synthetic.csi.example", "parameters": parameters}
    with pytest.raises(commands.CommandError, match="unreviewed_storage_resource"):
        commands.validate_operation(manifest_operation(obj))


@pytest.mark.parametrize("phase", ["provisioner", "controller-publish", "controller-expand", "node-publish", "node-stage", "node-expand"])
@pytest.mark.parametrize("field", ["name", "namespace"])
def test_csi_secret_parameter_keys_are_detected_in_nested_maps(phase, field):
    key = f"csi.storage.k8s.io/{phase}-secret-{field}"
    assert commands.unsafe_fragment({"parameters": {"nested": [{key: "private"}]}})


@pytest.mark.parametrize("key", ["nodeExpandSecretRef", "controllerExpandSecretRef", "node_publish_secret_ref", "NODE-STAGE-SECRET-REF"])
def test_normalized_csi_reference_keys_remain_forbidden(key):
    assert commands.unsafe_fragment({"spec": {"nested": [{key: {"name": "private", "namespace": "sandbox"}}]}})


def test_ordinary_configmap_token_text_is_not_a_secret_reference():
    op = operation(patch={"data": {"tokenizer": "token", "note": "node-publish-secret-name is documentation", "local": "ordinary"}})
    commands.validate_operation(op)
    assert commands.validate_delta(op, cm(), commands.materialize(op, cm()))


@pytest.mark.parametrize("typ", ["scale", "patch"])
@pytest.mark.parametrize("previous,desired,downtime", [(1, 0, "expected"), (3, 1, "possible")])
def test_replica_reduction_discloses_stop_separately_from_restart(typ, previous, desired, downtime):
    before = workload()
    before["spec"]["replicas"] = previous
    op = operation(typ, apiVersion="apps/v1", kind="Deployment", name="web",
                   **({"replicas": desired} if typ == "scale" else {"patch": {"spec": {"replicas": desired}}}))
    delta = commands.validate_delta(op, before, commands.materialize(op, before))
    result = commands.effects([op], [{"changes": delta, "created": False}])
    assert result["downtime"] == downtime
    assert result["restarts"] is False
    explanation = " ".join(result["items_ko"] + result["warnings_ko"])
    assert "sandbox/Deployment/web" in explanation
    assert f"{previous} → {desired}" in explanation
    assert "중단" in explanation


@pytest.mark.parametrize("op", [
    operation(),
    operation(kind="Service", patch={"spec": {"selector": {"app": "new"}}}),
    operation(kind="NetworkPolicy", apiVersion="networking.k8s.io/v1", patch={"spec": {"policyTypes": ["Ingress"]}}),
    operation("cordon", kind="Node", name="worker", namespace=None),
    operation("secret_update", kind="Secret", keys=["PASSWORD"]),
])
def test_unknown_configuration_impact_never_claims_no_downtime(op):
    result = commands.effects([op], [{"changes": [], "created": False}])
    assert result["downtime"] == "possible"
    assert any("서비스" in warning and "검증" in warning for warning in result["warnings_ko"])


def test_scale_to_zero_warning_is_persisted_before_confirmation(setup):
    x = setup
    obj = workload()
    x.kube.objects = {x.kube.key(obj): obj}
    x.planner.operations = [operation("scale", apiVersion="apps/v1", kind="Deployment", name="web", replicas=0)]
    result = planned(x)
    assert result["side_effects"]["downtime"] == "expected"
    assert result["side_effects"]["restarts"] is False
    assert result["status"] == "awaiting_confirmation"
    assert not any(not dry for _, dry in x.kube.calls)


@pytest.mark.parametrize("extra", [{"unexpected": "UNAPPROVED_DRIFT_SENTINEL"}, {
    "nested": {"secretRef": {"name": "UNAPPROVED_DRIFT_SENTINEL"}}}])
def test_live_admission_drift_is_persisted_without_unapproved_content(setup, extra):
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    original = x.kube.mutate
    injected = False

    def mutate(op, before, after, *, dry_run=False):
        nonlocal injected
        if not dry_run and not injected:
            injected = True
            after = copy.deepcopy(after)
            after["data"].update(extra)
        return original(op, before, after, dry_run=dry_run)

    x.kube.mutate = mutate
    result = x.service.execute(c["id"])
    assert result["status"] == "failed"
    assert result["error"] == "execution_admission_drift"
    # 새 서비스/DB 세션에서도 값 없는 drift 기록과 실행 소유권이 유지되어야 한다.
    reloaded = commands.CommandService(x.service.cfg, x.planner, x.kube)
    persisted = reloaded.get(c["id"])
    receipt = persisted["result"]["operations"][0]
    assert receipt["admission_drift"] == "detected"
    assert receipt["verified"] is False and persisted["result"]["claimed"] is True
    assert "UNAPPROVED_DRIFT_SENTINEL" not in json.dumps(persisted)
    with pytest.raises(commands.CommandError, match="command_not_executable"):
        reloaded.execute(c["id"])
    rollback = reloaded.rollback(c["id"], c["plan_sha256"])
    assert rollback["status"] == "failed"
    assert rollback["error"] == "rollback_unreviewed_admission_drift"
    assert rollback["rollback"]["verified"] is False
    assert rollback["rollback"]["claimed"] is True
    assert rollback["rollback"]["approved_fields_restored"] is True
    assert rollback["rollback"]["requires_review"] is True
    assert rollback["result"] == persisted["result"]
    assert "UNAPPROVED_DRIFT_SENTINEL" not in json.dumps(rollback)
    assert x.kube.get(operation())["data"] == {"mode": "old", **extra}
    with pytest.raises(commands.CommandError, match="command_not_rollbackable"):
        reloaded.rollback(c["id"], c["plan_sha256"])


def test_unchecked_live_receipt_survives_crash_and_prevents_reexecution(setup):
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    original_get, original_mutate = x.kube.get, x.kube.mutate
    written = False

    def mutate(op, before, after, *, dry_run=False):
        nonlocal written
        result = original_mutate(op, before, after, dry_run=dry_run)
        written = written or not dry_run
        return result

    def crash_after_write(op):
        if written:
            raise SystemExit("합성 프로세스 중단")
        return original_get(op)

    x.kube.mutate = mutate
    x.kube.get = crash_after_write
    with pytest.raises(SystemExit):
        x.service.execute(c["id"])
    persisted = x.service.get(c["id"])
    assert persisted["result"]["operations"][0]["admission_drift"] == "unchecked"
    assert persisted["result"]["claimed"] is True
    with pytest.raises(commands.CommandError, match="command_not_executable"):
        x.service.execute(c["id"])


def test_created_object_with_unapproved_drift_is_not_automatically_deleted(setup):
    x = setup
    obj = commands.content(cm())
    assert obj is not None
    x.kube.objects = {}
    x.planner.operations = [manifest_operation(obj)]
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    original = x.kube.mutate

    def mutate(op, before, after, *, dry_run=False):
        if not dry_run and after is not None:
            after = copy.deepcopy(after)
            after["data"]["unexpected"] = "UNAPPROVED_CREATED_SENTINEL"
        return original(op, before, after, dry_run=dry_run)

    x.kube.mutate = mutate
    assert x.service.execute(c["id"])["error"] == "execution_admission_drift"
    writes = sum(not dry for _, dry in x.kube.calls)
    result = x.service.rollback(c["id"], c["plan_sha256"])
    assert result["status"] == "failed"
    assert result["error"] == "rollback_unreviewed_admission_drift"
    assert result["rollback"]["verified"] is False
    assert result["rollback"]["approved_fields_restored"] is False
    assert result["rollback"]["requires_review"] is True
    assert sum(not dry for _, dry in x.kube.calls) == writes
    assert x.kube.get(operation())["data"]["unexpected"] == "UNAPPROVED_CREATED_SENTINEL"
    assert "UNAPPROVED_CREATED_SENTINEL" not in json.dumps(result)


def test_rollback_does_not_remove_drift_nested_inside_an_approved_container(setup):
    x = setup
    obj = cm()
    obj.pop("data")
    x.kube.objects = {x.kube.key(obj): obj}
    c = planned(x)
    assert c["dry_run"][0]["diff"] == [{"path": "/data", "before": None, "after": {"mode": "new"}}]
    x.service.confirm(c["id"], c["plan_sha256"])
    original = x.kube.mutate
    injected = False

    def mutate(op, before, after, *, dry_run=False):
        nonlocal injected
        if not dry_run and not injected:
            injected = True
            after = copy.deepcopy(after)
            after["data"]["unexpected"] = "UNAPPROVED_NESTED_SENTINEL"
        return original(op, before, after, dry_run=dry_run)

    x.kube.mutate = mutate
    assert x.service.execute(c["id"])["error"] == "execution_admission_drift"
    writes = sum(not dry for _, dry in x.kube.calls)
    result = x.service.rollback(c["id"], c["plan_sha256"])
    assert result["status"] == "failed" and result["rollback"]["verified"] is False
    assert result["error"] == "rollback_unreviewed_admission_drift"
    assert result["rollback"]["approved_fields_restored"] is False
    assert result["rollback"]["requires_review"] is True
    assert sum(not dry for _, dry in x.kube.calls) == writes
    assert x.kube.get(operation())["data"] == {"mode": "new", "unexpected": "UNAPPROVED_NESTED_SENTINEL"}
    assert "UNAPPROVED_NESTED_SENTINEL" not in json.dumps(result)


def _status_tick(x, op):
    """컨트롤러의 status 갱신을 흉내 낸다: 내용(spec/metadata)은 같고 resourceVersion만 오른다."""
    key = x.kube.key(op)
    obj = x.kube.objects[key]
    obj.setdefault("status", {})["observedGeneration"] = obj.get("status", {}).get("observedGeneration", 0) + 1
    obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)


def test_status_only_resource_version_bump_does_not_block_confirm_execute_or_rollback(setup):
    """실측: Deployment는 status 갱신만으로 resourceVersion이 바뀐다. 이를 stale로 보면 실제 워크로드 원복이 늘 409로 막힌다."""
    x = setup
    c = planned(x)
    _status_tick(x, operation())
    x.service.confirm(c["id"], c["plan_sha256"])
    out = x.service.execute(c["id"])
    assert out["status"] == "succeeded"
    _status_tick(x, operation())
    rb = x.service.rollback(c["id"], c["plan_sha256"])
    assert rb["status"] == "rolled_back" and rb["rollback"]["verified"] is True
    assert x.kube.get(operation())["data"] == {"mode": "old"}


def test_content_change_after_plan_or_execute_is_still_stale(setup):
    x = setup
    c = planned(x)
    key = x.kube.key(operation())
    x.kube.objects[key]["data"]["other"] = "concurrent"
    x.kube.objects[key]["metadata"]["resourceVersion"] = "50"
    with pytest.raises(commands.CommandError, match="stale_plan"):
        x.service.confirm(c["id"], c["plan_sha256"])


def test_content_change_after_execute_blocks_rollback(setup):
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    x.service.execute(c["id"])
    key = x.kube.key(operation())
    x.kube.objects[key]["data"]["other"] = "concurrent"
    x.kube.objects[key]["metadata"]["resourceVersion"] = "60"
    with pytest.raises(commands.CommandError, match="stale_rollback"):
        x.service.rollback(c["id"], c["plan_sha256"])


def test_stored_version_fingerprint_is_keyed_not_a_plain_content_hash(setup):
    import hashlib
    x = setup
    c = planned(x)
    snap = c["rollback"]["operations"][0]["before_identity"]
    plain = hashlib.sha256(commands.canonical(commands.content(cm())).encode()).hexdigest()
    assert snap.get("fp") and snap["fp"] != plain
    assert "old" not in json.dumps(snap)



def test_command_must_not_succeed_when_other_actor_deleted_changed_content(db_url, http_api, monkeypatch):
    """독립 검토 재현: 실행 중 다른 행위자가 내용을 바꾸고 삭제한 경우 CommandService는 성공으로 기록하면 안 된다."""
    import app.operator_kube as ok
    from app import db
    from app.commands import CommandService
    from app.config import AppConfig
    from app.models import Incident
    from app.operator_kube import OperatorKube
    from test_p15_commands import Planner
    monkeypatch.setattr(ok, "DELETE_WAIT_S", 0.3)
    monkeypatch.setattr(ok, "DELETE_POLL_S", 0.05)
    h = http_api
    k = OperatorKube(h.api)
    with db.session() as s:
        inc = Incident(fingerprint="delete-review", namespace="sandbox", workload="settings", title="삭제 경합 재현", status="handed_off")
        s.add(inc)
        s.flush()
        iid = inc.id
    service = CommandService(AppConfig(), Planner([operation("delete")]), k)
    command = service.create(iid, "sandbox의 settings ConfigMap을 삭제")
    command = service.plan(command["id"])
    assert command["status"] == "awaiting_confirmation"
    service.confirm(command["id"], command["plan_sha256"])
    original_delete, original_get = k.dynamic.delete, k.get
    phase = {"active": False, "gets": 0}

    def raced_delete(*args, **kwargs):
        if not kwargs.get("dry_run"):
            changed = cm(data={"mode": "concurrent-unapproved"})
            changed["metadata"].update(resourceVersion="3", deletionTimestamp="2026-10-21T00:00:00Z", finalizers=["orphan"])
            h.obj, h.delete = changed, (409, {"kind": "Status", "code": 409, "reason": "Conflict"})
            phase["active"] = True
        return original_delete(*args, **kwargs)

    def raced_get(op):
        if phase["active"]:
            phase["gets"] += 1
            if phase["gets"] >= 2:
                h.obj = None
        return original_get(op)
    monkeypatch.setattr(k.dynamic, "delete", raced_delete)
    monkeypatch.setattr(k, "get", raced_get)
    out = service.execute(command["id"])
    assert out["status"] == "failed", out
