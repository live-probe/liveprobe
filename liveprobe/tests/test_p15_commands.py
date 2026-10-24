"""P15 operator commands: real PostgreSQL transactions, in-memory API boundary."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig
from app.llm.base import LLMResult, Usage
from app.models import AgentRun, Incident, OperatorCommand


class Planner:
    name = "fake"

    def __init__(self, operations):
        self.operations = operations
        self.prompts = []

    def complete(self, role, prompt, schema, model, effort, timeout):
        self.prompts.append(prompt)
        return LLMResult("", {"summary_ko": "운영자 지시 변경", "operations": self.operations},
                         Usage(100, 50), 12, "fake", model, effort)


def cm(name="settings", data=None):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"namespace": "sandbox", "name": name, "uid": "uid-1", "resourceVersion": "1"},
            "data": data or {"mode": "old"}}


def operation(op="patch", **kw):
    return {"op": op, "apiVersion": "v1", "kind": "ConfigMap", "namespace": "sandbox", "name": "settings",
            **({"patch": {"data": {"mode": "new"}}} if op == "patch" else {}), **kw}


class MemoryKube:
    def __init__(self, obj=None):
        self.objects = {} if obj is None else {self.key(obj): copy.deepcopy(obj)}
        self.calls = []
        self.fail_dry = False
        self.fail_write = False

    @staticmethod
    def key(o):
        md = o.get("metadata", o)
        return (o["apiVersion"], o["kind"], md.get("namespace"), md["name"])

    def get(self, op):
        return copy.deepcopy(self.objects.get(self.key(op)))

    def mutate(self, op, before, after, *, dry_run=False):
        self.calls.append((op["op"], dry_run))
        if (dry_run and self.fail_dry) or (not dry_run and self.fail_write):
            raise RuntimeError("untrusted response body must not be stored")
        current = self.get(op)
        if current != before:
            raise RuntimeError("conflict")
        if after is None:
            if not dry_run:
                self.objects.pop(self.key(op), None)
            return None
        out = copy.deepcopy(after)
        out["metadata"]["uid"] = (before or {}).get("metadata", {}).get("uid", "created-uid")
        out["metadata"]["resourceVersion"] = str(int((before or {}).get("metadata", {}).get("resourceVersion", "0")) + 1)
        if not dry_run:
            self.objects[self.key(op)] = out
        return copy.deepcopy(out)


@pytest.fixture
def setup(db_url):
    from app.commands import CommandService
    with db.session() as s:
        row = Incident(fingerprint="p15", namespace="sandbox", workload="settings", title="시험", status="handed_off")
        s.add(row)
        s.flush()
        iid = row.id
    planner, kube = Planner([operation()]), MemoryKube(cm())
    return SimpleNamespace(service=CommandService(AppConfig(), planner, kube), planner=planner, kube=kube, iid=iid)


def planned(x):
    c = x.service.create(x.iid, "설정 mode를 new로 바꿔줘")
    assert c["status"] == "planning"
    x.service.plan(c["id"])
    return x.service.get(c["id"])


def test_plan_is_dry_only_and_explicit_confirmation_precedes_verified_execution(setup):
    x = setup
    c = planned(x)
    assert c["status"] == "awaiting_confirmation"
    assert x.kube.calls == [("patch", True)]
    assert x.kube.get(operation())["data"] == {"mode": "old"}
    assert c["dry_run"][0]["ok"] is True
    assert {"path": "/data/mode", "before": "old", "after": "new"} in c["dry_run"][0]["diff"]
    assert c["side_effects"]["reversible"] is True
    assert c["side_effects"]["targets"] == ["sandbox/ConfigMap/settings"]
    assert c["confirm_question"] == "정말 진행하시겠습니까?"
    assert x.service.list(x.iid)[0]["plan_sha256"] == c["plan_sha256"]
    x.service.confirm(c["id"], c["plan_sha256"])
    out = x.service.execute(c["id"])
    assert out["status"] == "succeeded"
    assert out["verification"]["ok"] is True
    assert x.kube.get(operation())["data"]["mode"] == "new"
    with db.session() as s:
        run = s.scalar(select(AgentRun).where(AgentRun.role == "operator_planner"))
        assert run.model == "gpt-6.1-sol" and run.input_tokens == 100


def test_rollback_restores_only_approved_fields_and_is_version_fenced(setup):
    from app.commands import CommandError
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    x.service.execute(c["id"])
    out = x.service.rollback(c["id"], c["plan_sha256"])
    assert out["status"] == "rolled_back" and out["rollback"]["verified"] is True
    assert x.kube.get(operation())["data"] == {"mode": "old"}
    with pytest.raises(CommandError):
        x.service.rollback(c["id"], c["plan_sha256"])


def test_hash_staleness_and_cancel_fail_closed(setup):
    from app.commands import CommandError
    x = setup
    c = planned(x)
    with pytest.raises(CommandError, match="plan_hash_mismatch"):
        x.service.confirm(c["id"], "0" * 64)
    # A real concurrent edit changes desired content (a status-only resourceVersion bump is not stale).
    x.kube.objects[x.kube.key(operation())]["metadata"]["resourceVersion"] = "9"
    x.kube.objects[x.kube.key(operation())]["data"]["concurrent"] = "edit"
    with pytest.raises(CommandError, match="stale_plan"):
        x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.cancel(c["id"])["status"] == "cancelled"
    with pytest.raises(CommandError):
        x.service.confirm(c["id"], c["plan_sha256"])
    assert x.kube.calls == [("patch", True)]


def test_concurrent_execution_claim_allows_one_write(setup):
    from concurrent.futures import ThreadPoolExecutor
    from app.commands import CommandError
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    def run(_):
        try:
            return x.service.execute(c["id"])["status"]
        except CommandError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ["conflict", "succeeded"]
    assert x.kube.calls.count(("patch", False)) == 1


def test_failed_dry_run_cannot_be_confirmed_and_response_body_is_not_persisted(setup):
    from app.commands import CommandError
    x = setup
    x.kube.fail_dry = True
    c = planned(x)
    assert c["status"] == "rejected"
    assert c["dry_run"][0]["ok"] is False and c["dry_run"][0]["error"]
    assert "untrusted response" not in json.dumps(c)
    with pytest.raises(CommandError):
        x.service.confirm(c["id"], "0" * 64)


def test_external_change_after_execution_blocks_rollback(setup):
    from app.commands import CommandError
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    x.service.execute(c["id"])
    # A real concurrent edit changes desired content (a status-only resourceVersion bump is not stale).
    x.kube.objects[x.kube.key(operation())]["metadata"]["resourceVersion"] = "99"
    x.kube.objects[x.kube.key(operation())]["data"]["concurrent"] = "edit"
    with pytest.raises(CommandError, match="stale_rollback"):
        x.service.rollback(c["id"], c["plan_sha256"])
    assert x.kube.get(operation())["data"]["mode"] == "new"


def workload():
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "sandbox", "uid": "web-uid", "resourceVersion": "1"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}}, "template": {"metadata": {"labels": {"app": "web"}},
                "spec": {"automountServiceAccountToken": False, "containers": [{"name": "web", "image": "web:v1", "resources": {"limits": {"memory": "128Mi"}}}]}}}}


@pytest.mark.parametrize("bad", [
    operation(kind="Secret"), operation(kind="secret"), operation(kind="Pod/exec"),
    operation(kind="ClusterRole", apiVersion="rbac.authorization.k8s.io/v1", namespace=None),
    operation(kind="RoleBinding", apiVersion="rbac.authorization.k8s.io/v1"),
    operation(kind="MutatingWebhookConfiguration", apiVersion="admissionregistration.k8s.io/v1", namespace=None),
    operation(kind="CustomResourceDefinition", apiVersion="apiextensions.k8s.io/v1", namespace=None),
    operation(kind="ServiceAccount"), operation(kind="TokenReview", apiVersion="authentication.k8s.io/v1"),
    operation(name="*"), operation(name="x/y"), operation(namespace="liveprobe-system"),
    operation(apiVersion="v1/../../v1"), operation(kind="Node", apiVersion="example.io/v1", namespace=None),
    operation("delete", kind="Namespace", namespace=None), operation("delete", kind="PersistentVolume", namespace=None),
    operation("apply", manifest={"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "different", "namespace": "sandbox"}}),
    operation(patch={"metadata": {"resourceVersion": "bad"}}), operation(patch={"status": {"ready": True}}),
    operation(patch={"spec": {"template": {"spec": {"hostNetwork": True}}}}),
    operation(patch={"spec": {"template": {"spec": {"volumes": [{"name": "host", "hostPath": {"path": "/proc"}}]}}}}),
    operation(patch={"nested": [{"securityContext": {"privileged": True}}]}),
    operation(patch={"nested": {"projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}}}),
    operation(patch={"nested": {"envFrom": [{"secretRef": {"name": "db"}}]}}),
    operation(patch={"nested": {"env": [{"valueFrom": {"secretKeyRef": {"name": "db", "key": "pwd"}}}]}}),
    operation(patch={"nested": {"volumes": [{"secret": {"secretName": "db"}}]}}),
])
def test_adversarial_plan_is_rejected_before_target_read(bad):
    from app.commands import CommandError, validate_operation
    with pytest.raises(CommandError):
        validate_operation(bad)


def test_existing_secret_workload_allows_resource_only_delta_without_persisting_values(setup):
    x = setup
    w = workload()
    w["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": "EXISTING_PRIVATE_SENTINEL"}, {"name": "REF", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pwd"}}}]
    x.kube.objects = {x.kube.key(w): w}
    op = operation(kind="Deployment", apiVersion="apps/v1", name="web", patch=[{"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": "256Mi"}])
    x.planner.operations = [op]
    c = planned(x)
    assert c["status"] == "awaiting_confirmation"
    assert "EXISTING_PRIVATE_SENTINEL" not in json.dumps(c)
    assert "EXISTING_PRIVATE_SENTINEL" not in json.dumps(x.planner.prompts)
    x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.execute(c["id"])["status"] == "succeeded"
    assert x.service.rollback(c["id"], c["plan_sha256"])["status"] == "rolled_back"


def test_existing_secret_mount_blocks_image_change_even_if_reference_is_unchanged():
    from app.commands import CommandError, materialize, validate_delta
    w = workload()
    w["spec"]["template"]["spec"]["volumes"] = [{"name": "secret", "secret": {"secretName": "db"}}]
    op = operation(kind="Deployment", apiVersion="apps/v1", name="web", patch={"spec": {"template": {"spec": {"containers": [{"name": "web", "image": "exfil:v1"}]}}}})
    with pytest.raises(CommandError):
        validate_delta(op, w, materialize(op, w))


@pytest.mark.parametrize("typ", ["apply", "delete", "scale", "restart", "cordon", "uncordon"])
def test_all_nonsecret_operations_execute_and_restore(setup, typ):
    x = setup
    obj = workload() if typ in {"scale", "restart"} else cm()
    if typ in {"cordon", "uncordon"}:
        obj = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "worker", "uid": "node-id", "resourceVersion": "1"}, "spec": {"unschedulable": typ == "uncordon"}}
    op = {"op": typ, "apiVersion": obj["apiVersion"], "kind": obj["kind"], "namespace": obj["metadata"].get("namespace"), "name": obj["metadata"]["name"]}
    x.kube.objects = {x.kube.key(obj): copy.deepcopy(obj)}
    if typ == "apply":
        manifest = copy.deepcopy(obj)
        manifest["metadata"].pop("uid")
        manifest["metadata"].pop("resourceVersion")
        op["manifest_json"] = json.dumps(manifest)
        x.kube.objects = {}
    if typ == "scale":
        op["replicas"] = 3
    x.planner.operations = [op]
    c = planned(x)
    assert c["status"] == "awaiting_confirmation", c["error"]
    x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.execute(c["id"])["status"] == "succeeded"
    out = x.service.rollback(c["id"], c["plan_sha256"])
    assert out["status"] == "rolled_back", out["error"]
    from app.commands import content
    assert content(x.kube.get(op)) == (None if typ == "apply" else content(obj))
    if typ == "delete":
        assert out["rollback"]["results"][0]["exact_uid_restore"] is False


def test_analyzing_incident_requires_confirmation_and_post_confirm_stale_is_blocked(setup):
    from app.commands import CommandError
    x = setup
    with db.session() as s:
        s.get(Incident, x.iid).status = "analyzing"
    c = planned(x)
    with pytest.raises(CommandError):
        x.service.execute(c["id"])
    x.service.confirm(c["id"], c["plan_sha256"])
    # A real concurrent edit changes desired content (a status-only resourceVersion bump is not stale).
    x.kube.objects[x.kube.key(operation())]["metadata"]["resourceVersion"] = "2"
    x.kube.objects[x.kube.key(operation())]["data"]["concurrent"] = "edit"
    out = x.service.execute(c["id"])
    assert out["status"] == "failed" and out["error"] == "stale_plan"
    assert not any(not dry for _, dry in x.kube.calls)


def test_namespace_creation_is_not_advertised_as_safe_destructive_rollback(setup):
    from app.commands import CommandError
    x = setup
    x.planner.operations = [{"op": "apply", "apiVersion": "v1", "kind": "Namespace", "namespace": None, "name": "new-space",
                             "manifest": {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "new-space"}}}]
    c = planned(x)
    assert c["status"] == "awaiting_confirmation"
    assert c["side_effects"]["reversible"] is False
    x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.execute(c["id"])["status"] == "succeeded"
    with pytest.raises(CommandError):
        x.service.rollback(c["id"], c["plan_sha256"])


@pytest.mark.parametrize("bad", [
    operation(kind="ExternalSecret", apiVersion="external-secrets.io/v1", patch={"spec": {"dataFrom": [{"extract": {"key": "sensitive"}}]}}),
    operation(patch=[{"op": "add", "path": "/spec/template/metadata/annotations/sidecar.istio.io~1inject", "value": "true"}]),
    operation(patch={"metadata": {"labels": {"pod-security.kubernetes.io/enforce": "privileged"}}}),
    operation(patch={"nested": {"volumeMounts": [{"name": "host", "mountPath": "/proc"}]}}),
    operation(patch={"nested": {"volumeMounts": [{"name": "host", "mountPath": "/dev/root"}]}}),
])
def test_controller_and_admission_escape_paths_are_closed(bad):
    from app.commands import CommandError, validate_operation
    with pytest.raises(CommandError):
        validate_operation(bad)


def test_planner_cannot_persist_existing_inline_env_value_in_merge_patch(setup):
    x = setup
    w = workload()
    w["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": "EXISTING_PRIVATE_SENTINEL"}]
    x.kube.objects = {x.kube.key(w): w}
    containers = copy.deepcopy(w["spec"]["template"]["spec"]["containers"])
    containers[0]["resources"]["limits"]["memory"] = "256Mi"
    x.planner.operations = [operation(kind="Deployment", apiVersion="apps/v1", name="web", patch={"spec": {"template": {"spec": {"containers": containers}}}})]
    c = planned(x)
    assert c["status"] == "rejected"
    assert "EXISTING_PRIVATE_SENTINEL" not in json.dumps(c)


def test_partial_results_survive_crash_and_unknown_effect_is_never_retried(setup):
    from app.commands import CommandError
    x = setup
    other = cm("other")
    other["metadata"]["uid"] = "other-uid"
    x.kube.objects[x.kube.key(other)] = other
    x.planner.operations = [operation(), operation(name="other")]
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    original = x.kube.mutate
    def crash(op, before, after, *, dry_run=False):
        if op["name"] == "other" and not dry_run:
            raise SystemExit("simulated process death")
        return original(op, before, after, dry_run=dry_run)
    x.kube.mutate = crash
    with pytest.raises(SystemExit):
        x.service.execute(c["id"])
    out = x.service.get(c["id"])
    assert out["result"]["claimed"] is True
    assert len(out["result"]["operations"]) == 1
    assert out["result"]["inflight"] == 1
    with pytest.raises(CommandError):
        x.service.execute(c["id"])
    with pytest.raises(CommandError):
        x.service.cancel(c["id"])


def test_admission_drift_after_approval_is_rejected_without_live_write(setup):
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    original = x.kube.mutate
    def admission_drift(op, before, after, *, dry_run=False):
        admitted = original(op, before, after, dry_run=dry_run)
        if dry_run:
            admitted["data"]["new-admission-value"] = "changed"
        return admitted
    x.kube.mutate = admission_drift
    out = x.service.execute(c["id"])
    assert out["status"] == "failed" and out["error"] == "admission_drift"
    assert not any(not dry for _, dry in x.kube.calls)


def test_rollback_rechecks_version_after_preflight(setup):
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    x.service.execute(c["id"])
    original, reads = x.kube.get, []
    def concurrent_change(op):
        reads.append(1)
        if len(reads) == 2:
            x.kube.objects[x.kube.key(op)]["metadata"]["resourceVersion"] = "99"
            x.kube.objects[x.kube.key(op)]["data"]["mode"] = "external-change"
        return original(op)
    x.kube.get = concurrent_change
    out = x.service.rollback(c["id"], c["plan_sha256"])
    assert out["status"] == "failed" and out["error"] == "stale_rollback"
    assert x.kube.get(operation())["data"]["mode"] == "external-change"


def test_rollback_crash_is_durably_claimed_and_never_replayed(setup):
    from app.commands import CommandError
    x = setup
    c = planned(x)
    x.service.confirm(c["id"], c["plan_sha256"])
    x.service.execute(c["id"])
    original = x.kube.mutate
    def crash(op, before, after, *, dry_run=False):
        out = original(op, before, after, dry_run=dry_run)
        if not dry_run:
            raise SystemExit("process died after rollback request")
        return out
    x.kube.mutate = crash
    with pytest.raises(SystemExit):
        x.service.rollback(c["id"], c["plan_sha256"])
    out = x.service.get(c["id"])
    assert out["status"] == "rolling_back" and out["rollback"]["inflight"] == 0
    with pytest.raises(CommandError):
        x.service.rollback(c["id"], c["plan_sha256"])


def test_provider_failure_preserves_usage_without_exception_body(setup):
    from app.llm.base import LLMError
    x = setup
    def failure(*_):
        raise LLMError("provider", "SENSITIVE_PROVIDER_BODY", usage=Usage(300, 20), duration_ms=21)
    x.planner.complete = failure
    c = planned(x)
    assert c["status"] == "rejected" and "SENSITIVE_PROVIDER_BODY" not in json.dumps(c)
    with db.session() as s:
        run = s.scalar(select(AgentRun).where(AgentRun.role == "operator_planner"))
        assert run.input_tokens == 300 and run.duration_ms == 21
        assert "SENSITIVE_PROVIDER_BODY" not in json.dumps(run.output) + (run.error or "")


@pytest.mark.parametrize("kind", ["Job", "CronJob", "Ingress", "IngressClass", "NetworkPolicy", "HorizontalPodAutoscaler", "PodDisruptionBudget", "StorageClass"])
def test_supported_kind_cannot_be_spoofed_in_unreviewed_api_group(kind):
    from app.commands import CommandError, validate_operation
    with pytest.raises(CommandError):
        validate_operation(operation(kind=kind, apiVersion="unreviewed.example.io/v1"))


def test_same_resource_across_api_versions_is_still_a_duplicate_target():
    from app.commands import CommandError, normalize_plan
    ops = [operation(kind="HorizontalPodAutoscaler", apiVersion=v, patch={"spec": {"minReplicas": 1}}) for v in ("autoscaling/v1", "autoscaling/v2")]
    with pytest.raises(CommandError, match="duplicate_target"):
        normalize_plan({"summary_ko": "중복 대상 금지", "operations": ops})


def test_restart_existing_default_token_workload_preserves_credential_access(setup):
    x = setup
    w = workload()
    w["spec"]["template"]["spec"].pop("automountServiceAccountToken")
    x.kube.objects = {x.kube.key(w): w}
    x.planner.operations = [operation("restart", apiVersion="apps/v1", kind="Deployment", name="web")]
    c = planned(x)
    assert c["status"] == "awaiting_confirmation", c["error"]
    x.service.confirm(c["id"], c["plan_sha256"])
    assert x.service.execute(c["id"])["status"] == "succeeded"


def test_planner_prompt_names_standard_api_versions_for_builtin_kinds(setup):
    """실측: 운영자가 apiVersion을 말하지 않으면 플래너가 빈 계획을 냈다. 내장 종류의 표준 apiVersion을 알려준다."""
    x = setup
    planned(x)
    prompt = x.planner.prompts[-1]
    for pair in ("Deployment=apps/v1", "StatefulSet=apps/v1", "ConfigMap=v1", "Service=v1", "CronJob=batch/v1", "Ingress=networking.k8s.io/v1"):
        assert pair in prompt, pair
    assert "추측" in prompt


def test_empty_plan_is_rejected_with_operator_readable_reason(setup):
    x = setup
    x.planner.operations = []
    c = planned(x)
    assert c["status"] == "rejected" and c["error"] == "plan_has_no_operations"
    assert c["plan"] is None or c["plan"].get("operations") in (None, [])
