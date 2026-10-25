"""KAN-103: image-only rollback and admission adversarial contract."""
import copy

import pytest

from app.rollback_controller import (CONTROLLER_USER, Rejected, plan_undo,
                                     trusted_images, validate_admission)
from conftest import mk_dep, mk_rs

SAFE = "registry.example/inventory@sha256:" + "a" * 64
ORDER = "registry.example/order@sha256:" + "b" * 64
BAD = "registry.example/inventory:broken"
APPROVED = {"inventory": SAFE, "order": ORDER}


def resources():
    dep = mk_dep("inventory", 2, BAD)
    dep["metadata"].update(uid="uid-inventory", resourceVersion="11")
    rs = mk_rs("inventory-old", "inventory", 1, SAFE)
    rs["metadata"]["ownerReferences"][0].update(uid="uid-inventory", controller=True)
    return dep, rs


def review(old, new, user=CONTROLLER_USER, name="inventory"):
    return {"request": {"uid": "request-1", "namespace": "liveprobe-target",
                        "operation": "UPDATE", "name": name,
                        "resource": {"group": "apps", "resource": "deployments"},
                        "userInfo": {"username": user},
                        "oldObject": old, "object": new}}


def test_exact_previous_revision_produces_cas_image_only_patch():
    dep, rs = resources()
    ops, revision, source = plan_undo(dep, [rs], "inventory", SAFE)
    assert (revision, source) == (1, "inventory-old")
    assert ops == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "11"},
        {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": BAD},
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": SAFE},
    ]
    new = copy.deepcopy(dep)
    new["spec"]["template"]["spec"]["containers"][0]["image"] = SAFE
    assert validate_admission(review(dep, new), APPROVED)["response"]["allowed"]


def test_api_kube_client_has_no_direct_patch_and_rejects_unconfigured_controller(monkeypatch):
    from app.k8s import Kube
    from app.config import AppConfig
    monkeypatch.setattr("app.config.get_app_config", lambda: AppConfig(target_namespace="liveprobe-target"))
    assert not hasattr(Kube, "patch_deployment_json")
    monkeypatch.delenv("ROLLBACK_CONTROLLER_URL", raising=False)
    kube = Kube.__new__(Kube)
    with pytest.raises(RuntimeError, match="not configured"):
        kube.request_rollback("liveprobe-target", "inventory")
    with pytest.raises(ValueError, match="not allowlisted"):
        kube.request_rollback("liveprobe-system", "inventory")


@pytest.mark.parametrize("tamper", [
    lambda d, r: r["metadata"]["ownerReferences"][0].update(uid="other"),
    lambda d, r: r["metadata"]["ownerReferences"][0].update(controller=False),
    lambda d, r: r["metadata"]["annotations"].update({"deployment.kubernetes.io/revision": "3"}),
    lambda d, r: r["spec"]["template"]["spec"]["containers"][0].update(image="attacker.example:latest"),
    lambda d, r: r["spec"]["template"]["spec"]["containers"][0].update(env=[{"name": "LEAK", "valueFrom": {"secretKeyRef": {"name": "token", "key": "value"}}}]),
    lambda d, r: r["spec"]["template"]["spec"].update(serviceAccountName="privileged"),
])
def test_provenance_and_template_negative(tamper):
    dep, rs = resources()
    tamper(dep, rs)
    with pytest.raises(Rejected):
        plan_undo(dep, [rs], "inventory", SAFE)


def test_newer_non_image_revision_cannot_be_skipped():
    dep, _ = resources()
    rs = mk_rs("inventory-newer", "inventory", 2, SAFE)
    rs["metadata"]["ownerReferences"][0].update(uid="uid-inventory", controller=True)
    rs["spec"]["template"]["spec"].update(serviceAccountName="other")
    dep["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
    with pytest.raises(Rejected):
        plan_undo(dep, [resources()[1], rs], "inventory", SAFE)


def test_token_review_rejects_wrong_principal_or_audience():
    from types import SimpleNamespace
    from app.rollback_controller import authenticate, AUDIENCE, CALLER_USER
    class Auth:
        def __init__(self, user, audiences, authenticated=True):
            self.status = SimpleNamespace(authenticated=authenticated, error=None,
                                          user=SimpleNamespace(username=user), audiences=audiences)
        def create_token_review(self, payload):
            assert payload["spec"]["audiences"] == [AUDIENCE]
            return SimpleNamespace(status=self.status)
    for header, auth in ((None, Auth(CALLER_USER, [AUDIENCE])),
                         ("Bearer wrong", Auth("system:serviceaccount:liveprobe-system:other", [AUDIENCE])),
                         ("Bearer wrong", Auth(CALLER_USER, ["kubernetes.default.svc"])),
                         ("Bearer wrong", Auth(CALLER_USER, [AUDIENCE], False))):
        with pytest.raises(Rejected, match="not authorized"):
            authenticate(auth, header)
    authenticate(Auth(CALLER_USER, [AUDIENCE]), "Bearer projected-token")


def test_no_unpinned_or_mismatched_target():
    dep, rs = resources()
    with pytest.raises(Rejected):
        plan_undo(dep, [rs], "order", SAFE)
    with pytest.raises(Rejected):
        plan_undo(dep, [rs], "inventory", "registry.example/inventory:dev")
    with pytest.raises(Rejected):
        trusted_images({"ROLLBACK_INVENTORY_IMAGE": "registry.example/inventory:dev", "ROLLBACK_ORDER_IMAGE": ORDER})
    assert trusted_images({"ROLLBACK_INVENTORY_IMAGE": SAFE, "ROLLBACK_ORDER_IMAGE": ORDER}) == APPROVED


@pytest.mark.parametrize("tamper", [
    lambda n: n["spec"]["template"]["spec"]["containers"][0].update(image="evil.example/steal:latest"),
    lambda n: n["spec"]["template"]["spec"]["containers"][0].update(env=[{"name": "LEAK", "valueFrom": {"secretKeyRef": {"name": "token", "key": "value"}}}]),
    lambda n: n["spec"]["template"]["spec"]["containers"][0].update(command=["sh", "-c", "cat /token"]),
    lambda n: n["spec"]["template"]["spec"].update(serviceAccountName="target-token"),
    lambda n: n["spec"]["template"]["spec"].update(volumes=[{"name": "token", "secret": {"secretName": "chaos"}}]),
    lambda n: n["spec"]["template"]["spec"].update(automountServiceAccountToken=True),
    lambda n: n["spec"]["template"]["metadata"]["labels"].update(app="evil"),
    lambda n: n["spec"].update(selector={"matchLabels": {"app": "evil"}}),
    lambda n: n["metadata"].update(labels={"app": "evil"}),
    lambda n: n["spec"].update(replicas=0),
])
def test_admission_rejects_all_other_fields(tamper):
    dep, _ = resources()
    new = copy.deepcopy(dep)
    new["spec"]["template"]["spec"]["containers"][0]["image"] = SAFE
    tamper(new)
    assert not validate_admission(review(dep, new), APPROVED)["response"]["allowed"]


def test_admission_rejects_other_principals_and_targets():
    dep, _ = resources()
    new = copy.deepcopy(dep)
    new["spec"]["template"]["spec"]["containers"][0]["image"] = SAFE
    assert not validate_admission(review(dep, new, "system:serviceaccount:liveprobe-system:liveprobe"), APPROVED)["response"]["allowed"]
    assert not validate_admission(review(dep, new, name="third-party"), APPROVED)["response"]["allowed"]
