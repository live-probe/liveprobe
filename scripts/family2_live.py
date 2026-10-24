#!/usr/bin/env python3
"""KAN-131: fail-closed Family 2 live orchestrator (separate from family2_gate).

Default is a read-only dry-run. A live run requires --live, the exact
--confirm-oom-injection token, an --exclusive-card declaration and a NEW private
--backup-dir. `family2_gate.py --execute` stays a hard STOP; this module does not
touch it.

Wiring (Jira KAN-131 decisions):
  exclusive window = orchestrator declaration (only the declared Kanban card is
      running) + no active Job + local flock + create-only UID-owned Lease
      `family2-exclusive-fault` (1800 s) + re-check before start and before POST,
      bounded to a 30 minute window;
  incident observation = KAN-128 read-only DB audit_log observer (fixed SELECT);
  chaos GET/off/POST/reset = KAN-127/129 opaque in-Pod scripts bound to the
      actual chaos-profile Pod UID; default-profile fault-clear is structural.
Writes (live only): JSON Patch CAS of Deployment inventory /spec/template and
ConfigMap prometheus-config /data, plus UID-precondition delete of the single
Prometheus Pod so the subPath-mounted rules reload (Deployment spec untouched).
No Secret read, no token on the host, no response bodies in output.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import family2_adapter as adapter  # noqa: E402
import family2_gate as gate  # noqa: E402
import family2_opaque as opaque  # noqa: E402
import family2_signals as signals  # noqa: E402
import family2_transaction as tx  # noqa: E402
import family2_webhook_audit as audit  # noqa: E402

CONFIRM = "inventory-native-oom-6s-once"
LEASE_SECONDS = 1800
WINDOW_SECONDS = 1800
CARD_RE = re.compile(r"t_[0-9a-f]{8}")
PROM_PATHS = ("/api/v1/rules", "/api/v1/query?")
PROM_PROXY = "/api/v1/namespaces/liveprobe-observability/services/prometheus:9090/proxy"


def kanban_db():
    return Path(os.environ.get("HERMES_KANBAN_DB", Path.home() / ".hermes" / "kanban.db"))


def running_cards(db_path=None):
    """Read-only snapshot of running Kanban cards (URI mode=ro, no writes)."""
    path = Path(db_path or kanban_db())
    tx.require(path.is_file(), "Kanban board unavailable")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        raise tx.UnsafeState("Kanban board read failed (details suppressed)") from None
    return sorted(r[0] for r in rows)


def active_jobs(run=gate.run):
    items = json.loads(run("get", "jobs", "-A", "-o", "json"))["items"]
    return sorted(f"{j['metadata']['namespace']}/{j['metadata']['name']}" for j in items
                  if (j.get("status", {}).get("active") or 0) > 0)


class ExclusiveAuthority:
    """Boolean exclusive-window attestation; re-evaluated at every call.

    True only while: the declared card is the ONLY running Kanban card, no Job
    is active cluster-wide, and the bounded window has not expired. Lease
    ownership and flock are enforced separately by transaction/CLI.
    """
    def __init__(self, card, cards=running_cards, jobs=active_jobs,
                 clock=time.monotonic, window=WINDOW_SECONDS):
        tx.require(isinstance(card, str) and CARD_RE.fullmatch(card) is not None,
                   "exclusive declaration must name a Kanban card id")
        self.card, self.cards, self.jobs, self.clock = card, cards, jobs, clock
        self.deadline = clock() + window
        self.last = {}

    def check(self):
        status = {"declared_card": self.card, "window_left_s": int(self.deadline - self.clock())}
        status["ok"] = False
        try:
            status["running_cards"] = self.cards()
            status["active_jobs"] = self.jobs()
        except Exception:
            status["error"] = "exclusive inputs unavailable"
            self.last = status
            return False
        status["ok"] = (status["running_cards"] == [self.card] and status["active_jobs"] == [] and
                        self.clock() < self.deadline)
        self.last = status
        return status["ok"]

    def __call__(self):
        return self.check() is True


def prometheus_get_json(target, path, runner=subprocess.run):
    """GET-only Prometheus API through the apiserver service proxy."""
    tx.require(target == "prometheus" and any(path.startswith(p) for p in PROM_PATHS),
               "Prometheus path not allowed")
    try:
        proc = runner(["kubectl", "--context", gate.CONTEXT, "get", "--raw", PROM_PROXY + path],
                      text=True, capture_output=True, timeout=15)
    except Exception:
        raise tx.UnsafeState("Prometheus read unavailable (details suppressed)") from None
    tx.require(proc.returncode == 0 and len(proc.stdout) <= 2_000_000,
               "Prometheus read failed (output suppressed)")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise tx.UnsafeState("Prometheus response invalid") from None


class DbIncidents:
    """KAN-128 approved observation path: read-only fixed-projection SELECTs.

    Only integer IDs and booleans are projected; titles/summaries/detail never.
    """
    # Constant SQL only (no interpolation, same rule as KAN-128).
    LIST_SQL = "SELECT id FROM incidents ORDER BY id DESC LIMIT 500"
    GET_SQL = ("SELECT id, (namespace = 'liveprobe-target')::int, (workload = 'inventory')::int,"
               " (COALESCE(signal_reasons, '[]'::jsonb) ? 'TargetWorkloadOOMKilled')::int"
               " FROM incidents ORDER BY id DESC LIMIT 50")

    def __init__(self, observer):
        self.observer = observer

    def list_incidents(self):
        rows = self.observer._query(self.LIST_SQL, 1)
        tx.require(len(rows) <= 500, "incident list truncated")
        return {"items": [{"id": audit._number(r[0])} for r in rows]}

    def get_incident(self, incident_id):
        tx.require(type(incident_id) is int and 0 < incident_id < 2 ** 62, "incident id invalid")
        rows = [r for r in self.observer._query(self.GET_SQL, 4) if audit._number(r[0]) == incident_id]
        tx.require(len(rows) == 1 and all(v in ("0", "1") for v in rows[0][1:]),
                   "incident projection invalid or not in recent window")
        iid = audit._number(rows[0][0])
        ns_ok, wl_ok, reason_ok = (v == "1" for v in rows[0][1:])
        return {"id": iid, "namespace": signals.NAMESPACE if ns_ok else "other",
                "workload": "inventory" if wl_ok else "other",
                "signal_reasons": [signals.ALERT] if reason_ok else []}


def inventory_pod(run=gate.run):
    """Single non-terminating inventory Pod; readiness is NOT required here."""
    pods = json.loads(run("get", "pods", "-l", "app=inventory", "-o", "json"))["items"]
    live = [p for p in pods if p["metadata"].get("deletionTimestamp") is None]
    tx.require(len(pods) == len(live) == 1, "inventory Pod count changed")
    return live[0]


def pod_profile(pod):
    containers = [c for c in pod["spec"]["containers"] if c["name"] == "inventory"]
    tx.require(len(containers) == 1, "inventory Pod container shape changed")
    env = [e for e in containers[0].get("env", []) if e["name"] == "SPRING_PROFILES_ACTIVE"]
    tx.require(len(env) <= 1, "ambiguous Pod profile")
    return env[0].get("value") if env else None


def pod_ready(pod):
    return (pod.get("status", {}).get("phase") == "Running" and
            any(s.get("name") == "inventory" and s.get("ready") is True
                for s in pod.get("status", {}).get("containerStatuses", [])))


class LiveFaultBoundary:
    """Profile-aware fault state: opaque GET in chaos, structural in default.

    In the default profile the chaos controller beans do not exist
    (@Profile("chaos")) and the per-Pod emptyDir state is absent, so a pending
    fault is impossible only if Deployment AND running Pod carry no chaos
    profile/volume and no fault is owned. Anything else is not clear.
    """
    def __init__(self, deployment_reader, pod_reader, opaque_fault, timeout: float = 120, interval: float = 2):
        self.deployment_reader = deployment_reader
        self.pod_reader = pod_reader
        self.opaque = opaque_fault
        self.timeout, self.interval = timeout, interval

    @property
    def fault_uid(self):
        return self.opaque.fault_uid

    def faults_clear(self):
        if self.opaque.fault_uid is not None:
            return False
        dep = self.deployment_reader()
        pod = self.pod_reader()
        profile = gate.profile(dep)
        if profile == "chaos":
            tx.require(pod_profile(pod) == "chaos", "Deployment/Pod profile mismatch")
            return self.opaque.faults_clear() is True
        return (profile is None and pod_profile(pod) is None and
                not dep["spec"]["template"]["spec"].get("volumes") and
                not any(v.get("name") == "chaos-state" for v in pod["spec"].get("volumes", [])) and
                pod_ready(pod))

    def fault(self, pod_uid, mode="oom", duration_seconds=6):
        return self.opaque.fault(pod_uid, mode=mode, duration_seconds=duration_seconds)

    def clear_fault(self):
        # An OOM restarts the container in place; wait (bounded) for the SAME
        # Pod UID to be ready again. Never reset a replacement Pod.
        uid = self.opaque.fault_uid
        tx.require(uid is not None, "no owned fault to reset")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                pod = self.pod_reader()
                tx.require(pod["metadata"]["uid"] == uid, "fault Pod replaced: manual recovery")
                if pod_ready(pod):
                    break
            except tx.UnsafeState:
                raise
            except Exception:
                pass
            tx.require(time.monotonic() < deadline, "fault Pod not ready for reset")
            time.sleep(self.interval)
        return self.opaque.clear_fault()


def oom_rule_for(cm):
    alerts = yaml.safe_load(cm["data"]["alerts.yml"])
    rules = [r for g in alerts["groups"] for r in g["rules"] if r.get("alert") == signals.ALERT]
    tx.require(len(rules) == 1 and rules[0].get("for") in ("0s", "30s"), "OOM rule shape changed")
    return rules[0]["for"]


def spec_hash(item):
    body = item["spec"] if item["kind"] == "Deployment" else item.get("data")
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class PrometheusRestarter:
    """UID-precondition delete of the single Prometheus Pod (Recreate strategy)."""
    def __init__(self, transport, run=gate.run):
        self.transport = transport
        self.run = run

    def pods(self):
        return json.loads(self.run("get", "pods", "-l", "app=prometheus", "-o", "json",
                                   namespace=tx.OBS))["items"]

    def current(self):
        pods = [p for p in self.pods() if p["metadata"].get("deletionTimestamp") is None]
        tx.require(len(pods) == 1, "Prometheus Pod count changed")
        return pods[0]

    def restart(self, pod):
        meta = pod["metadata"]
        tx.require(meta["namespace"] == tx.OBS and re.fullmatch(r"prometheus-[a-z0-9-]+", meta["name"])
                   and meta.get("uid") and meta["ownerReferences"][0]["kind"] == "ReplicaSet",
                   "Prometheus Pod scope invalid")
        self.transport._call(self.transport.core.delete_namespaced_pod, meta["name"], tx.OBS,
                             body={"apiVersion": "v1", "kind": "DeleteOptions",
                                   "preconditions": {"uid": meta["uid"]}})


class LiveHealth(adapter.ReadOnlyHealth):
    def rollout(self, namespace, name):
        try:
            dep = gate.obj(namespace, "deployment", name)
            if not gate.deployment_ready(dep):
                return False
            if name == "inventory":
                pod = inventory_pod()
                return pod_ready(pod) and pod_profile(pod) == gate.profile(dep)
            return True
        except Exception:
            return False


class LiveOperator(adapter.PreparedOperator):
    def __init__(self, transport, restarter, signals_reader, run=gate.run, **kwargs):
        super().__init__(transport, **kwargs)
        tx.require(self.health is not None, "health reader required")
        self.restarter = restarter
        self.signals = signals_reader
        self.run = run
        self.dry_run_verified = False

    def get(self, namespace, kind, name):
        # Reads use kubectl JSON (server representation) so CAS test values
        # match exactly; the client transport is used only for writes/Lease.
        tx.require((namespace, kind, name) in tx.OBJECTS, "object read scope invalid")
        return gate.obj(namespace, kind, name)

    def patch(self, namespace, kind, name, operations):
        """CAS JSON Patch via kubectl (Go JSON encoder), not the Python client.

        KAN-130 live finding: the apiserver JSON-Patch `test` op compares string
        values against Go-marshaled bytes, where '<', '>' and '&' are escaped
        (\\u003e). The Python client sends them raw, so `test /data` on
        prometheus-config (alerts/recording rules contain '>') was rejected 422
        although identical kubectl server dry-run passed. Same scope/shape
        checks as KubernetesTransport.patch; output suppressed.
        """
        tx.require((namespace, kind, name) in tx.OBJECTS, "object patch scope invalid")
        field = "/spec/template" if kind == "deployment" else "/data"
        tx.require(len(operations) == 4 and
                   [(op.get("op"), op.get("path")) for op in operations] ==
                   [("test", "/metadata/uid"), ("test", "/metadata/resourceVersion"),
                    ("test", field), ("replace", field)] and
                   all(op.get("value") is not None for op in operations), "non-CAS patch rejected")
        out = json.loads(self.run("patch", kind, name, "--type=json", "-p", json.dumps(operations),
                                  "-o", "json", namespace=namespace))
        tx.require(out["metadata"]["uid"] == operations[0]["value"], "patched object identity mismatch")
        return out

    def preflight(self):
        before, desired, report = gate.preflight()
        desired = self.server_normalize(before, desired)
        pod = inventory_pod()
        prom = gate.obj(tx.OBS, "deployment", "prometheus")
        report = dict(report, pod_template_hash=pod["metadata"]["labels"].get("pod-template-hash"),
                      prometheus_uid=prom["metadata"]["uid"], prometheus_spec=spec_hash(prom),
                      prometheus_pod_uid=self.restarter.current()["metadata"]["uid"])
        tx.require(report["pod_template_hash"], "inventory pod-template-hash missing")
        return before, desired, report

    def server_normalize(self, before, desired):
        """Server-side dry-run (dryRun=All, never persisted) of each CAS patch.

        The API server adds defaults (e.g. probe scheme) to the candidate; the
        stored result must be the exact `desired` or later verify/restore
        comparisons would misread defaulting as a foreign edit and strand the
        Lease. Only additive defaulting is accepted.
        """
        normalized = []
        for index, (namespace, kind, name) in enumerate(tx.OBJECTS):
            if tx.field_value(desired[index], index) == tx.field_value(before[index], index):
                # KAN-134 no-op object: no patch (not even a server dry-run).
                normalized.append(json.loads(json.dumps(desired[index])))
                continue
            field = "/spec/template" if index == 0 else "/data"
            ops = tx.patch_body(before[index], desired[index], field)
            out = json.loads(self.run("patch", kind, name, "--type=json", "--dry-run=server",
                                      "-p", json.dumps(ops), "-o", "json", namespace=namespace))
            tx.require(out["metadata"]["uid"] == before[index]["metadata"]["uid"] and
                       out["metadata"]["resourceVersion"] == before[index]["metadata"]["resourceVersion"],
                       "server dry-run changed identity/resourceVersion")
            tx.require(superset(tx.field_value(out, index), tx.field_value(desired[index], index)),
                       "server dry-run changed candidate beyond defaulting")
            item = json.loads(json.dumps(desired[index]))
            if index == 0:
                item["spec"]["template"] = out["spec"]["template"]
            else:
                item["data"] = out["data"]
            normalized.append(item)
        for index, key in enumerate(tx.OBJECTS):  # prove nothing was persisted
            now = gate.obj(*key)
            tx.require(now["metadata"]["resourceVersion"] == before[index]["metadata"]["resourceVersion"] and
                       tx.field_value(now, index) == tx.field_value(before[index], index),
                       "object changed during server dry-run")
        self.dry_run_verified = True
        return tuple(normalized)

    def current_pod_uid(self):
        pod = inventory_pod()
        tx.require(pod_ready(pod) and pod_profile(pod) == "chaos", "fault Pod not chaos/ready")
        return pod["metadata"]["uid"]

    def rollout(self, name):
        tx.require(name in ("inventory", "prometheus"), "rollout scope invalid")
        if name == "inventory":
            return super().rollout(name)
        expected = oom_rule_for(gate.obj(tx.OBS, "configmap", "prometheus-config"))
        health = self.health
        if health is None:
            raise tx.UnsafeState("health reader required")
        if health.rollout(tx.OBS, "prometheus") and self.signals.rule_loaded(expected) is True:
            return None  # loaded rule already matches the ConfigMap: no restart
        old = self.restarter.current()
        self.restarter.restart(old)

        def reloaded():
            try:
                pod = self.restarter.current()
                return (pod["metadata"]["uid"] != old["metadata"]["uid"] and pod_ready_generic(pod) and
                        health.rollout(tx.OBS, "prometheus") and
                        self.signals.rule_loaded(expected) is True)
            except Exception:
                return False
        self.wait_until(reloaded, "prometheus reload")

    def verify_restored(self, before, report):
        super().verify_restored(before, report)
        # Independent kubectl re-read: identity + full spec/data equality.
        for index, key in enumerate(tx.OBJECTS):
            now = gate.obj(*key)
            old = before[index]
            tx.require(now["metadata"]["uid"] == old["metadata"]["uid"] and
                       spec_hash(now) == spec_hash(old), "independent baseline diff failed")
        prom = gate.obj(tx.OBS, "deployment", "prometheus")
        tx.require(prom["metadata"]["uid"] == report["prometheus_uid"] and
                   spec_hash(prom) == report["prometheus_spec"], "Prometheus Deployment drifted")
        pod = inventory_pod()
        tx.require(pod_ready(pod) and pod_profile(pod) is None and
                   pod["metadata"]["labels"].get("pod-template-hash") == report["pod_template_hash"],
                   "inventory did not return to baseline ReplicaSet")
        baseline_for = report.get("baseline_oom_rule_for", "30s")
        tx.require(oom_rule_for(gate.obj(tx.OBS, "configmap", "prometheus-config")) == baseline_for,
                   "baseline OOM rule not restored")


def superset(server, local):
    """True if `server` equals `local` plus only added dict keys (defaults)."""
    if isinstance(local, dict):
        return isinstance(server, dict) and all(k in server and superset(server[k], v)
                                                for k, v in local.items())
    if isinstance(local, list):
        return (isinstance(server, list) and len(server) == len(local) and
                all(superset(s, v) for s, v in zip(server, local)))
    return server == local


def pod_ready_generic(pod):
    return (pod.get("status", {}).get("phase") == "Running" and
            all(s.get("ready") is True for s in pod.get("status", {}).get("containerStatuses", [{}])))


def cas_shape(before, desired):
    """Build (never send) both CAS bodies and validate them like the transport."""
    shapes = []
    for index, (namespace, kind, name) in enumerate(tx.OBJECTS):
        field = "/spec/template" if index == 0 else "/data"
        if tx.field_value(desired[index], index) == tx.field_value(before[index], index):
            digest = hashlib.sha256(json.dumps(tx.field_value(before[index], index),
                                               sort_keys=True).encode()).hexdigest()
            shapes.append({"object": f"{namespace}/{kind}/{name}", "ops": [], "noop": True,
                           "uid": before[index]["metadata"]["uid"],
                           "resourceVersion": before[index]["metadata"]["resourceVersion"],
                           "old_sha256": digest, "new_sha256": digest})
            continue
        ops = tx.patch_body(before[index], desired[index], field)
        tx.require([(o["op"], o["path"]) for o in ops] ==
                   [("test", "/metadata/uid"), ("test", "/metadata/resourceVersion"),
                    ("test", field), ("replace", field)] and
                   all(o.get("value") is not None for o in ops), "non-CAS patch rejected")
        shapes.append({"object": f"{namespace}/{kind}/{name}", "ops": [o["op"] + " " + o["path"] for o in ops],
                       "uid": before[index]["metadata"]["uid"],
                       "resourceVersion": before[index]["metadata"]["resourceVersion"],
                       "old_sha256": hashlib.sha256(json.dumps(tx.field_value(before[index], index),
                                                               sort_keys=True).encode()).hexdigest(),
                       "new_sha256": hashlib.sha256(json.dumps(tx.field_value(desired[index], index),
                                                               sort_keys=True).encode()).hexdigest()})
    return shapes


def build(card, transport=None, runner=subprocess.run):
    """Assemble live components. Constructors perform no cluster calls."""
    transport = transport or adapter.KubernetesTransport(gate.CONTEXT)
    authority = ExclusiveAuthority(card)
    dep_reader = lambda: gate.obj(tx.TARGET, "deployment", "inventory")  # noqa: E731
    opaque_fault = opaque.OpaquePodFault(dep_reader, inventory_pod, runner)
    boundary = LiveFaultBoundary(dep_reader, inventory_pod, opaque_fault)
    reader = signals.SignalReader(lambda target, path: prometheus_get_json(target, path, runner))
    health = LiveHealth(rule_loaded=reader.rule_loaded, faults_clear=boundary.faults_clear)
    operator = LiveOperator(transport, PrometheusRestarter(transport), reader,
                            fault_boundary=boundary, exclusive_authority=authority,
                            health=health, timeout=300, interval=3)
    webhook = audit.WebhookAuditObserver(runner)
    incidents = DbIncidents(webhook)
    observer = signals.ChainObserver(reader, inventory_pod, incidents.list_incidents,
                                     incidents.get_incident, timeout=180, interval=3, audit=webhook)
    lease = tx.KubernetesLease(transport, duration_seconds=LEASE_SECONDS)
    return {"transport": transport, "authority": authority, "boundary": boundary,
            "signals": reader, "operator": operator, "observer": observer, "audit": webhook,
            "incidents": incidents, "lease": lease}


def dry_run(parts):
    """Read-only: every gate evaluated, CAS bodies built, nothing written."""
    report = {"mode": "DRY_RUN", "writes": 0, "lease_created": False, "fault_post": False}
    authority = parts["authority"]
    ok = authority()
    report["exclusive_window"] = dict(authority.last, ok=ok)
    report["lease_absent"] = parts["transport"].get(tx.LEASE) is None
    before, desired, pre = parts["operator"].preflight()
    report["preflight"] = pre
    report["server_dry_run_cas"] = parts["operator"].dry_run_verified is True
    report["cas"] = cas_shape(before, desired)
    report["live_oom_rule_for"] = oom_rule_for(before[1])
    report["candidate_oom_rule_for"] = oom_rule_for(desired[1])
    # KAN-134: 'patch' = 30s baseline -> 0s CAS; 'noop' = live already 0s with
    # identical /data (no ConfigMap write, restore also no-op). Else STOP.
    report["oom_rule_mode"], baseline_for = gate.oom_rule_mode(before[1], desired[1])
    tx.require(pre.get("oom_rule_mode", report["oom_rule_mode"]) == report["oom_rule_mode"],
               "OOM rule plan mismatch between gate and orchestrator")
    report["baseline_oom_rule_for"] = baseline_for
    report["prometheus_rule_loaded_baseline"] = parts["signals"].rule_loaded(baseline_for) is True
    report["profile"] = gate.profile(before[0])
    report["faults_clear_default_profile"] = parts["boundary"].faults_clear() is True
    report["audit_observer_ready"] = parts["audit"].ready() is True
    report["incident_list_ok"] = isinstance(parts["incidents"].list_incidents().get("items"), list)
    report["chaos_opaque_get"] = "deferred: requires chaos-profile Pod (live step 6)"
    report["plan"] = [
        "1 flock + exclusive re-check + create-only Lease family2-exclusive-fault (1800s)",
        "2 preflight + private 0600 snapshot (fsync) + snapshot drift re-read",
        "3 CAS JSON Patch inventory /spec/template; prometheus-config /data only if oom_rule_mode=patch",
        "4 inventory rollout (chaos Pod ready) + rule for=0s loaded (Prometheus Pod UID-delete only if needed)",
        "5 chaos opaque GET mode=off on actual chaos Pod UID; audit/incident baseline bind",
        "6 exclusive re-check + Lease assert, single POST oom 6s (NOT executed in KAN-131)",
        "7 observe OOMKilled/137 + KSM + firing + new DB audit incident",
        "8 finally: reset same UID, reverse CAS restore of changed objects, rollouts, rule = baseline "
        "(30s, or 0s unchanged in noop mode), 3x200, UID/spec diff",
        "9 release Lease only if restored and no uncertainty; else retain + ESCALATE"]
    gates = ["lease_absent", "server_dry_run_cas", "prometheus_rule_loaded_baseline",
             "faults_clear_default_profile", "audit_observer_ready", "incident_list_ok"]
    report["ready_for_live"] = (report["exclusive_window"]["ok"] and report["profile"] is None and
                                report["candidate_oom_rule_for"] == "0s" and
                                ((report["oom_rule_mode"] == "patch" and report["live_oom_rule_for"] == "30s") or
                                 (report["oom_rule_mode"] == "noop" and report["live_oom_rule_for"] == "0s")) and
                                all(report[g] for g in gates))
    return report


def live(parts, backup_dir):
    tx.require(parts["authority"](), "exclusive window not established: STOP")
    tx.require(not backup_dir.exists(), "backup dir must be new")
    backup_dir.mkdir(mode=0o700, parents=False)
    os.chmod(backup_dir, 0o700)
    return tx.transaction(parts["operator"], parts["observer"], parts["lease"],
                          lambda before, desired: gate.snapshot(backup_dir, before, desired))


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", required=True, choices=[gate.CONTEXT])
    p.add_argument("--exclusive-card", help="Kanban card id declared as the sole fault runner")
    p.add_argument("--live", action="store_true", help="perform the single live F2 run")
    p.add_argument("--confirm-oom-injection", default="")
    p.add_argument("--backup-dir", type=Path)
    return p.parse_args(argv)


def main(argv=None, build_parts=build, lock=gate.local_lease):
    args = parse(argv)
    if args.live:
        tx.require(args.confirm_oom_injection == CONFIRM, "live requires exact confirmation token")
        tx.require(args.exclusive_card is not None and args.backup_dir is not None,
                   "live requires --exclusive-card and a new --backup-dir")
    card = args.exclusive_card or os.environ.get("HERMES_KANBAN_TASK", "")
    with lock():
        parts = build_parts(card)
        if not args.live:
            print(json.dumps(dry_run(parts), sort_keys=True, indent=1))
            return 0
        try:
            result = live(parts, args.backup_dir)
        except Exception as exc:
            retained = parts["lease"].uid is not None
            owned = getattr(parts["boundary"], "fault_uid", None) is not None
            print(json.dumps({"result": "ESCALATE" if (retained or owned) else "STOPPED",
                              "lease_retained": retained, "fault_owned": owned,
                              "reason": (str(exc) if isinstance(exc, (tx.UnsafeState, gate.GateError))
                                         else type(exc).__name__),
                              "backup_dir": str(args.backup_dir)}, sort_keys=True))
            return 4 if (retained or owned) else 3
        print(json.dumps({"result": "LIVE_F2_VERIFIED", **result,
                          "backup_dir": str(args.backup_dir)}, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (gate.GateError, tx.UnsafeState) as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(3)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("STOP: " + type(exc).__name__ + " (details suppressed)", file=sys.stderr)
        sys.exit(3)
