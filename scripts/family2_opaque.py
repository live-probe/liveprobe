"""Opaque in-Pod v1 operator boundary; never connected to the live CLI.

The token is expanded only by the container shell and piped to curl's header
stdin. No host Secret, port-forward, token callback, or CLI execution exists.
"""
import json
import re
import subprocess

import family2_gate as gate
import family2_transaction as tx


# Passed as script stdin to `sh -s`, never expanded in kubectl argv. Do not
# enable tracing or log the process output: even an unexpected server response
# must be treated as sensitive until validated.
_GET_SCRIPT = '''set +x
set -eu
[ -n "${CHAOS_OPERATOR_TOKEN:-}" ] || exit 1
printf 'Authorization: Bearer %s\\n' "$CHAOS_OPERATOR_TOKEN" |
  curl -q --silent --noproxy '*' --proto '=http' --max-time 5 --max-filesize 4096 -H @- \\
    -w '\\n%{http_code}' http://127.0.0.1:8080/internal/chaos/v1/faults
'''


class OpaquePodFaultGet:
    """Fail closed unless a chaos-profile Deployment and exact ready Pod match."""

    def __init__(self, deployment_reader, pod_reader, runner=None):
        self.deployment_reader = deployment_reader
        self.pod_reader = pod_reader
        self.runner = runner or subprocess.run

    def _identity(self, pod_name, pod_uid):
        try:
            dep = self.deployment_reader()
            pod = self.pod_reader()
            tx.require(gate.deployment_ready(dep) and gate.profile(dep) == "chaos",
                       "chaos profile not active: STOP")
            meta = pod["metadata"]
            tx.require(meta["namespace"] == tx.TARGET and meta["name"] == pod_name and
                       meta["uid"] == pod_uid and meta.get("deletionTimestamp") is None and
                       pod["status"]["phase"] == "Running" and
                       any(s.get("name") == "inventory" and s.get("ready") is True
                           for s in pod["status"]["containerStatuses"]),
                       "inventory Pod identity or readiness changed")
        except Exception:
            raise tx.UnsafeState("chaos profile/Pod identity unavailable: STOP") from None

    def get_off(self, pod_name, pod_uid):
        tx.require(isinstance(pod_name, str) and
                   re.fullmatch(r"inventory-[a-z0-9-]+", pod_name) is not None and
                   isinstance(pod_uid, str) and bool(pod_uid), "invalid Pod binding")
        self._identity(pod_name, pod_uid)
        args = ["kubectl", "--context", gate.CONTEXT, "-n", tx.TARGET,
                "exec", "-i", pod_name, "-c", "inventory", "--", "sh", "-s"]
        try:
            result = self.runner(args, input=_GET_SCRIPT, text=True,
                                 capture_output=True, timeout=12)
        except Exception:
            raise tx.UnsafeState("opaque GET unavailable (details suppressed)") from None
        finally:
            self._identity(pod_name, pod_uid)
        tx.require(result.returncode == 0 and isinstance(result.stdout, str) and
                   len(result.stdout) <= 4096, "opaque GET failed (output suppressed)")
        try:
            body, code = result.stdout.rsplit("\n", 1)
            payload = json.loads(body)
            tx.require(code == "200" and type(payload) is dict and
                       payload.get("mode") == "off", "opaque GET not off")
        except Exception:
            raise tx.UnsafeState("opaque GET response invalid (output suppressed)") from None
        return True


# Fixed stdin scripts: no token value, user-controlled URL, or command is
# interpolated. curl reads its sole Authorization header from stdin; -q must
# be the first curl option to disable user configuration. No tracing/proxies.
_POST_SCRIPT = '''set +x
set -eu
[ -n "${CHAOS_OPERATOR_TOKEN:-}" ] || exit 1
printf 'Authorization: Bearer %s\n' "$CHAOS_OPERATOR_TOKEN" |
  curl -q --silent --noproxy '*' --proto '=http' --max-time 5 --max-filesize 4096 \\
    -H @- -H 'Content-Type: application/json' -X POST \\
    --data-binary '{"mode":"oom","duration_seconds":6}' \\
    -w '\\n%{http_code}' http://127.0.0.1:8080/internal/chaos/v1/faults
'''
_RESET_SCRIPT = '''set +x
set -eu
[ -n "${CHAOS_OPERATOR_TOKEN:-}" ] || exit 1
printf 'Authorization: Bearer %s\n' "$CHAOS_OPERATOR_TOKEN" |
  curl -q --silent --noproxy '*' --proto '=http' --max-time 5 --max-filesize 4096 \\
    -H @- -H 'Content-Type: application/json' -X POST --data-binary '{}' \\
    -w '\\n%{http_code}' http://127.0.0.1:8080/internal/chaos/v1/reset
'''


class OpaquePodFault(OpaquePodFaultGet):
    """UID-bound, six-second v1 OOM with mandatory reset after any POST attempt.

    Only validated booleans leave this boundary. A lost acknowledgement never
    disowns the fault; the transaction must retain its Lease on uncertainty.
    """
    def __init__(self, deployment_reader, pod_reader, runner=None):
        super().__init__(deployment_reader, pod_reader, runner)
        self.fault_uid = None
        self.fault_name = None
        # KAN-132: inventory container (restartCount, containerID) at POST time.
        self.fault_container = None

    @staticmethod
    def _container(pod):
        statuses = [s for s in pod.get("status", {}).get("containerStatuses", [])
                    if s.get("name") == "inventory"]
        tx.require(len(statuses) == 1, "inventory container status ambiguous")
        return statuses[0]

    @classmethod
    def _container_state(cls, pod):
        """(restartCount, containerID) or None when either is not observable."""
        try:
            status = cls._container(pod)
        except Exception:
            return None
        count, cid = status.get("restartCount"), status.get("containerID")
        if type(count) is not int or count < 0 or not isinstance(cid, str) or not cid:
            return None
        return count, cid

    def restarted_after_oom(self, pod):
        """True only for the SAME owned Pod whose inventory container was
        OOMKilled and restarted after the POST and is running+ready again.

        A ready restarted container means the JVM passed ChaosBootstrapGuard
        (runs before Spring) after the OOM, which deletes an expired intent or
        OOMs again (never ready inside the armed window); the in-JVM v1 state
        died with the killed process. Anything else: False (fail closed).
        """
        base = self.fault_container
        if self.fault_uid is None or base is None:
            return False
        try:
            meta = pod["metadata"]
            status = self._container(pod)
            now = self._container_state(pod)
            terminated = status.get("lastState", {}).get("terminated", {})
            return (meta["uid"] == self.fault_uid and meta["name"] == self.fault_name and
                    meta.get("deletionTimestamp") is None and
                    pod["status"]["phase"] == "Running" and now is not None and
                    now[0] > base[0] and now[1] != base[1] and
                    terminated.get("reason") == "OOMKilled" and
                    terminated.get("exitCode") == 137 and
                    "running" in status.get("state", {}) and status.get("ready") is True)
        except Exception:
            return False

    def confirm_clear_after_oom(self):
        """GET-only reset acknowledgement after a verified OOM restart.

        Requires restarted_after_oom before AND after an opaque GET showing
        mode off on the same Pod UID, with the container identity unchanged
        across the GET (no further restart). No POST is sent. On any doubt the
        fault stays owned (Lease retained by the transaction).
        """
        tx.require(self.fault_uid is not None, "no owned fault to reset")
        pod = self.pod_reader()
        tx.require(self.restarted_after_oom(pod), "no verified OOM restart: reset uncertain")
        seen = self._container_state(pod)
        tx.require(self.get_off(self.fault_name, self.fault_uid) is True, "fault not off after restart")
        after = self.pod_reader()
        tx.require(self.restarted_after_oom(after) and self._container_state(after) == seen,
                   "container changed during post-OOM GET: reset uncertain")
        self.fault_uid, self.fault_name, self.fault_container = None, None, None
        return True

    def _exec(self, name, uid, script, code, fields):
        self._identity(name, uid)
        args = ["kubectl", "--context", gate.CONTEXT, "-n", tx.TARGET,
                "exec", "-i", name, "-c", "inventory", "--", "sh", "-s"]
        try:
            result = self.runner(args, input=script, text=True,
                                 capture_output=True, timeout=12)
        except Exception:
            raise tx.UnsafeState("opaque POST uncertain (details suppressed)") from None
        finally:
            self._identity(name, uid)
        tx.require(result.returncode == 0 and isinstance(result.stdout, str) and
                   len(result.stdout) <= 4096, "opaque POST uncertain (output suppressed)")
        try:
            body, status = result.stdout.rsplit("\n", 1)
            payload = json.loads(body)
            tx.require(status == code and type(payload) is dict and
                       all(type(payload.get(k)) is type(v) and payload[k] == v
                           for k, v in fields.items()), "opaque acknowledgement uncertain")
        except Exception:
            raise tx.UnsafeState("opaque acknowledgement uncertain (output suppressed)") from None
        return True

    def faults_clear(self):
        if self.fault_uid is not None:
            return False
        pod = self.pod_reader()
        return self.get_off(pod["metadata"]["name"], pod["metadata"]["uid"])

    def fault(self, pod_uid, mode="oom", duration_seconds=6):
        tx.require(mode == "oom" and type(duration_seconds) is int and duration_seconds == 6,
                   "only bounded v1 OOM allowed")
        tx.require(self.fault_uid is None and isinstance(pod_uid, str) and bool(pod_uid),
                   "fault already owned or invalid UID")
        pod = self.pod_reader()
        name = pod["metadata"]["name"]
        self._identity(name, pod_uid)
        tx.require(self.get_off(name, pod_uid) is True, "fault not off")
        # Container identity baseline for the post-OOM reset semantics. If it
        # is not observable, only the strict reset acknowledgement can clear.
        state = self._container_state(self.pod_reader())
        # Ownership is recorded before kubectl exec; an OOM may kill the shell
        # after accepting the POST but before its acknowledgement reaches us.
        self.fault_name, self.fault_uid, self.fault_container = name, pod_uid, state
        return self._exec(name, pod_uid, _POST_SCRIPT, "202",
                          {"mode": "oom", "effect": "pending"})

    def clear_fault(self):
        tx.require(self.fault_uid is not None, "no owned fault to reset")
        # Never reset a replacement Pod. Preserve ownership for manual recovery.
        try:
            self._exec(self.fault_name, self.fault_uid, _RESET_SCRIPT, "200",
                       {"mode": "off", "intent_cleared": True})
        except tx.UnsafeState:
            # KAN-132: after an OOMKilled restart the intent is already consumed
            # (ack intent_cleared:false) or the exec hit the restart window.
            # Accept only a verified same-UID OOM restart + GET off; else raise.
            try:
                restarted = self.restarted_after_oom(self.pod_reader())
            except Exception:
                restarted = False
            if not restarted:
                raise
            return self.confirm_clear_after_oom()
        self.fault_uid, self.fault_name, self.fault_container = None, None, None
        return True
