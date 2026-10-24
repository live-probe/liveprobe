"""Rendered Kubernetes authorization contract, no cluster access required."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def objects():
    data = subprocess.check_output(["kubectl", "kustomize", str(ROOT / "deploy/base")], text=True)
    return list(yaml.safe_load_all(data))


def find(items, kind, name, ns=None):
    return next(o for o in items if o["kind"] == kind and o["metadata"]["name"] == name
                and (ns is None or o["metadata"].get("namespace") == ns))


def test_rancher_desktop_kan111_images_are_digest_pinned():
    overlay = ROOT / "deploy/overlays/rancher-desktop"
    data = subprocess.check_output(["kubectl", "kustomize", str(overlay)], text=True)
    items = list(yaml.safe_load_all(data))
    for ns, name in (("liveprobe-system", "rollback-controller"),
                     ("liveprobe-system", "liveprobe-api"),
                     ("liveprobe-target", "inventory"),
                     ("liveprobe-target", "order")):
        pod = find(items, "Deployment", name, ns)["spec"]["template"]["spec"]
        for c in pod.get("initContainers", []) + pod["containers"]:
            image = c["image"]
            assert image.count("@sha256:") == 1, (name, image)
            assert len(image.split("@sha256:", 1)[1]) == 64, (name, image)
    api = find(items, "Deployment", "liveprobe-api", "liveprobe-system")
    urls = [e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] == "ROLLBACK_CONTROLLER_URL"]
    assert urls == [{"name": "ROLLBACK_CONTROLLER_URL",
                     "value": "https://rollback-controller.liveprobe-system.svc:443/undo"}]


def test_named_rollback_and_denied_api_rights():
    items = objects()
    role = find(items, "Role", "rollback-controller")
    assert role["metadata"]["namespace"] == "liveprobe-target"
    assert role["rules"] == [
        {"apiGroups": ["apps"], "resources": ["deployments"],
         "resourceNames": ["inventory", "order"], "verbs": ["get", "patch"]},
        {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["list"]},
    ]
    binding = find(items, "RoleBinding", "rollback-controller")
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "rollback-controller",
                                     "namespace": "liveprobe-system"}]
    reader = find(items, "ClusterRole", "liveprobe-reader")
    assert not any("pods/log" in rule["resources"] or any(v in rule["verbs"] for v in ("patch", "update"))
                   or "secrets" in rule["resources"] for rule in reader["rules"])
    # P14 adds one namespace-scoped read-only binding, not a write identity.
    api_bindings = [o for o in items if o["kind"] == "RoleBinding"
                    and any(s.get("name") == "liveprobe" for s in o["subjects"])]
    assert [b["metadata"]["name"] for b in api_bindings] == ["liveprobe-config-reader"]
    assert api_bindings[0]["metadata"]["namespace"] == "liveprobe-target"
    assert api_bindings[0]["roleRef"]["kind"] == "Role"
    config_reader = find(items, "Role", api_bindings[0]["roleRef"]["name"], "liveprobe-target")
    assert config_reader["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list"]}]
    guard = find(items, "ValidatingAdmissionPolicy", "liveprobe-deny-target-writes")
    assert guard["spec"]["failurePolicy"] == "Fail"
    webhook = find(items, "ValidatingWebhookConfiguration", "rollback-controller-image-guard")["webhooks"][0]
    assert webhook["failurePolicy"] == "Fail"
    assert "rollback-controller'" in webhook["matchConditions"][0]["expression"]
    assert webhook["clientConfig"]["service"]["path"] == "/validate"
    assert "caBundle" not in webhook["clientConfig"]  # intentionally unprovisioned, fail closed
    api = find(items, "Deployment", "liveprobe-api")
    env = api["spec"]["template"]["spec"]["containers"][0]["env"]
    urls = [e for e in env if e.get("name") == "ROLLBACK_CONTROLLER_URL"]
    # Exactly one entry: a duplicate later entry silently overrides the HTTPS URL (KAN-111).
    assert urls == [{"name": "ROLLBACK_CONTROLLER_URL",
                     "value": "https://rollback-controller.liveprobe-system.svc:443/undo"}]
    api_policy = find(items, "NetworkPolicy", "liveprobe-api", "liveprobe-system")
    to_controller = [r for r in api_policy["spec"]["egress"]
                     if any((p.get("podSelector") or {}).get("matchLabels") == {"app": "rollback-controller"}
                            for p in r.get("to", []))]
    assert [p["port"] for r in to_controller for p in r["ports"]] == [8443]
    volumes = {v["name"]: v for v in api["spec"]["template"]["spec"]["volumes"]}
    token = volumes["rollback-caller"]["projected"]["sources"][0]["serviceAccountToken"]
    assert token["audience"] == "liveprobe-rollback" and token["expirationSeconds"] == 600
    assert volumes["rollback-ca"]["configMap"]["name"] == "rollback-ca"
    assert volumes["rollback-grant"]["secret"]["secretName"] == "rollback-action-grant"
    assert any(m["name"] == "rollback-grant" and m["readOnly"] for m in
               api["spec"]["template"]["spec"]["containers"][0]["volumeMounts"])
    controller = find(items, "Deployment", "rollback-controller")
    controller_spec = controller["spec"]["template"]["spec"]
    assert next(v for v in controller_spec["volumes"] if v["name"] == "rollback-grant")["secret"]["secretName"] == "rollback-action-grant"
    assert any(m["name"] == "rollback-grant" and m["readOnly"] for m in controller_spec["containers"][0]["volumeMounts"])
    assert [p["containerPort"] for p in controller["spec"]["template"]["spec"]["containers"][0]["ports"]] == [8443]
    service = find(items, "Service", "rollback-controller")
    assert [p["port"] for p in service["spec"]["ports"]] == [443]
    token_role = find(items, "ClusterRole", "rollback-controller-token-review")
    assert token_role["rules"] == [{"apiGroups": ["authentication.k8s.io"],
                                    "resources": ["tokenreviews"], "verbs": ["create"]}]
    assert find(items, "ClusterRoleBinding", "rollback-controller-token-review")["subjects"] == [
        {"kind": "ServiceAccount", "name": "rollback-controller", "namespace": "liveprobe-system"}]
