#!/usr/bin/env python3
"""Generate reviewed P15 rules from discovery, never Kubernetes resource objects.

Run from liveprobe: uv run python ../scripts/gen-operator-rbac.py --check
--check compares the checked-in rules with live discovery (no writes).
--static --check validates checked-in rules offline; --discovery-json accepts a
JSON array of APIResourceList fixtures. Only the marked rules block is generated;
identities, token declarations and admission policy remain hand-reviewed YAML.
The surface is intentionally narrower than the original all-non-Secret brief.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import yaml

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "deploy/base/liveprobe/operator-executor.yaml"
START = "# BEGIN GENERATED OPERATOR RULES\n"
END = "# END GENERATED OPERATOR RULES"
VERBS = ("get", "list", "watch", "create", "update", "patch", "delete")
# No reviewed write policy yet: deny at RBAC as well as admission. In particular,
# local/hostPath PVs and CSI StorageClass parameters can expose node/Secret data.
READ_ONLY = {("", "namespaces"), ("", "persistentvolumes"),
             ("", "persistentvolumeclaims"), ("storage.k8s.io", "storageclasses")}


def permitted_verbs(group, resource):
    if (group, resource) in READ_ONLY:
        return VERBS[:3]
    if (group, resource) == ("", "nodes"):
        return ("get", "list", "watch", "update", "patch")
    return VERBS


# A positive allowlist prevents new CRDs/controllers or similarly named resources
# from silently obtaining credentials, privilege or admission-management access.
SURFACE = {
    "v1": {"configmaps": "ConfigMap", "pods": "Pod", "services": "Service", "endpoints": "Endpoints",
           "nodes": "Node", "namespaces": "Namespace", "persistentvolumes": "PersistentVolume",
           "persistentvolumeclaims": "PersistentVolumeClaim", "replicationcontrollers": "ReplicationController",
           "resourcequotas": "ResourceQuota", "limitranges": "LimitRange"},
    "apps/v1": {"deployments": "Deployment", "statefulsets": "StatefulSet", "daemonsets": "DaemonSet", "replicasets": "ReplicaSet"},
    "batch/v1": {"jobs": "Job", "cronjobs": "CronJob"},
    "networking.k8s.io/v1": {"ingresses": "Ingress", "ingressclasses": "IngressClass", "networkpolicies": "NetworkPolicy"},
    "autoscaling/v1": {"horizontalpodautoscalers": "HorizontalPodAutoscaler"},
    "autoscaling/v2": {"horizontalpodautoscalers": "HorizontalPodAutoscaler"},
    "policy/v1": {"poddisruptionbudgets": "PodDisruptionBudget"},
    "storage.k8s.io/v1": {"storageclasses": "StorageClass"},
}


def generate_rules(discovery_documents):
    """Pure deterministic function; never mutate discovery or invoke kubectl."""
    discovered = {}
    for doc in discovery_documents:
        version = doc.get("groupVersion", "")
        permitted = SURFACE.get(version, {})
        group = version.rsplit("/", 1)[0] if "/" in version else ""
        for resource in doc.get("resources", []):
            name = resource.get("name", "")
            if name not in permitted or resource.get("kind") != permitted[name]:
                continue
            allowed = permitted_verbs(group, name)
            verbs = set(resource.get("verbs", [])) & set(allowed)
            discovered.setdefault((group, name), set()).update(verbs)
    return [{"apiGroups": [group], "resources": [name], "verbs": [v for v in VERBS if v in verbs]}
            for (group, name), verbs in sorted(discovered.items()) if verbs]


def validate_rules(rules):
    """Reject wildcards, extra keys, escalation and unknown/unreviewed resources."""
    permitted = {(version.rsplit("/", 1)[0] if "/" in version else "", resource)
                 for version, resources in SURFACE.items() for resource in resources}
    seen = set()
    if not isinstance(rules, list) or not rules:
        raise ValueError("operator rules must be a nonempty list")
    for rule in rules:
        if set(rule) != {"apiGroups", "resources", "verbs"}:
            raise ValueError("unexpected rule fields")
        if len(rule["apiGroups"]) != 1 or len(rule["resources"]) != 1:
            raise ValueError("one explicit group/resource per rule required")
        target = (rule["apiGroups"][0], rule["resources"][0])
        if target not in permitted or target in seen:
            raise ValueError("unreviewed or duplicate resource")
        seen.add(target)
        allowed = set(permitted_verbs(*target))
        if not rule["verbs"] or set(rule["verbs"]) - allowed or len(set(rule["verbs"])) != len(rule["verbs"]):
            raise ValueError("unreviewed or duplicate verb")


def read_discovery(context):
    """Only discovery GET endpoints; never Secret objects or token subresources."""
    def raw(path):
        result = subprocess.run(["kubectl", "--context", context, "get", "--raw", path],
                                check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    documents = [raw("/api/v1")]
    groups = raw("/apis")
    versions = {v["groupVersion"] for g in groups["groups"] for v in g["versions"]
                if v["groupVersion"] in SURFACE}
    documents.extend(raw("/apis/" + version) for version in sorted(versions))
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="rancher-desktop")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true", help="read/compare only; never rewrite")
    parser.add_argument("--static", action="store_true", help="offline structural check (requires --check)")
    parser.add_argument("--discovery-json", type=Path, help="offline JSON array of APIResourceList fixtures")
    args = parser.parse_args(argv)
    if args.static and (not args.check or args.discovery_json):
        parser.error("--static requires --check and cannot use --discovery-json")
    try:
        text = args.manifest.read_text()
        docs = list(yaml.safe_load_all(text))
        role = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "liveprobe-operator-executor")
        if args.static:
            validate_rules(role["rules"])
            print(f"PASS static rules: {len(role['rules'])} explicit group/resource rules (no discovery comparison)")
            return 0
        source = json.loads(args.discovery_json.read_text()) if args.discovery_json else read_discovery(args.context)
        rules = generate_rules(source)
        validate_rules(rules)
        if args.check:
            validate_rules(role["rules"])
            if role["rules"] != rules:
                print("FAIL discovery drift: regenerate and review operator rules", file=sys.stderr)
                return 1
            print(f"PASS discovery match: {len(rules)} explicit group/resource rules; no objects read or changed")
            return 0
        if text.count(START) != 1 or text.count(END) != 1:
            raise ValueError("unique generated rules markers required")
        prefix, rest = text.split(START)
        _, suffix = rest.split(END)
        block = yaml.safe_dump({"rules": rules}, sort_keys=False)
        args.manifest.write_text(prefix + START + block + END + suffix)
        print(f"Generated {len(rules)} explicit rules in {args.manifest}; no cluster objects changed")
        return 0
    except (ValueError, KeyError, StopIteration, OSError, subprocess.SubprocessError) as exc:
        # Do not echo external API responses (nor kubeconfig/auth errors).
        print(f"FAIL generator: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
