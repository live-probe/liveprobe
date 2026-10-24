"""KAN-96 regression: scripts/check-chaos-overlay.py.

Runs the real renders (kubectl kustomize) and proves the check both passes on the repo and detects the
leak/omission classes it exists for, by mutating copies of the real render.
"""
import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/check-chaos-overlay.py"
spec = importlib.util.spec_from_file_location("check_chaos_overlay", CHECK)
cco = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cco)


@pytest.fixture(scope="module")
def chaos():
    return cco.render(ROOT, cco.CHAOS)


@pytest.fixture(scope="module")
def default():
    return cco.render(ROOT, "deploy/overlays/rancher-desktop")


def dep(docs, name):
    return cco.find(docs, "Deployment", "liveprobe-target", name)


def test_repo_passes():
    r = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "chaos overlay check OK" in r.stdout


@pytest.mark.parametrize("overlay", cco.NON_CHAOS)
def test_non_chaos_overlays_clean(overlay):
    assert cco.check_non_chaos(overlay, cco.render(ROOT, overlay)) == []


def test_chaos_render_clean(chaos):
    assert cco.check_chaos(chaos) == []


def test_default_leak_of_profile_detected(default):
    docs = copy.deepcopy(default)
    dep(docs, "order")["spec"]["template"]["spec"]["containers"][0].setdefault("env", []).append(
        {"name": "SPRING_PROFILES_ACTIVE", "value": "chaos"})
    assert any("SPRING_PROFILES_ACTIVE" in f for f in cco.check_non_chaos("x", docs))


def test_default_leak_of_secret_ref_detected(default):
    docs = copy.deepcopy(default)
    dep(docs, "inventory")["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "X", "valueFrom": {"secretKeyRef": {"name": "order-db", "key": "password"}}})
    assert any("chaos Secrets" in f for f in cco.check_non_chaos("x", docs))


def test_extra_ingress_detected(chaos):
    docs = copy.deepcopy(chaos) + [{"kind": "Ingress", "metadata": {"name": "chaos", "namespace": "liveprobe-target"},
                                    "spec": {}}]
    assert any("Ingress set" in f for f in cco.check_chaos(docs))


def test_nodeport_detected(chaos):
    docs = copy.deepcopy(chaos)
    cco.find(docs, "Service", "liveprobe-target", "order")["spec"]["type"] = "NodePort"
    assert any("NodePort" in f for f in cco.check_chaos(docs))


def test_literal_token_detected(chaos):
    docs = copy.deepcopy(chaos)
    for e in dep(docs, "order")["spec"]["template"]["spec"]["containers"][0]["env"]:
        if e["name"] == "CHAOS_OPERATOR_TOKEN":
            e.pop("valueFrom")
            e["value"] = "placeholder"
    fails = cco.check_chaos(docs)
    assert any("literal value" in f for f in fails)
    assert all("placeholder" not in f for f in fails)


def test_missing_order_netpol_detected(chaos):
    docs = [d for d in copy.deepcopy(chaos) if d["metadata"]["name"] != "order-postgres-egress"]
    assert any("chaos NetworkPolicies" in f for f in cco.check_chaos(docs))


def test_broadened_netpol_detected(chaos):
    docs = copy.deepcopy(chaos)
    np = cco.find(docs, "NetworkPolicy", "liveprobe-system", "postgres-order-ingress")
    np["spec"]["ingress"][0]["from"][0]["podSelector"] = {}
    assert any("postgres-order-ingress" in f for f in cco.check_chaos(docs))


def test_gateway_internal_route_detected(chaos):
    docs = copy.deepcopy(chaos)
    cm = cco.find(docs, "ConfigMap", "liveprobe-target", "order-gateway")
    cm["data"]["nginx.conf"] = cm["data"]["nginx.conf"].replace("location / {", "location /internal/ {")
    assert any("only /api/orders/" in f for f in cco.check_chaos(docs))


def test_unpinned_gateway_image_detected(chaos):
    docs = copy.deepcopy(chaos)
    for c in dep(docs, "loadgen")["spec"]["template"]["spec"]["containers"]:
        if c["name"] == "order-gateway":
            c["image"] = "nginxinc/nginx-unprivileged:1.27-alpine"
    assert any("digest-pinned" in f for f in cco.check_chaos(docs))
    for c in dep(docs, "loadgen")["spec"]["template"]["spec"]["containers"]:
        if c["name"] == "order-gateway":
            c["image"] = "nginx:latest@sha256:" + "0" * 64
    assert any("digest-pinned" in f for f in cco.check_chaos(docs))


def test_missing_state_volume_and_probe_detected(chaos):
    docs = copy.deepcopy(chaos)
    spec = dep(docs, "inventory")["spec"]["template"]["spec"]
    spec.pop("volumes")
    spec["containers"][0]["readinessProbe"]["periodSeconds"] = 5
    fails = cco.check_chaos(docs)
    assert any("chaos-state emptyDir" in f for f in fails)
    assert any("readinessProbe" in f for f in fails)
