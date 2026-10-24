"""P15 deployment boundary: discovery-only RBAC and independent write identities."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/base/liveprobe/operator-executor.yaml"
GENERATOR = ROOT / "scripts/gen-operator-rbac.py"


def documents():
    assert MANIFEST.is_file(), "P15 operator identities/guards manifest is missing"
    return list(yaml.safe_load_all(MANIFEST.read_text()))


def named(kind, name):
    return next(d for d in documents() if d["kind"] == kind and d["metadata"]["name"] == name)


def generator():
    assert GENERATOR.is_file(), "discovery-only RBAC generator is missing"
    spec = importlib.util.spec_from_file_location("operator_rbac_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discovery(group_version, *resources):
    return {"groupVersion": group_version, "resources": [
        {"name": name, "kind": kind, "verbs": verbs, "namespaced": name != "nodes"}
        for name, kind, verbs in resources
    ]}


def test_separate_identities_and_controller_populated_tokens():
    for account in ("liveprobe-operator-executor", "liveprobe-secret-writer"):
        sa = named("ServiceAccount", account)
        assert sa["metadata"]["namespace"] == "liveprobe-system"
        assert sa["automountServiceAccountToken"] is False
        token = named("Secret", account + "-token")
        assert token["type"] == "kubernetes.io/service-account-token"
        assert token["metadata"]["annotations"] == {"kubernetes.io/service-account.name": account}
        assert "data" not in token and "stringData" not in token
        binding = named("ClusterRoleBinding", account)
        assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": account}
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": account, "namespace": "liveprobe-system"}]
    writer = named("ClusterRole", "liveprobe-secret-writer")
    assert writer["rules"] == [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["patch"]}]


def test_generator_is_pure_deterministic_and_excludes_escalation_resources():
    mod = generator()
    all_verbs = ["get", "list", "watch", "create", "update", "patch", "delete", "deletecollection", "bind", "escalate", "impersonate"]
    fixture = [
        discovery("v1", ("configmaps", "ConfigMap", all_verbs), ("nodes", "Node", all_verbs),
                  ("secrets", "Secret", all_verbs), ("serviceaccounts", "ServiceAccount", all_verbs),
                  ("serviceaccounts/token", "TokenRequest", all_verbs), ("pods/exec", "PodExecOptions", all_verbs),
                  ("bindings", "Binding", all_verbs), ("events", "Event", all_verbs)),
        discovery("apps/v1", ("deployments", "Deployment", all_verbs)),
        discovery("rbac.authorization.k8s.io/v1", ("clusterroles", "ClusterRole", all_verbs)),
        discovery("admissionregistration.k8s.io/v1", ("validatingadmissionpolicies", "ValidatingAdmissionPolicy", all_verbs)),
        discovery("certificates.k8s.io/v1", ("certificatesigningrequests", "CertificateSigningRequest", all_verbs)),
        discovery("example.invalid/v1", ("deployments", "Deployment", all_verbs)),
        discovery("coordination.k8s.io/v1", ("leases", "Lease", all_verbs)),
    ]
    snapshot = copy.deepcopy(fixture)
    rules = mod.generate_rules(fixture)
    assert rules == mod.generate_rules(list(reversed(fixture)))
    assert fixture == snapshot
    flat = {(g, r): v for rule in rules for g in rule["apiGroups"] for r in rule["resources"] for v in [rule["verbs"]]}
    assert set(flat) == {("", "configmaps"), ("", "nodes"), ("apps", "deployments")}
    assert flat[("", "nodes")] == ["get", "list", "watch", "update", "patch"]
    assert flat[("", "configmaps")] == ["get", "list", "watch", "create", "update", "patch", "delete"]
    assert mod.generate_rules([discovery("apps/v1", ("deployments", "Deployment", ["get", "patch"]))])[0]["verbs"] == ["get", "patch"]


def test_static_operator_rules_have_no_wildcards_secrets_or_subresources():
    mod = generator()
    rules = named("ClusterRole", "liveprobe-operator-executor")["rules"]
    mod.validate_rules(rules)
    assert rules
    for rule in rules:
        assert set(rule) == {"apiGroups", "resources", "verbs"}
        assert len(rule["apiGroups"]) == 1 and len(rule["resources"]) == 1
        assert "*" not in str(rule) and "/" not in rule["resources"][0]
        assert set(rule["verbs"]) <= {"get", "list", "watch", "create", "update", "patch", "delete"}
        assert rule["resources"][0] not in {"secrets", "serviceaccounts", "events", "bindings", "leases"}
    bad = [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]
    try:
        mod.validate_rules(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("static validation accepted Secret read access")


def test_admission_is_fail_closed_identity_scoped_and_bound_to_deny():
    for name, account in (("liveprobe-operator-boundary", "liveprobe-operator-executor"),
                          ("liveprobe-secret-writer-boundary", "liveprobe-secret-writer")):
        policy = named("ValidatingAdmissionPolicy", name)["spec"]
        assert policy["failurePolicy"] == "Fail"
        assert policy["matchConstraints"]["matchPolicy"] == "Equivalent"
        assert policy["matchConstraints"]["resourceRules"] == [{"apiGroups": ["*"], "apiVersions": ["*"],
            "operations": ["CREATE", "UPDATE", "DELETE", "CONNECT"], "resources": ["*/*"]}]
        assert policy["matchConditions"] == [{"name": "dedicated-identity", "expression":
            f"request.userInfo.username == 'system:serviceaccount:liveprobe-system:{account}'"}]
        assert all(v["expression"].strip() not in {"true", "false"} for v in policy["validations"])
        binding = named("ValidatingAdmissionPolicyBinding", name)["spec"]
        assert binding == {"policyName": name, "validationActions": ["Deny"]}
        assert "liveprobe-system" in str(policy) and "liveprobe-observability" in str(policy)
    operator = named("ValidatingAdmissionPolicy", "liveprobe-operator-boundary")["spec"]
    validations = {v["message"]: v["expression"] for v in operator["validations"]}
    assert {"executor resource surface", "protected objects", "safe workload creation", "existing workload tuning only", "node cordon only", "safe deletion only", "protected metadata"} <= validations.keys()
    variables = {v["name"]: v["expression"] for v in operator["variables"]}
    # AdmissionRequest는 omitempty로 빈 subResource·cluster 범위 namespace 키를 생략한다.
    # 직접 접근하면 no such key 오류 → failurePolicy Fail로 정상 요청까지 거부된다.
    writer = named("ValidatingAdmissionPolicy", "liveprobe-secret-writer-boundary")["spec"]
    for spec in (operator, writer):
        text = "\n".join([v["expression"] for v in spec["validations"]] + [v["expression"] for v in spec["variables"]])
        bare = text.replace("!has(request.subResource) || request.subResource == ''", "")
        assert "request.subResource" not in bare
        assert "request.namespace" not in text.replace("has(request.namespace) ? request.namespace", "")
    assert "!has(request.subResource) || request.subResource == ''" in validations["executor resource surface"]
    # StatefulSet은 컨트롤러가 PVC를 대신 만들므로 생성 시 claim 템플릿을 허용하지 않는다.
    assert "volumeClaimTemplates" in validations["safe workload creation"]
    assert "variables.safePod" in validations["safe workload creation"]
    assert "variables.safeTuning" in validations["existing workload tuning only"]
    assert "variables.safePod" in validations["safe deletion only"]
    source = "\n".join(variables.values())
    for required in ("automountServiceAccountToken", "secretKeyRef", "configMapRef", "volumes", "emptyDir", "hostNetwork", "hostPID", "hostIPC", "privileged", "allowPrivilegeEscalation", "capabilities", "procMount", "sysctls", "volumeMounts", "imagePullSecrets", "serviceAccountName", "ephemeralContainers", "jobTemplate", "initContainers", "resources", "limits", "requests", "cpu", "memory", "liveprobe.io/restarted-at", "unschedulable"):
        assert required in source, required
    assert "kubernetes.io/service-account-token" in str(named("ValidatingAdmissionPolicy", "liveprobe-secret-writer-boundary"))


def test_api_mounts_are_distinct_optional_read_only_and_old_identities_unchanged():
    path = ROOT / "deploy/base/liveprobe/liveprobe.yaml"
    docs = list(yaml.safe_load_all(path.read_text()))
    deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "liveprobe-api")
    spec = deployment["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "liveprobe"
    api = next(c for c in spec["containers"] if c["name"] == "api")
    mounts = {v["name"]: v for v in api["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    for name in ("operator-executor", "secret-writer"):
        assert mounts[name] == {"name": name, "mountPath": "/var/run/secrets/" + name, "readOnly": True}
        assert volumes[name] == {"name": name, "secret": {"secretName": "liveprobe-" + name + "-token", "optional": True, "items": [{"key": "token", "path": "token"}]}}
    assert mounts["safe-action-executor"]["mountPath"] == "/var/run/secrets/safe-action-executor"
    assert volumes["safe-action-executor"]["secret"]["secretName"] == "safe-action-executor-token"
    # Compare full old RBAC/SA declarations with committed baseline, not a weak verb subset.
    import subprocess
    baseline = yaml.safe_load_all(subprocess.run(["git", "show", "HEAD:deploy/base/liveprobe/liveprobe.yaml"], cwd=ROOT,
                                               check=True, capture_output=True, text=True).stdout)
    protected = {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
    assert [d for d in docs if d["kind"] in protected] == [d for d in baseline if d["kind"] in protected]
    new_bindings = [d for d in documents() if d["kind"].endswith("Binding") and d["kind"] != "ValidatingAdmissionPolicyBinding"]
    assert all(s["name"] not in {"liveprobe", "safe-action-executor"} for d in new_bindings for s in d["subjects"])
    kustomization = yaml.safe_load((ROOT / "deploy/base/kustomization.yaml").read_text())
    assert kustomization["resources"].count("liveprobe/operator-executor.yaml") == 1


def test_admission_blocks_implicit_escalation_and_controller_secret_references():
    policy = named("ValidatingAdmissionPolicy", "liveprobe-operator-boundary")["spec"]
    variables = {v["name"]: v["expression"] for v in policy["variables"]}
    assert "has(c.securityContext) &&" in variables["safePod"]
    assert "has(c.securityContext.allowPrivilegeEscalation)" in variables["safePod"]
    # A controller-created Pod is not admitted as the executor. Require an
    # explicit Istio opt-out in templates so namespace-default injection cannot
    # add service-account-token volumes after this identity-scoped template gate.
    assert "variables.tm.labels['sidecar.istio.io/inject'] == 'false'" in variables["safePod"]
    refs = next(v["expression"] for v in policy["validations"] if v["message"] == "safe indirect references")
    for field in ("scaleTargetRef", "tls", "parameters", "liveprobe-", "safe-action-", "rollback-controller"):
        assert field in refs


def test_storage_is_fail_closed_even_with_direct_executor_token():
    import re
    policy = named("ValidatingAdmissionPolicy", "liveprobe-operator-boundary")["spec"]
    surface = next(v["expression"] for v in policy["validations"] if v["message"] == "executor resource surface")
    # No CREATE/UPDATE/DELETE path for PV/local.path, PVC, or StorageClass
    # parameters such as csi.storage.k8s.io/node-publish-secret-name/namespace.
    for forbidden in ("persistentvolumes", "persistentvolumeclaims", "storageclasses", "storage.k8s.io"):
        assert repr(forbidden) not in surface
    role = named("ClusterRole", "liveprobe-operator-executor")
    read_only_resources = {"namespaces", "persistentvolumes", "persistentvolumeclaims", "storageclasses"}
    for rule in role["rules"]:
        if rule["resources"][0] in read_only_resources:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}
    mod = generator()
    fixture = [discovery("v1", ("persistentvolumes", "PersistentVolume", list(mod.VERBS)),
                         ("persistentvolumeclaims", "PersistentVolumeClaim", list(mod.VERBS))),
               discovery("storage.k8s.io/v1", ("storageclasses", "StorageClass", list(mod.VERBS)))]
    assert all(r["verbs"] == ["get", "list", "watch"] for r in mod.generate_rules(fixture))
    safe_pod = next(v["expression"] for v in policy["variables"] if v["name"] == "safePod")
    match = re.search(r"v\.all\(k, k in \[(.*?)\]\)", safe_pod)
    assert match is not None
    allowed = match.group(1)
    assert set(re.findall(r"'([^']+)'", allowed)) == {"name", "emptyDir", "configMap", "downwardAPI"}
    assert "variables.ps.volumes.all(v," in safe_pod
    assert "has(v.emptyDir) || has(v.configMap) || has(v.downwardAPI)" in safe_pod


def test_generator_discovery_transport_never_reads_resource_objects(monkeypatch):
    import json
    from types import SimpleNamespace
    mod = generator()
    calls = []
    core = discovery("v1", ("configmaps", "ConfigMap", ["get", "patch"]))
    responses = {"/api/v1": core, "/apis": {"groups": [
        {"versions": [{"groupVersion": "apps/v1"}]},
        {"versions": [{"groupVersion": "authentication.k8s.io/v1"}]},
        {"versions": [{"groupVersion": "custom.invalid/v1"}]},
    ]}, "/apis/apps/v1": discovery("apps/v1", ("deployments", "Deployment", ["get", "update"]))}
    def run(args, **kwargs):
        assert args[:5] == ["kubectl", "--context", "fixture-context", "get", "--raw"]
        assert len(args) == 6 and args[5] in responses
        assert kwargs["check"] is True and kwargs["timeout"] == 30
        calls.append(args[5])
        return SimpleNamespace(stdout=json.dumps(responses[args[5]]))
    monkeypatch.setattr(mod.subprocess, "run", run)
    result = mod.read_discovery("fixture-context")
    assert len(result) == 2
    assert calls == ["/api/v1", "/apis", "/apis/apps/v1"]


def test_generator_check_is_read_only_and_reports_discovery_drift(tmp_path, capsys):
    import json
    mod = generator()
    original = MANIFEST.read_text()
    path = tmp_path / "operator.yaml"
    path.write_text(original)
    # Static validation must neither connect to Kubernetes nor rewrite YAML.
    assert mod.main(["--check", "--static", "--manifest", str(path)]) == 0
    fixture = tmp_path / "discovery.json"
    fixture.write_text(json.dumps([discovery("v1", ("configmaps", "ConfigMap", ["get"]))]))
    assert mod.main(["--check", "--manifest", str(path), "--discovery-json", str(fixture)]) == 1
    assert "discovery drift" in capsys.readouterr().err
    assert path.read_text() == original
    # Generation touches the marked block only; the next fixture check matches.
    assert mod.main(["--manifest", str(path), "--discovery-json", str(fixture)]) == 0
    assert mod.main(["--check", "--manifest", str(path), "--discovery-json", str(fixture)]) == 0
    assert path.read_text().split(mod.END, 1)[1] == original.split(mod.END, 1)[1]
