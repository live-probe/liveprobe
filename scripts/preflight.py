#!/usr/bin/env python3
"""Preflight for scripts/install.sh: the overlay's platform parameters must match the target cluster.

Checks (read-only, operator kubeconfig):
  * IngressClass `ingressClassName` exists
  * StorageClass `storageClassName` exists
  * ingress controller pods exist in `ingressNamespace` with app.kubernetes.io/name=`ingressPodName`
  * every Kubernetes API endpoint (EndpointSlice of Service default/kubernetes) is inside `apiserverCIDR`
  * cluster DNS pods carry the label k8s-app=kube-dns in kube-system (DNS NetworkPolicy allow-list)
Exits 1 with a list of mismatches; a mismatched NetworkPolicy parameter would otherwise silently cut
ingress, DNS or API access once default-deny policies are applied.
"""
import argparse
import ipaddress
import json
import subprocess
import sys

import yaml


def kubectl(ctx, *args):
    out = subprocess.run(["kubectl", "--context", ctx, *args, "-o", "json"], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--overlay", required=True)
    a = ap.parse_args()

    platform = yaml.safe_load(open(f"{a.overlay}/platform.yaml"))["data"]
    fails = []

    if kubectl(a.context, "get", "ingressclass", platform["ingressClassName"]) is None:
        fails.append(f"IngressClass {platform['ingressClassName']} not found")
    if kubectl(a.context, "get", "storageclass", platform["storageClassName"]) is None:
        fails.append(f"StorageClass {platform['storageClassName']} not found")

    pods = kubectl(a.context, "-n", platform["ingressNamespace"], "get", "pods",
                   "-l", f"app.kubernetes.io/name={platform['ingressPodName']}")
    if not pods or not pods["items"]:
        fails.append(f"no ingress controller pods app.kubernetes.io/name={platform['ingressPodName']} "
                     f"in namespace {platform['ingressNamespace']}")

    dns = kubectl(a.context, "-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns")
    if not dns or not dns["items"]:
        fails.append("no kube-system pods labelled k8s-app=kube-dns (DNS egress policy would not match)")

    net = ipaddress.ip_network(platform["apiserverCIDR"], strict=False)
    slices = kubectl(a.context, "-n", "default", "get", "endpointslices", "-l", "kubernetes.io/service-name=kubernetes")
    addrs = [ad for s in (slices or {}).get("items", []) for ep in s.get("endpoints") or [] for ad in ep["addresses"]]
    if not addrs:
        fails.append("could not read API server EndpointSlice")
    for ad in addrs:
        if ipaddress.ip_address(ad) not in net:
            fails.append(f"API server endpoint {ad} is outside apiserverCIDR {net}")

    summary = {k: platform[k] for k in ("ingressClassName", "storageClassName", "ingressNamespace",
                                         "ingressPodName", "apiserverCIDR")}
    summary["apiserverEndpoints"] = addrs
    print("preflight:", json.dumps(summary, sort_keys=True))
    if fails:
        print("PREFLIGHT FAIL:\n  " + "\n  ".join(fails), file=sys.stderr)
        return 1
    print("preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
