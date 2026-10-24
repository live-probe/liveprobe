#!/usr/bin/env python3
"""Assert that a rendered overlay carries exactly the values of its platform.yaml (KAN-61).

usage: check-overlay.py <overlay-dir> <rendered.yaml>
"""
import sys

import yaml

overlay, rendered = sys.argv[1:3]
p = yaml.safe_load(open(f"{overlay}/platform.yaml"))["data"]
kust = yaml.safe_load(open(f"{overlay}/kustomization.yaml"))
docs = [d for d in yaml.safe_load_all(open(rendered)) if d]
fails = []


def by(kind):
    return [d for d in docs if d["kind"] == kind]


if any(d["kind"] == "ConfigMap" and d["metadata"]["name"] == "liveprobe-platform" for d in docs):
    fails.append("local-config ConfigMap liveprobe-platform leaked into the render")

ing = by("Ingress")
if len(ing) != 3:
    fails.append(f"expected 3 Ingresses, got {len(ing)}")
for d in ing:
    n = d["metadata"]["name"]
    if d["spec"].get("ingressClassName") != p["ingressClassName"]:
        fails.append(f"Ingress {n}: ingressClassName {d['spec'].get('ingressClassName')}")
    want = p["grafanaHost"] if n == "grafana" else p["liveprobeHost"]
    if d["spec"]["rules"][0]["host"] != want:
        fails.append(f"Ingress {n}: host {d['spec']['rules'][0]['host']} != {want}")

pvcs = by("PersistentVolumeClaim")
for d in pvcs:
    if d["spec"].get("storageClassName") != p["storageClassName"]:
        fails.append(f"PVC {d['metadata']['name']}: storageClassName {d['spec'].get('storageClassName')}")
for d in by("StatefulSet"):
    for t in d["spec"].get("volumeClaimTemplates", []):
        if t["spec"].get("storageClassName") != p["storageClassName"]:
            fails.append(f"StatefulSet {d['metadata']['name']}/{t['metadata']['name']}: storageClassName")

nps = by("NetworkPolicy")
for d in nps:
    ns, n = d["metadata"]["namespace"], d["metadata"]["name"]
    if n == "allow-ingress-controller":
        frm = d["spec"]["ingress"][0]["from"][0]
        if frm["namespaceSelector"]["matchLabels"].get("kubernetes.io/metadata.name") != p["ingressNamespace"]:
            fails.append(f"NetworkPolicy {ns}/{n}: ingress namespace")
        if frm["podSelector"]["matchLabels"].get("app.kubernetes.io/name") != p["ingressPodName"]:
            fails.append(f"NetworkPolicy {ns}/{n}: ingress pod label")
    if d["metadata"].get("labels", {}).get("liveprobe.io/egress") == "apiserver":
        if d["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] != p["apiserverCIDR"]:
            fails.append(f"NetworkPolicy {ns}/{n}: apiserver cidr")
    if d["metadata"].get("labels", {}).get("liveprobe.io/egress") == "kubelet":
        if d["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] != p["nodeCIDR"]:
            fails.append(f"NetworkPolicy {ns}/{n}: kubelet cidr")
for ns in ("liveprobe-system", "liveprobe-observability", "liveprobe-target"):
    if not any(d["metadata"]["namespace"] == ns and d["metadata"]["name"] == "default-deny-all" for d in nps):
        fails.append(f"missing default-deny-all in {ns}")

# every container image resolved through the overlay images: list (registry/tag rewrite)
images = {i["name"]: i for i in kust.get("images", [])}
seen = []
for d in docs:
    spec = d.get("spec", {}).get("template", {}).get("spec", {})
    for c in spec.get("initContainers", []) + spec.get("containers", []):
        seen.append(c["image"])
for img in seen:
    for src, rule in images.items():
        new = rule.get("newName", src)
        if img.split(":")[0] == new and "newTag" in rule and img != f"{new}:{rule['newTag']}":
            fails.append(f"image {img} does not carry overlay tag {rule['newTag']}")
    reg = p.get("imageRegistry", "")
    if reg and not img.startswith(reg.rstrip("/") + "/"):
        fails.append(f"image {img} not rewritten to overlay registry {reg}")

tokens = {d["metadata"]["name"]: d["spec"]["template"]["spec"].get("automountServiceAccountToken", True)
          for d in docs if d["kind"] in ("Deployment", "StatefulSet")}
api_clients = {"liveprobe-api", "rollback-controller", "prometheus", "alloy", "kube-state-metrics"}
for n, auto in tokens.items():
    if (n in api_clients) != (auto is not False):
        fails.append(f"{n}: automountServiceAccountToken={auto} (API clients: {sorted(api_clients)})")

print(f"   ingressClass={p['ingressClassName']} storageClass={p['storageClassName']} hosts={p['liveprobeHost']},{p['grafanaHost']} "
      f"ingressCtl={p['ingressNamespace']}/{p['ingressPodName']} apiserver={p['apiserverCIDR']} "
      f"ingresses={len(ing)} pvcs={len(pvcs)} netpols={len(nps)} images={len(seen)}")
print("   images: " + " ".join(sorted(set(seen))))
if fails:
    print("OVERLAY CHECK FAIL:\n  " + "\n  ".join(fails), file=sys.stderr)
    sys.exit(1)
print(f"   overlay check OK: {overlay}")
