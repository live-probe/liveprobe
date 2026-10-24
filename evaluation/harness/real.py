"""Real (non-fake) implementations: Kubernetes-API fault worker and in-cluster Codex runner.

Fault worker  : wraps the audited KAN-71 operator logic in chaos/run-case.py (snapshot -> inject -> signal -> restore ->
                30 s stability) for the 16 Kubernetes-API-owned cases (bad_image, selector_mismatch). The other 44 cases
                need the target-API chaos overlay (P10-b) and are refused here rather than faked.
Runner        : executes one arm inside the liveprobe-api pod (`python -m app.evalrun run`), i.e. the real engine, DB,
                RAG and subscription Codex CLI. No API key and no Claude is ever used.

Neither class is a fake; `is_fake` is False so the scheduler requires an authorization to use them.
"""
from __future__ import annotations

from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import subprocess
import time

from .runners import FaultWorker, InferenceRunner, RunResult

REPO = Path(__file__).resolve().parents[2]
CONTEXT, SYS_NS = "rancher-desktop", "liveprobe-system"
RESULT_PREFIX = "EVALRUN_RESULT "
K8S_MODES = {"bad_image", "selector_mismatch"}
CHAOS_MODES = {"controlled_crash": "crash", "controlled_oom": "oom", "controlled_latency": "latency",
               "controlled_pool_hold": "pool_exhaust", "controlled_endpoint_off": "endpoint_off"}
SUPPORTED_MODES = K8S_MODES | set(CHAOS_MODES)
MAX_REARM_S = 150          # a fault is held at most this long; the analysis normally finishes well inside it


def _load_chaos():
    spec = importlib.util.spec_from_file_location("chaos_run_case", REPO / "chaos" / "run-case.py")
    mod = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
    import sys
    sys.path.insert(0, str(REPO / "chaos"))              # run-case imports `validate` from its own directory
    spec.loader.exec_module(mod)                         # type: ignore[union-attr]
    return mod


def _kubectl(*args, ns=SYS_NS, timeout=60, stdin=None):
    return subprocess.run(["kubectl", "--context", CONTEXT, "-n", ns, *args], input=stdin, text=True,
                          capture_output=True, timeout=timeout)


class K8sFaultWorker(FaultWorker):
    """Injects bad_image / selector_mismatch with the proven KAN-71 operations; restores and proves the baseline."""
    worker_id = "k8s-fault-worker/1 (chaos/run-case.py KAN-71 logic)"
    is_fake = False

    def __init__(self, cases_dir: Path | None = None, stable_s: int = 30):
        self.c = _load_chaos()
        self._patch_default_routes()
        self.cases_dir = Path(cases_dir) if cases_dir else REPO / "chaos" / "cases"
        self.stable_s = stable_s
        self.before = None
        self.pg_base = None
        self.mode = None
        self.target = None
        self.old_uids = set()
        self.signal_seen = None

    # ---- chaos-API cases (needs deploy/overlays/rancher-desktop-chaos) -------------------------------------------
    def _cx(self, target: str, method: str, path: str, body: dict | None = None, timeout: int = 20):
        """Authenticated call to the target's loopback chaos API. The token is read inside the container
        (CHAOS_OPERATOR_TOKEN) and never leaves it."""
        script = ("printf 'Authorization: Bearer %s' \"$CHAOS_OPERATOR_TOKEN\" | curl -sS -m 10 -o /tmp/.cx_out "
                  "-w '%{http_code}' -H @- -H 'Content-Type: application/json' "
                  + ("--data-binary @/tmp/.cx_in " if body is not None else "")
                  + f"-X {method} http://127.0.0.1:8080{path}; echo; cat /tmp/.cx_out; rm -f /tmp/.cx_out /tmp/.cx_in")
        if body is not None:
            script = "cat > /tmp/.cx_in; " + script
        r = subprocess.run(["kubectl", "--context", CONTEXT, "-n", self.c.NS, "exec", "-i", f"deploy/{target}", "-c", target,
                            "--", "sh", "-c", script], input=json.dumps(body) if body is not None else "", text=True,
                           capture_output=True, timeout=timeout)
        head, _, rest = r.stdout.partition("\n")
        try:
            return int(head.strip()[-3:]), (json.loads(rest) if rest.strip().startswith("{") else {"raw": rest[:200]})
        except ValueError:
            return -1, {"raw": (r.stdout + r.stderr)[:200]}

    def _chaos_body(self, case: dict) -> dict:
        mode, params = case["injection"]["mode"], case["injection"]["parameters"]
        api = CHAOS_MODES[mode]
        if api == "latency":
            return {"mode": "latency", "delay_ms": params["delay_ms"], "duration_seconds": 30}
        if api == "pool_exhaust":
            return {"mode": "pool_exhaust", "duration_seconds": 30}
        return {"mode": api, "duration_seconds": params["duration_seconds"]}

    def _wait(self, fn, timeout_s: int, every: float = 1.0):
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                v = fn()
                if v:
                    return v
            except Exception:  # noqa: BLE001 - transient kubectl/exec failure while the target is faulted
                pass
            time.sleep(every)
        return None

    def _chaos_signal(self, target: str, api: str, restarts0: int, delay_ms: int):
        c = self.c
        if api == "crash":
            def crashed():
                st = c.pod_status(target)
                return any((s.get("state", {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff"
                           or s.get("restartCount", 0) >= restarts0 + 2 for _, _, s in st) and "crashed"
            return self._wait(crashed, 75)
        if api == "oom":
            return self._wait(lambda: any((s.get("lastState", {}).get("terminated") or {}).get("reason") == "OOMKilled"
                                          for _, _, s in c.pod_status(target)) and "oom_killed", 60)
        if api == "latency":
            def slow():
                out = c.kubectl("exec", f"deploy/{target}", "-c", target, "--", "curl", "-s", "-o", "/dev/null", "-m", "8",
                                "-w", "%{time_total}", "http://127.0.0.1:8080" + ("/api/stock/a" if target == "inventory"
                                                                                  else "/api/orders/check?item=a"))
                return float(out.strip()) * 1000 >= delay_ms * 0.8 and "slow"
            return self._wait(slow, 30)
        if api == "pool_exhaust":
            return self._wait(lambda: self._cx(target, "GET", "/internal/chaos/v1/faults")[1].get("held_connections", 0) > 0
                              and "pool_held", 20)
        return self._wait(lambda: c.endpoints(target) == 0 and "no_endpoints", 40)

    def _start_hold(self, target: str, body: dict):
        """Re-arm the fault (same request) whenever it has expired, until restore_baseline() or MAX_REARM_S."""
        import threading
        self._hold_stop = threading.Event()

        fp0 = self._workload_fingerprint(target)

        def loop():
            end = time.monotonic() + MAX_REARM_S
            while not self._hold_stop.wait(2.0) and time.monotonic() < end:
                try:
                    # Policy C: once anyone acts on the workload (new pod / new Deployment generation), stop re-arming,
                    # otherwise the harness would undo the agent's fix and auto-recovery could never be observed.
                    fp = self._workload_fingerprint(target)
                    if fp0 is not None and fp is not None and fp != fp0:
                        self.rearm_stopped_by_change = True
                        return
                    sc, j = self._cx(target, "GET", "/internal/chaos/v1/faults")
                    if sc == 200 and j.get("mode") == "off":
                        self._cx(target, "POST", "/internal/chaos/v1/faults", body)
                        self.rearms += 1
                except Exception:  # noqa: BLE001
                    pass
        self.rearms = 0
        self.rearm_stopped_by_change = False
        self._hold = threading.Thread(target=loop, daemon=True)
        self._hold.start()

    def _workload_fingerprint(self, target: str):
        """Pod UIDs + Deployment generation: changes when a pod is deleted/restarted or the spec is patched.
        Container restarts inside the same pod (the fault itself crash-looping) do NOT change it."""
        try:
            gen = self.c.obj("deployment", target)["metadata"].get("generation")
            uids = sorted({uid for _, uid, _ in self.c.pod_status(target)})
            return (gen, tuple(uids))
        except Exception:  # noqa: BLE001 - unknown state never stops the hold early
            return None

    def _stop_hold(self):
        if getattr(self, "_hold_stop", None) is not None:
            self._hold_stop.set()
            self._hold.join(timeout=15)

    TARGETS = ("inventory", "order")

    def _agent_mutable_spec(self) -> dict:
        """What the agent's typed actions may change: memory limit, replicas, restart annotation. Policy C makes these
        part of the baseline the harness must restore (the agent acts on the real cluster)."""
        out = {}
        for t in self.TARGETS:
            d = self.c.obj("deployment", t)
            c = d["spec"]["template"]["spec"]["containers"][0]
            out[t] = {"memory": ((c.get("resources") or {}).get("limits") or {}).get("memory"),
                      "replicas": d["spec"].get("replicas", 1),
                      "restartedAt": ((d["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get(
                          "kubectl.kubernetes.io/restartedAt")}
        return out

    def _restore_agent_spec(self):
        """Put back any change the agent made to the baseline spec; a no-op when nothing changed."""
        now = self._agent_mutable_spec()
        for t, want in (getattr(self, "spec_before", None) or {}).items():
            have = now[t]
            if have["memory"] != want["memory"] and want["memory"]:
                self.c.kubectl("set", "resources", f"deployment/{t}", "-c", t, f"--limits=memory={want['memory']}")
            if have["replicas"] != want["replicas"]:
                self.c.kubectl("scale", f"deployment/{t}", f"--replicas={want['replicas']}")
            if have["restartedAt"] != want["restartedAt"]:
                if want["restartedAt"] is None:
                    self.c.kubectl("patch", "deployment", t, "--type=json", "-p", json.dumps(
                        [{"op": "remove", "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt"}]))
                else:
                    self.c.kubectl("annotate", f"deployment/{t}", f"kubectl.kubernetes.io/restartedAt={want['restartedAt']}",
                                   "--overwrite")
        for t in self.TARGETS:
            self.c.kubectl("rollout", "status", f"deployment/{t}", "--timeout=150s", timeout=165)

    def _chaos_restore(self, target: str):
        self._stop_hold()
        self._cx(target, "POST", "/internal/chaos/v1/reset", {})
        # a crash/oom-looped pod may sit in kubelet back-off; a fresh pod is the proven reset (KAN-96)
        ok = self._wait(lambda: self.c.kubectl("get", "deploy", target, "-o", "jsonpath={.status.readyReplicas}").strip() == "1"
                        and self.c.endpoints(target) >= 1, 25, every=2)
        if not ok:
            self.c.kubectl("delete", "pod", "-l", f"app={target}", "--wait=true", timeout=180)
            self.c.kubectl("rollout", "status", f"deployment/{target}", "--timeout=150s", timeout=165)

    def _gate(self, wait_s: int = 180) -> dict:
        """KAN-117 node stop gate, unchanged. A transient breach (e.g. right after an image build) is retried for
        up to `wait_s`; a breach that persists still stops the campaign."""
        end, last = time.monotonic() + wait_s, None
        while True:
            try:
                return self.c.node_gate()
            except self.c.Stop as exc:
                last = exc
                if time.monotonic() >= end:
                    raise last
                time.sleep(10)

    def _patch_default_routes(self):
        """Without the chaos overlay the fault route is 404; with it, the route exists but must refuse unauthenticated
        callers (401). Either way an unauthenticated caller must not be able to inject."""
        c = self.c

        def default_routes():
            for target in c.TARGETS:
                code = c.kubectl("exec", f"deploy/{target}", "-c", target, "--", "curl", "-s", "-o", "/dev/null", "-m", "5",
                                 "-w", "%{http_code}", "http://127.0.0.1:8080/internal/chaos/v1/faults").strip()
                if code not in ("404", "401"):
                    raise c.Stop(f"unauthenticated chaos route answers {code}")
        c.default_routes = default_routes

    def acquire(self) -> None:
        if self.cases_dir == REPO / "chaos" / "cases":
            errors, _ = self.c.validate_catalog()
        else:   # P13 held-out catalog: same per-case safety checks, 3 per family
            errors, _ = self.c.validate_catalog(self.cases_dir, per_family=3, total=12)
        if errors:
            raise self.c.Stop("catalog failed validation")
        self._gate()
        self.before = self.c.snapshot()
        self.spec_before = self._agent_mutable_spec()
        self.c.healthy(self.before, stable=0)
        # The inventory pool legitimately holds up to INVENTORY_DB_POOL_MAX (5) sessions after load; a leak would exceed it.
        # (First real run: F4-11 baseline check failed on a transient above-start count while the pool was within bounds.)
        self.pg_base = max(self.c.pg_sessions(), 5)

    def release(self) -> None:
        pass

    def inject(self, public_case: dict) -> dict:
        cid = public_case["id"]
        case = json.loads((self.cases_dir / f"{cid}.yaml").read_text())     # operator side: needs injection params only
        mode = case["injection"]["mode"]
        if mode not in SUPPORTED_MODES:
            raise self.c.Stop(f"{cid}: unknown mode {mode}")
        self.mode, self.target = mode, case["target"]
        gate = self._gate()
        if mode in CHAOS_MODES:
            return self._inject_chaos(case, gate)
        self.c.healthy(self.before, stable=0)
        self.old_uids = {uid for _, uid, _ in self.c.pod_status(self.target)}
        params = case["injection"]["parameters"]
        t0 = time.monotonic()
        if mode == "bad_image":
            self.c.kubectl("set", "image", f"deployment/{self.target}", f"{self.target}={params['tag']}")
        else:
            self.c.kubectl("patch", "service", self.target, "--type=merge", "-p",
                           json.dumps({"spec": {"selector": {"app": params["selector_value"]}}}))
        self.signal_seen = self.c.signal(self.target, mode, self.old_uids)
        return {"mode": mode, "node_gate": gate, "signal": self.signal_seen,
                "signal_after_s": round(time.monotonic() - t0, 1)}

    def _inject_chaos(self, case: dict, gate: dict) -> dict:
        target, mode = self.target, self.mode
        api, body = CHAOS_MODES[mode], self._chaos_body(case)
        self.c.healthy(self.before, stable=0)
        st0 = self.c.pod_status(target)
        restarts0 = max((s.get("restartCount", 0) for _, _, s in st0), default=0)
        t0 = time.monotonic()
        sc, j = self._cx(target, "POST", "/internal/chaos/v1/faults", body)
        if sc != 202:
            raise self.c.Stop(f"chaos POST {api} on {target} returned {sc}")
        self._start_hold(target, body)
        sig = self._chaos_signal(target, api, restarts0, case["injection"]["parameters"].get("delay_ms", 0))
        if not sig:
            self._stop_hold()
            raise self.c.Stop(f"expected {api} symptom on {target} not observed")
        self.signal_seen = sig
        return {"mode": mode, "api": api, "node_gate": gate, "signal": sig, "signal_after_s": round(time.monotonic() - t0, 1)}

    def restore_baseline(self, public_case: dict):
        info = {"case": public_case["id"], "ok": False}
        try:
            if self.mode in CHAOS_MODES:
                self._chaos_restore(self.target)
                self._restore_agent_spec()
            elif self.mode is not None:
                self.c.restore(self.before, self.target, self.mode)
                self._restore_agent_spec()
            end = time.monotonic() + 150
            while True:
                try:
                    self.c.healthy(self.before, stable=self.stable_s, pg_max=self.pg_base)
                    break
                except self.c.Stop:
                    if time.monotonic() >= end:
                        raise
                    time.sleep(3)
            info.update(ok=True, checks=public_case.get("baseline_checks", []), stable_s=self.stable_s,
                        rearms=getattr(self, "rearms", 0),
                        rearm_stopped_by_change=getattr(self, "rearm_stopped_by_change", False))
        except Exception as exc:  # noqa: BLE001 - reported as an unverified baseline; scheduler quarantines
            info.update(ok=False, error_type=type(exc).__name__, error=str(exc)[:200])
        finally:
            self.mode = self.target = None
        return info["ok"], info


class CodexInClusterRunner(InferenceRunner):
    """Runs one (case, arm) through the real engine inside the liveprobe-api pod."""
    runner_id = "codex-in-cluster/1 (liveprobe-api: python -m app.evalrun; subscription Codex CLI only)"
    is_fake = False

    def __init__(self, timeout_s: int = 1500):
        self.timeout_s = timeout_s

    def _exec(self, *argv, timeout=None):
        return subprocess.run(["kubectl", "--context", CONTEXT, "-n", SYS_NS, "exec", "deploy/liveprobe-api", "-c", "api",
                               "--", "python", "-m", "app.evalrun", *argv], text=True, capture_output=True,
                              timeout=timeout or self.timeout_s)

    def lock(self, ttl_s: int = 1800):
        r = self._exec("lock", "--ttl", str(ttl_s), timeout=60)
        if r.returncode:
            raise RuntimeError("could not set evaluation lock: " + r.stderr[-300:])

    def unlock(self):
        self._exec("unlock", timeout=60)

    def run(self, arm, public_case: dict) -> RunResult:
        spec = {"arm_id": arm.arm_id, "strategy": arm.strategy,
                "roles": {k: {"model": v.model, "effort": v.effort} for k, v in arm.roles.items()}}
        if getattr(arm, "rag_kinds", None):
            spec["rag_kinds"] = list(arm.rag_kinds)
        # The campaign (CLI) holds the evaluation lock for its whole duration so the live watcher never opens a
        # second incident from the injected fault between injection and analysis. Each run only refreshes the TTL.
        self.lock(self.timeout_s + 600)
        try:
            r = self._exec("run", "--case", public_case["id"], "--target", public_case["target"],
                           "--signal", public_case["expected_signal"], "--arm", json.dumps(spec))
        except subprocess.TimeoutExpired:
            return RunResult(False, 0.0, {}, f"runner timeout after {self.timeout_s}s")
        line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith(RESULT_PREFIX)), None)
        if line is None:
            return RunResult(False, 0.0, {}, f"no result line (rc={r.returncode}): {r.stderr[-300:]}")
        data = json.loads(line[len(RESULT_PREFIX):])
        if "error" in data:
            return RunResult(False, 0.0, {}, data["error"])
        tel = data.get("telemetry", {})
        # a run whose analysis never produced a real model answer is an infrastructure failure, not a result
        if tel.get("failed_calls") and not data.get("model_calls"):
            return RunResult(False, 0.0, data, "no model call succeeded")
        return RunResult(True, float(data.get("cost_usd") or 0.0), data)
