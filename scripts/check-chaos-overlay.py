#!/usr/bin/env python3
"""KAN-96 render assertions for the chaos overlay (contract t_21061685 §1.1, §1.6, §2.5, §3, §8).

usage: check-chaos-overlay.py [--repo ROOT]

Renders deploy/base and every overlay with `kubectl kustomize` and asserts:
  * only deploy/overlays/rancher-desktop-chaos carries the chaos profile, the chaos Secret refs
    (inventory-chaos-operator / order-chaos-operator / order-db), the chaos-state emptyDir, CHAOS_STATE_DIR,
    ORDER_DB_* wiring, the order-gateway sidecar/ConfigMap and the chaos NetworkPolicies;
  * the chaos overlay carries all of them exactly as the contract specifies;
  * no overlay renders a Secret object, no Ingress beyond the 3 platform Ingresses (the chaos overlay has exactly
    those 3);
  * no Service of type NodePort/LoadBalancer and no hostPort anywhere.
Prints object names/counts only, never values of Secrets (none are rendered anyway).
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

CHAOS = "deploy/overlays/rancher-desktop-chaos"
NON_CHAOS = ["deploy/base", "deploy/overlays/rancher-desktop", "deploy/overlays/rancher-desktop-observability",
             "deploy/overlays/portable-example"]
CHAOS_SECRETS = {"inventory-chaos-operator", "order-chaos-operator", "order-db"}
CHAOS_LABEL = "liveprobe.io/chaos-overlay"
STATE_DIR = "/var/run/liveprobe-chaos"
GATEWAY_IMAGE_PREFIX = "nginxinc/nginx-unprivileged:1.27-alpine@sha256:"
EXPECTED_INGRESSES = {"liveprobe-api", "liveprobe-dashboard", "grafana"}
ORDER_DB = {
    "ORDER_DB_ENABLED": "true",
    "ORDER_DB_URL": "jdbc:postgresql://postgres.liveprobe-system.svc:5432/order_app",
    "ORDER_DB_USER": "order_app",
    "ORDER_DB_POOL_MAX": "4",
    "ORDER_DB_CONNECTION_TIMEOUT_MS": "1000",
}
READINESS_1111 = {"periodSeconds": 1, "failureThreshold": 1, "successThreshold": 1, "timeoutSeconds": 1}


def render(root, path):
    out = subprocess.run(["kubectl", "kustomize", str(root / path)], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def pod_specs(docs):
    for d in docs:
        spec = d.get("spec", {}).get("template", {}).get("spec")
        if spec:
            yield d, spec


def env_map(c):
    return {e["name"]: e for e in c.get("env", [])}


def secret_refs(docs):
    refs = set()
    for _, spec in pod_specs(docs):
        for c in spec.get("initContainers", []) + spec.get("containers", []):
            for e in c.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    refs.add(ref["name"])
            for ef in c.get("envFrom", []):
                if "secretRef" in ef:
                    refs.add(ef["secretRef"]["name"])
        for v in spec.get("volumes", []):
            if "secret" in v:
                refs.add(v["secret"]["secretName"])
    return refs


def common_checks(name, docs):
    fails = []
    if any(d["kind"] == "Secret" for d in docs):
        fails.append(f"{name}: renders a Secret object")
    ing = {d["metadata"]["name"] for d in docs if d["kind"] == "Ingress"}
    if not ing <= EXPECTED_INGRESSES or (name == CHAOS and ing != EXPECTED_INGRESSES):
        fails.append(f"{name}: Ingress set {sorted(ing)} != {sorted(EXPECTED_INGRESSES)}")
    if any(d["kind"] == "Route" for d in docs):
        fails.append(f"{name}: renders a Route")
    for d in docs:
        if d["kind"] == "Service" and d["spec"].get("type", "ClusterIP") not in ("ClusterIP",):
            fails.append(f"{name}: Service {d['metadata']['name']} type {d['spec'].get('type')}")
    for d, spec in pod_specs(docs):
        if spec.get("hostNetwork"):
            fails.append(f"{name}: {d['metadata']['name']} hostNetwork")
        for c in spec.get("containers", []):
            if any("hostPort" in p for p in c.get("ports", [])):
                fails.append(f"{name}: {d['metadata']['name']}/{c['name']} hostPort")
    return fails


def check_non_chaos(name, docs):
    fails = common_checks(name, docs)
    text = yaml.safe_dump_all(docs)
    for needle in ("SPRING_PROFILES_ACTIVE", "CHAOS_OPERATOR_TOKEN", "CHAOS_STATE_DIR", "chaos-state",
                   "ORDER_DB_", "order_app", "order-gateway", CHAOS_LABEL, "nginx-unprivileged"):
        if needle in text:
            fails.append(f"{name}: contains chaos-only marker {needle!r}")
    leaked = secret_refs(docs) & CHAOS_SECRETS
    if leaked:
        fails.append(f"{name}: references chaos Secrets {sorted(leaked)}")
    return fails


def find(docs, kind, ns, name):
    for d in docs:
        if d["kind"] == kind and d["metadata"].get("namespace") == ns and d["metadata"]["name"] == name:
            return d
    return None


def check_target_container(fails, dep, token_secret, extra_env=()):
    n = dep["metadata"]["name"]
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    env = env_map(c)
    if env.get("SPRING_PROFILES_ACTIVE", {}).get("value") != "chaos":
        fails.append(f"{n}: SPRING_PROFILES_ACTIVE != chaos")
    ref = env.get("CHAOS_OPERATOR_TOKEN", {}).get("valueFrom", {}).get("secretKeyRef", {})
    if ref != {"name": token_secret, "key": "token"}:
        fails.append(f"{n}: CHAOS_OPERATOR_TOKEN not from {token_secret}/token")
    if "value" in env.get("CHAOS_OPERATOR_TOKEN", {}):
        fails.append(f"{n}: CHAOS_OPERATOR_TOKEN has a literal value")
    if env.get("CHAOS_STATE_DIR", {}).get("value") != STATE_DIR:
        fails.append(f"{n}: CHAOS_STATE_DIR != {STATE_DIR}")
    for var, secret, key in extra_env:
        if env.get(var, {}).get("valueFrom", {}).get("secretKeyRef") != {"name": secret, "key": key}:
            fails.append(f"{n}: {var} not from {secret}/{key}")
    vols = {v["name"]: v for v in spec.get("volumes", [])}
    if vols.get("chaos-state", {}).get("emptyDir") != {"sizeLimit": "1Mi"}:
        fails.append(f"{n}: chaos-state emptyDir sizeLimit 1Mi missing")
    mounts = {m["name"]: m["mountPath"] for m in c.get("volumeMounts", [])}
    if mounts.get("chaos-state") != STATE_DIR:
        fails.append(f"{n}: chaos-state not mounted at {STATE_DIR}")
    rp = c.get("readinessProbe", {})
    if rp.get("httpGet", {}).get("path") != "/actuator/health/readiness" or any(
            rp.get(k) != v for k, v in READINESS_1111.items()):
        fails.append(f"{n}: readinessProbe is not /actuator/health/readiness 1/1/1/1")


def check_chaos(docs):
    fails = common_checks(CHAOS, docs)
    tgt = "liveprobe-target"
    inv, order, lg = (find(docs, "Deployment", tgt, n) for n in ("inventory", "order", "loadgen"))
    if not (inv and order and lg):
        return fails + [f"{CHAOS}: missing target Deployments"]
    check_target_container(fails, inv, "inventory-chaos-operator")
    check_target_container(fails, order, "order-chaos-operator", [("ORDER_DB_PASSWORD", "order-db", "password")])
    for dep in (inv, order):
        sel = (find(docs, "Service", tgt, dep["metadata"]["name"]) or {}).get("spec", {}).get("selector")
        if sel != {"app": dep["metadata"]["name"]}:
            fails.append(f"Service {dep['metadata']['name']}: selector changed {sel}")

    cm = find(docs, "ConfigMap", tgt, "order-config")
    for k, v in ORDER_DB.items():
        if (cm or {}).get("data", {}).get(k) != v:
            fails.append(f"order-config: {k} != {v}")

    # loadgen -> gateway sidecar
    cs = {c["name"]: c for c in lg["spec"]["template"]["spec"]["containers"]}
    gw = cs.get("order-gateway")
    if not gw:
        fails.append("loadgen: order-gateway sidecar missing")
    else:
        digest = gw["image"][len(GATEWAY_IMAGE_PREFIX):]
        if not gw["image"].startswith(GATEWAY_IMAGE_PREFIX) or len(digest) != 64 or \
                any(ch not in "0123456789abcdef" for ch in digest):
            fails.append(f"order-gateway: image not digest-pinned {GATEWAY_IMAGE_PREFIX}")
        if gw.get("resources", {}).get("limits", {}).get("memory") != "32Mi":
            fails.append("order-gateway: memory limit != 32Mi")
        if gw.get("ports"):
            fails.append("order-gateway: must not declare container ports")
    args = " ".join(cs["loadgen"].get("args", []))
    if "http://127.0.0.1:8088/api/orders/check?item=$item" not in args or "--max-time 5" not in args \
            or "sleep 0.5" not in args or "loadgen item=$item status=$code n=$i" not in args:
        fails.append("loadgen: loop does not call the gateway every 0.5 s with --max-time 5 and base log format")
    if "order.liveprobe-target.svc" in args:
        fails.append("loadgen: loop still bypasses the gateway")
    gcm = find(docs, "ConfigMap", tgt, "order-gateway")
    conf = (gcm or {}).get("data", {}).get("nginx.conf", "")
    for needle in ("listen 127.0.0.1:8088;", "location /api/orders/ {", "server order.liveprobe-target.svc:8080;",
                   "proxy_connect_timeout 1s;", "proxy_read_timeout 5s;", "proxy_next_upstream off;",
                   "return 404;",
                   "gw status=$status upstream_status=$upstream_status upstream_addr=$upstream_addr "
                   "rt=$request_time time=$time_iso8601"):
        if needle not in conf:
            fails.append(f"order-gateway nginx.conf: missing {needle!r}")
    if conf.count("location ") != 2 or "/internal" in conf:
        fails.append("order-gateway nginx.conf: only /api/orders/ may be proxied")

    # NetworkPolicies: exactly the two contract additions carry the chaos label
    nps = {(d["metadata"]["namespace"], d["metadata"]["name"]): d for d in docs if d["kind"] == "NetworkPolicy"}
    labelled = {k for k, d in nps.items() if d["metadata"].get("labels", {}).get(CHAOS_LABEL) == "true"}
    want = {("liveprobe-system", "postgres-order-ingress"), ("liveprobe-target", "order-postgres-egress")}
    if labelled != want:
        fails.append(f"chaos NetworkPolicies {sorted(labelled)} != {sorted(want)}")
    else:
        ing = nps[("liveprobe-system", "postgres-order-ingress")]["spec"]
        if ing["podSelector"] != {"matchLabels": {"app": "postgres"}} or ing["ingress"] != [{
                "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": tgt}},
                          "podSelector": {"matchLabels": {"app": "order"}}}],
                "ports": [{"protocol": "TCP", "port": 5432}]}]:
            fails.append("postgres-order-ingress: not order -> postgres:5432 only")
        eg = nps[("liveprobe-target", "order-postgres-egress")]["spec"]
        if eg["podSelector"] != {"matchLabels": {"app": "order"}} or eg["egress"] != [{
                "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "liveprobe-system"}},
                        "podSelector": {"matchLabels": {"app": "postgres"}}}],
                "ports": [{"protocol": "TCP", "port": 5432}]}]:
            fails.append("order-postgres-egress: not order -> postgres:5432 only")
    if gcm is None:
        fails.append("order-gateway ConfigMap missing")
    elif gcm["metadata"].get("labels", {}).get(CHAOS_LABEL) != "true":
        fails.append(f"order-gateway ConfigMap: missing {CHAOS_LABEL} label")
    refs = secret_refs(docs) & CHAOS_SECRETS
    if refs != CHAOS_SECRETS:
        fails.append(f"chaos Secret refs {sorted(refs)} != {sorted(CHAOS_SECRETS)}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    root = Path(ap.parse_args().repo)
    fails = []
    renders = {}
    for p in NON_CHAOS + [CHAOS]:
        renders[p] = render(root, p)
    base_names = {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])
                  for d in renders["deploy/overlays/rancher-desktop"]}
    for p in NON_CHAOS:
        f = check_non_chaos(p, renders[p])
        fails += f
        print(f"   {p}: objects={len(renders[p])} {'OK' if not f else 'FAIL'}")
    f = check_chaos(renders[CHAOS])
    added = sorted(f"{k}/{ns}/{n}" for k, ns, n in
                   {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]) for d in renders[CHAOS]}
                   - base_names)
    fails += f
    print(f"   {CHAOS}: objects={len(renders[CHAOS])} added_vs_rancher-desktop={added} {'OK' if not f else 'FAIL'}")
    if fails:
        print("CHAOS OVERLAY CHECK FAIL:\n  " + "\n  ".join(fails), file=sys.stderr)
        return 1
    print("   chaos overlay check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
