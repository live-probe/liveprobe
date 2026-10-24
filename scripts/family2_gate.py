#!/usr/bin/env python3
"""KAN-122: fail-closed, single-inventory Family 2 operator harness.

Default is read-only. --execute is deliberately unavailable. The isolated
transaction below requires an injected operator/observer and is never wired to
live credentials or fault injection by this CLI.
"""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = "liveprobe-target"
OBS = "liveprobe-observability"
CONTEXT = "rancher-desktop"
OBJECTS = ((TARGET, "deployment", "inventory"), (OBS, "configmap", "prometheus-config"))
LOCK = ROOT / "backups" / "family2" / "operator.lock"
KSM_OOM_METRIC = "kube_pod_container_status_last_terminated_reason"


class GateError(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise GateError(reason)


def run(*args, namespace=TARGET, input_text=None, timeout=30, allowed=(0,)):
    # Never send subprocess stderr/stdout to diagnostics; Kubernetes errors can
    # include sensitive object data. Every call uses the explicit context.
    proc = subprocess.run(["kubectl", "--context", CONTEXT, "-n", namespace, *args],
                          input=input_text, text=True, capture_output=True, timeout=timeout)
    require(proc.returncode in allowed, "kubectl operation failed (output suppressed)")
    return proc.stdout


def obj(namespace, kind, name):
    data = json.loads(run("get", kind, name, "-o", "json", namespace=namespace))
    require(data["metadata"]["namespace"] == namespace and data["metadata"]["name"] == name,
            "object identity mismatch")
    return data


def deployment_ready(dep):
    spec = dep["spec"]
    state = dep.get("status", {})
    return (spec.get("replicas") == 1 and state.get("observedGeneration", 0) >= dep["metadata"]["generation"]
            and state.get("readyReplicas") == 1 and state.get("updatedReplicas") == 1
            and state.get("availableReplicas") == 1)


def inventory_container(dep):
    containers = dep["spec"]["template"]["spec"]["containers"]
    found = [c for c in containers if c["name"] == "inventory"]
    require(len(containers) == len(found) == 1, "inventory container shape changed")
    c = found[0]
    require(c["resources"]["limits"]["memory"] == "512Mi", "inventory limit is not 512Mi")
    require(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", c["image"]), "inventory image not digest-pinned")
    return c


def profile(dep):
    env = inventory_container(dep).get("env", [])
    matches = [e for e in env if e["name"] == "SPRING_PROFILES_ACTIVE"]
    require(len(matches) <= 1 and all(set(e) == {"name", "value"} for e in matches), "ambiguous profile")
    return matches[0]["value"] if matches else None


def pod():
    pods = json.loads(run("get", "pods", "-l", "app=inventory", "-o", "json"))["items"]
    require(len(pods) == 1, "inventory Pod count changed")
    p = pods[0]
    require(p["metadata"].get("deletionTimestamp") is None and p["status"]["phase"] == "Running",
            "inventory Pod not stable")
    statuses = [s for s in p["status"].get("containerStatuses", []) if s["name"] == "inventory"]
    require(len(statuses) == 1 and statuses[0]["ready"] is True, "inventory Pod not ready")
    return p, statuses[0]


def node_gate():
    nodes = json.loads(run("get", "nodes", "-o", "json"))["items"]
    require(len(nodes) == 1, "expected one node")
    status = nodes[0]["status"]
    conditions = {c["type"]: c["status"] for c in status["conditions"]}
    require(conditions.get("Ready") == "True" and all(conditions.get(t) == "False" for t in
            ("MemoryPressure", "DiskPressure", "PIDPressure")), "node condition gate failed")
    top = run("top", "node", "--no-headers").split()
    require(len(top) == 5 and re.fullmatch(r"\d+m", top[1]) and
            re.fullmatch(r"\d+Mi", top[3]) and re.fullmatch(r"\d+%", top[4]), "node metrics unavailable")
    alloc = status["allocatable"]["memory"]
    require(re.fullmatch(r"\d+Ki", alloc), "allocatable memory unit unknown")
    cpu, memory, capacity = int(top[1][:-1]), int(top[3][:-2]), int(alloc[:-2]) / 1024
    require(cpu < 1400 and memory < capacity * 0.7 and capacity - memory >= 2048,
            "KAN-117 resource stop line breached")
    return {"cpu_m": cpu, "memory_mi": memory, "allocatable_mi": int(capacity)}


def http_code(url, headers=None):
    require(url.startswith("http://127.0.0.1:"), "non-loopback URL prohibited")
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as result:
            result.read(2048)  # discard, never expose response body
            return result.status
    except urllib.error.HTTPError as exc:
        return exc.code


def baseline():
    for name in ("inventory", "order"):
        dep = obj(TARGET, "deployment", name)
        require(deployment_ready(dep), f"{name} Deployment not ready")
        slices = json.loads(run("get", "endpointslices", "-l", f"kubernetes.io/service-name={name}",
                                "-o", "json"))["items"]
        require(any(ep.get("conditions", {}).get("ready") is True for item in slices
                    for ep in item.get("endpoints", [])), f"{name} has no ready endpoint")
    for _ in range(3):
        code = run("exec", "deploy/loadgen", "-c", "loadgen", "--", "curl", "-sS", "-o", "/dev/null",
                   "-w", "%{http_code}", "--max-time", "5",
                   "http://order.liveprobe-target.svc:8080/api/orders/check?item=a", timeout=15)
        require(code.strip() == "200", "order to inventory to DB baseline not 200")


def rendered_candidates():
    # Render locally, never apply the full chaos overlay. YAML contains Secret
    # *references*, not values. Reject non-scoped changes before any handoff.
    proc = subprocess.run(["kubectl", "kustomize", str(ROOT / "deploy/overlays/rancher-desktop-chaos")],
                          capture_output=True, text=True, timeout=45)
    require(proc.returncode == 0, "chaos render unavailable")
    matches = [d for d in yaml.safe_load_all(proc.stdout) if d and d.get("kind") == "Deployment"
               and d.get("metadata", {}).get("name") == "inventory"
               and d.get("metadata", {}).get("namespace") == TARGET]
    require(len(matches) == 1, "rendered inventory count changed")
    local = yaml.safe_load_all((ROOT / "deploy/base/observability/prometheus/prometheus.yaml").read_text())
    config = [d for d in local if d and d.get("kind") == "ConfigMap"
              and d.get("metadata", {}).get("name") == "prometheus-config"]
    require(len(config) == 1, "local prometheus-config count changed")
    return matches[0], config[0]


def scoped_candidates(before, rendered, local_cm):
    dep, cm = before
    candidate = copy.deepcopy(dep)
    template = candidate["spec"]["template"]
    rendered_template = rendered["spec"]["template"]
    require(rendered_template["metadata"].get("labels") == template["metadata"].get("labels"),
            "Pod labels changed")
    old_spec = dep["spec"]["template"]["spec"]
    new_spec = template["spec"]
    rendered_spec = rendered_template["spec"]
    require(not old_spec.get("volumes") and not inventory_container(dep).get("volumeMounts"),
            "unexpected baseline volume/mount")
    new_spec["volumes"] = copy.deepcopy(rendered_spec["volumes"])
    new_container = inventory_container(candidate)
    rendered_container = inventory_container(rendered)
    require(rendered_container["image"] == inventory_container(dep)["image"] and
            rendered_container["resources"] == inventory_container(dep)["resources"],
            "rendered inventory image or limits changed")
    for key in ("env", "volumeMounts", "readinessProbe"):
        new_container[key] = copy.deepcopy(rendered_container[key])
    require(profile(dep) is None and profile(candidate) == "chaos", "profile gate failed")
    old, new = inventory_container(dep), inventory_container(candidate)
    require(new["image"] == old["image"] and new["resources"] == old["resources"],
            "inventory image or limits changed in overlay")
    old_spec = dep["spec"]["template"]["spec"]
    new_spec = candidate["spec"]["template"]["spec"]
    require(set(old_spec) == set(new_spec) - {"volumes"}, "inventory overlay Pod spec key set changed")
    require(all(new_spec[k] == v for k, v in old_spec.items() if k != "containers"),
            "inventory overlay base Pod spec values changed")
    require(new_spec.get("volumes") == [{"name": "chaos-state", "emptyDir": {"sizeLimit": "1Mi"}}],
            "chaos volume changed")
    old_c = {k: v for k, v in old.items() if k not in {"env", "volumeMounts", "readinessProbe"}}
    new_c = {k: v for k, v in new.items() if k not in {"env", "volumeMounts", "readinessProbe"}}
    require(old_c == new_c and len(new.get("env", [])) == len(old.get("env", [])) + 3 and
            new.get("volumeMounts") == [{"name": "chaos-state", "mountPath": "/var/run/liveprobe-chaos"}],
            "inventory overlay container scope changed")
    require({e["name"] for e in new["env"]} - {e["name"] for e in old.get("env", [])} ==
            {"SPRING_PROFILES_ACTIVE", "CHAOS_OPERATOR_TOKEN", "CHAOS_STATE_DIR"} and
            next(e for e in new["env"] if e["name"] == "CHAOS_OPERATOR_TOKEN")["valueFrom"]["secretKeyRef"] ==
            {"name": "inventory-chaos-operator", "key": "token"}, "operator ref mismatch")
    require(set(cm["data"]) == set(local_cm["data"]), "Prometheus config keys differ")
    # Restrict the diff to the sole approved alert duration. This gate also
    # detects concurrent edits and disallows unrelated rule/config changes.
    live_alerts = yaml.safe_load(cm["data"]["alerts.yml"])
    desired_alerts = yaml.safe_load(local_cm["data"]["alerts.yml"])
    live_rule = next((r for g in live_alerts["groups"] for r in g["rules"]
                      if r.get("alert") == "TargetWorkloadOOMKilled"), None)
    desired_rule = next((r for g in desired_alerts["groups"] for r in g["rules"]
                         if r.get("alert") == "TargetWorkloadOOMKilled"), None)
    if live_rule is None or desired_rule is None:
        raise GateError("OOM alert missing")
    require(desired_rule.get("for") == "0s" and 'reason="OOMKilled"' in desired_rule["expr"],
            "OOM alert duration/selector mismatch")
    if live_rule.get("for") == "0s":
        # KAN-134 (b): live already carries the approved 0s rule (KAN-46).
        # Accept only an exact contract: KSM last-terminated-reason OOMKilled
        # selector and byte-identical /data vs the local manifest. The
        # ConfigMap candidate is then the unchanged live object (no-op: no
        # patch and nothing to restore). Anything else stays fail-closed.
        require(KSM_OOM_METRIC in live_rule.get("expr", "") and
                'reason="OOMKilled"' in live_rule.get("expr", "") and
                live_alerts == desired_alerts and cm["data"] == local_cm["data"],
                "OOM alert already 0s but contract mismatch")
        return candidate, copy.deepcopy(cm)
    # KAN-134 (a): 30s baseline -> scoped patch to 0s (original behaviour).
    require(live_rule.get("for") == "30s", "OOM alert duration/selector mismatch")
    live_rule["for"] = "0s"
    require(live_alerts == desired_alerts and all(cm["data"][k] == local_cm["data"][k]
                                                   for k in cm["data"] if k != "alerts.yml"),
            "Prometheus scoped diff changed")
    new_cm = copy.deepcopy(cm)
    new_cm["data"]["alerts.yml"] = local_cm["data"]["alerts.yml"]
    return candidate, new_cm


def oom_rule_for(cm):
    alerts = yaml.safe_load(cm["data"]["alerts.yml"])
    rules = [r for g in alerts["groups"] for r in g["rules"] if r.get("alert") == "TargetWorkloadOOMKilled"]
    require(len(rules) == 1 and rules[0].get("for") in ("0s", "30s"), "OOM rule shape changed")
    return rules[0]["for"]


def oom_rule_mode(before_cm, desired_cm):
    """KAN-134: ('patch', '30s') for 30s->0s, ('noop', '0s') when live is
    already 0s with identical /data. Any other combination is a STOP."""
    live_for, cand_for = oom_rule_for(before_cm), oom_rule_for(desired_cm)
    same = before_cm["data"] == desired_cm["data"]
    if live_for == "30s" and cand_for == "0s" and not same:
        return "patch", live_for
    require(live_for == "0s" and cand_for == "0s" and same, "OOM rule plan ambiguous")
    return "noop", live_for


def snapshot(directory, before, candidates):
    # No Secret objects or values are retrieved. ConfigMap data and Pod env
    # must still be kept private, and must never be pasted into Jira/logs.
    require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode & 0o077 == 0,
            "backup directory must be private 0700")
    path = directory / "snapshot.json"
    require(not path.exists() and not path.is_symlink(), "existing snapshot: manual recovery required")
    for item in before + candidates:
        require(item["kind"] in ("Deployment", "ConfigMap") and
                "Secret" not in json.dumps(item), "unsafe backup object")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"objects": before, "candidates": candidates}, out)
        out.flush()
        os.fsync(out.fileno())
    return path


@contextlib.contextmanager
def local_lease():
    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not LOCK.is_symlink() and LOCK.parent.stat().st_mode & 0o077 == 0,
            "unsafe local lease directory")
    with LOCK.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GateError("another local Family 2 operator owns lease") from exc
        yield


def preflight():
    require(run("config", "current-context", namespace=TARGET).strip() in ("", CONTEXT),
            "current context does not match rancher-desktop")
    metrics = node_gate()
    before = tuple(obj(*key) for key in OBJECTS)
    dep, cm = before
    require(deployment_ready(dep) and profile(dep) is None, "inventory not baseline")
    p, status = pod()
    require(p["metadata"]["uid"] and status["imageID"] and
            status["imageID"].endswith(inventory_container(dep)["image"].split("@")[-1]),
            "inventory Pod UID/image does not match Deployment")
    require(p["metadata"]["ownerReferences"][0]["kind"] == "ReplicaSet", "Pod owner mismatch")
    baseline()
    require(deployment_ready(obj(OBS, "deployment", "prometheus")) and
            deployment_ready(obj(OBS, "deployment", "alertmanager")) and
            deployment_ready(obj("liveprobe-system", "deployment", "liveprobe-api")),
            "signal or receiver deployment unhealthy")
    desired = scoped_candidates(before, *rendered_candidates())
    mode, baseline_for = oom_rule_mode(cm, desired[1])
    return before, desired, {"node": metrics, "pod_uid": p["metadata"]["uid"],
                             "restart_count": status["restartCount"],
                             "oom_rule_mode": mode, "baseline_oom_rule_for": baseline_for,
                             "deployment_uid": dep["metadata"]["uid"], "config_uid": cm["metadata"]["uid"],
                             "sha256": [hashlib.sha256(json.dumps(d["spec"] if d["kind"] == "Deployment"
                                                                  else d["data"], sort_keys=True).encode()).hexdigest()
                                        for d in desired]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, choices=[CONTEXT])
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="reserved: never inject from this host-only card")
    args = parser.parse_args()
    require(not args.execute, "live execution disabled: lease/operator adapters and authenticated observer not wired")
    with local_lease():
        before, desired, report = preflight()
        backup = snapshot(args.backup_dir, before, desired)
        print(json.dumps({"result": "READ_ONLY_PREFLIGHT", "backup": str(backup), **report}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except GateError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(3)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("STOP: " + type(exc).__name__ + " (details suppressed)", file=sys.stderr)
        sys.exit(3)
