"""KAN-103 fail-closed RBAC/admission contract (rendered base, no live cluster)."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def rendered():
    result = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "deploy/base")],
        check=True, capture_output=True, text=True,
    )
    return list(yaml.safe_load_all(result.stdout))


def test_liveprobe_has_observe_but_no_target_write_or_logs():
    objs = rendered()
    sa = {"kind": "ServiceAccount", "name": "liveprobe", "namespace": "liveprobe-system"}
    bound = []
    for binding in objs:
        if binding.get("kind") not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if sa not in binding.get("subjects", []):
            continue
        ref = binding["roleRef"]
        role = next(o for o in objs if o.get("kind") == ref["kind"]
                    and o["metadata"]["name"] == ref["name"]
                    and (ref["kind"] == "ClusterRole" or o["metadata"].get("namespace") == binding["metadata"].get("namespace")))
        bound.extend(role["rules"])
    assert bound
    assert any("pods" in rule["resources"] and "list" in rule["verbs"] for rule in bound)
    assert any("deployments" in rule["resources"] and "get" in rule["verbs"] for rule in bound)
    for rule in bound:
        assert not set(rule["verbs"]) & {"*", "patch", "update", "create", "delete", "deletecollection"}
        assert not set(rule["resources"]) & {"*", "secrets", "pods/log", "pods/exec", "serviceaccounts/token"}


def test_admission_blocks_even_an_accidental_extra_role_binding():
    objs = rendered()
    policy = next(o for o in objs if o.get("kind") == "ValidatingAdmissionPolicy"
                  and o["metadata"]["name"] == "liveprobe-deny-target-writes")
    binding = next(o for o in objs if o.get("kind") == "ValidatingAdmissionPolicyBinding"
                   and o["spec"]["policyName"] == policy["metadata"]["name"])
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert "request.namespace == 'liveprobe-target'" in [c["expression"] for c in policy["spec"]["matchConditions"]]
    assert "request.userInfo.username != 'system:serviceaccount:liveprobe-system:liveprobe'" in [
        v["expression"] for v in policy["spec"]["validations"]]
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    assert {("apps", "deployments"), ("", "services")} <= {
        (group, resource) for rule in rules for group in rule["apiGroups"] for resource in rule["resources"]
        if "UPDATE" in rule["operations"]
    }
