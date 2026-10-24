"""KAN-123 no-fault Kubernetes adapter. Never wired to --execute.

The optional kubernetes client performs no calls at import/construction time. All
write methods require an explicit caller; no fault HTTP client is implemented.
An exclusive-window authority and authenticated incident observer are injected,
not inferred from a per-directory lock or a token read from a Secret.
"""
import contextlib
import re
import socket
import subprocess
import time

import family2_gate as gate
import family2_transaction as tx


class TransportError(tx.UnsafeState):
    pass


class KubernetesTransport:
    """Explicit context and resource allowlist; suppress API exception contents."""
    def __init__(self, context, client_factory=None):
        tx.require(context == gate.CONTEXT, "wrong Kubernetes context")
        if client_factory is None:
            try:
                from kubernetes import client, config
                api_client = config.new_client_from_config(context=context)
            except Exception as exc:
                raise TransportError("Kubernetes client/context unavailable (details suppressed)") from None
            client_factory = lambda kind: (client.CoordinationV1Api(api_client) if kind == "lease" else
                                           client.AppsV1Api(api_client) if kind == "deployment" else
                                           client.CoreV1Api(api_client))
            self._serialize = api_client.sanitize_for_serialization
        else:
            self._serialize = lambda value: value
        self.lease_api = client_factory("lease")
        self.apps = client_factory("deployment")
        self.core = client_factory("configmap")

    def _call(self, method, *args, **kwargs):
        try:
            return self._serialize(method(*args, **kwargs))
        except Exception as exc:
            raise TransportError("Kubernetes request failed (details suppressed)") from None

    def get(self, name):
        tx.require(name == tx.LEASE, "Lease name not allowed")
        try:
            result = self.lease_api.read_namespaced_lease(name, tx.TARGET)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise TransportError("Lease read failed (details suppressed)") from None
        return self._serialize(result)

    def create(self, body):
        tx.require(body.get("metadata", {}).get("name") == tx.LEASE and
                   body["metadata"].get("namespace") == tx.TARGET and
                   body.get("kind") == "Lease", "Lease create scope invalid")
        return self._call(self.lease_api.create_namespaced_lease, tx.TARGET, body)

    def delete(self, name, uid):
        tx.require(name == tx.LEASE and bool(uid), "Lease delete scope invalid")
        # The server, not the client read, enforces UID identity atomically.
        self._call(self.lease_api.delete_namespaced_lease, name, tx.TARGET,
                   body={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})

    def object(self, namespace, kind, name):
        tx.require((namespace, kind, name) in tx.OBJECTS, "object read scope invalid")
        method = (self.apps.read_namespaced_deployment if kind == "deployment" else
                  self.core.read_namespaced_config_map)
        return self._call(method, name, namespace)

    def patch(self, namespace, kind, name, operations):
        tx.require((namespace, kind, name) in tx.OBJECTS, "object patch scope invalid")
        field = "/spec/template" if kind == "deployment" else "/data"
        tx.require(len(operations) == 4 and
                   [(op.get("op"), op.get("path")) for op in operations] ==
                   [("test", "/metadata/uid"), ("test", "/metadata/resourceVersion"),
                    ("test", field), ("replace", field)] and
                   all(op.get("value") is not None for op in operations), "non-CAS patch rejected")
        method = (self.apps.patch_namespaced_deployment if kind == "deployment" else
                  self.core.patch_namespaced_config_map)
        return self._call(method, name, namespace, operations,
                          _content_type="application/json-patch+json")


@contextlib.contextmanager
def exclusive_local_lock():
    """Hold the shared host flock across the entire caller operation."""
    with gate.local_lease():
        yield


class IncidentObserver:
    """Callbacks must return booleans only; never expose credentials or bodies.

    `ready_check` authenticates via a separately approved, loopback-only path;
    `proof_check` verifies correlation and alert/incident identity. No callback
    is provided here, so this boundary cannot itself authorize a live fault.
    """
    def __init__(self, ready_check=None, proof_check=None):
        self.ready_check = ready_check
        self.proof_check = proof_check

    def ready(self):
        return bool(self.ready_check and self.ready_check() is True)

    def observe(self, report):
        return bool(self.proof_check and self.proof_check(report) is True)


class PodLoopbackForward:
    """Pod-name/UID-bound port-forward; authentication lives in an approved callback.

    Not a CLI entrypoint. The callback performs HTTP itself and returns decoded
    JSON; this class neither accepts nor retrieves a token, Secret or cookie.
    """
    def __init__(self, pod_reader, pod_name, pod_uid, local_port,
                 authenticated_request=None, launcher=None, probe=None, timeout: float=10):
        tx.require(re.fullmatch(r"inventory-[a-z0-9-]+", pod_name) is not None and
                   isinstance(pod_uid, str) and bool(pod_uid) and
                   type(local_port) is int and 1024 <= local_port <= 65535 and
                   0 < timeout <= 30, "invalid Pod loopback binding")
        self.pod_reader = pod_reader
        self.pod_name = pod_name
        self.pod_uid = pod_uid
        self.local_port = local_port
        self.authenticated_request = authenticated_request
        self.launcher = launcher or self._launch
        self.probe = probe or self._probe
        self.timeout = timeout
        self.process = None

    @staticmethod
    def _launch(args):
        return subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _probe(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.3):
                return True
        except OSError:
            return False

    def _identity(self):
        try:
            pod = self.pod_reader()
            tx.require(pod["metadata"]["namespace"] == tx.TARGET and
                       pod["metadata"]["name"] == self.pod_name and
                       pod["metadata"]["uid"] == self.pod_uid and
                       pod["metadata"].get("deletionTimestamp") is None,
                       "forward Pod identity changed")
        except Exception:
            raise tx.UnsafeState("forward Pod identity unavailable or changed") from None

    def __enter__(self):
        tx.require(self.process is None and callable(self.authenticated_request),
                   "approved authenticated callback unavailable")
        self._identity()
        # Do not mistake a pre-existing listener for our own forward.
        tx.require(not self.probe(self.local_port), "local port already occupied")
        args = ["kubectl", "--context", gate.CONTEXT, "-n", tx.TARGET,
                "port-forward", "--address", "127.0.0.1",
                "pod/" + self.pod_name, f"{self.local_port}:8080"]
        try:
            self.process = self.launcher(args)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                tx.require(self.process.poll() is None, "port-forward exited")
                if self.probe(self.local_port):
                    self._identity()
                    return self
                time.sleep(.05)
            raise tx.UnsafeState("port-forward readiness timed out")
        except Exception:
            self.__exit__(None, None, None)
            raise tx.UnsafeState("Pod port-forward unavailable (details suppressed)") from None

    def request(self, method, path, body=None):
        tx.require(self.process is not None and self.process.poll() is None and
                   method in ("GET", "POST") and
                   path in ("/internal/chaos/v1/faults", "/internal/chaos/v1/reset"),
                   "forward request not allowed")
        self._identity()
        callback = self.authenticated_request
        if not callable(callback):
            raise tx.UnsafeState("approved authenticated callback unavailable")
        try:
            result = callback(f"http://127.0.0.1:{self.local_port}" + path, method, body)
        except Exception:
            raise tx.UnsafeState("authenticated loopback request unavailable (details suppressed)") from None
        finally:
            self._identity()
        tx.require(isinstance(result, tuple) and len(result) == 2 and
                   type(result[0]) is int and isinstance(result[1], dict),
                   "authenticated loopback response invalid")
        return result

    def __exit__(self, exc_type, exc, tb):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                process.kill()
                process.wait(timeout=3)


class AuthenticatedIncidents:
    """Fail-closed reader facade; approved callback owns browser/session auth.

    The callback is intentionally absent by default and must never return
    credentials to this adapter. No browser vault handle is convertible here.
    """
    def __init__(self, authenticated_get=None):
        self.authenticated_get = authenticated_get

    def _get(self, path):
        callback = self.authenticated_get
        if not callable(callback):
            raise tx.UnsafeState("approved incident callback unavailable")
        try:
            result = callback(path)
        except Exception:
            raise tx.UnsafeState("authenticated incident read unavailable (details suppressed)") from None
        tx.require(isinstance(result, tuple) and len(result) == 2,
                   "authenticated incident read invalid")
        status, payload = result
        tx.require(status == 200 and isinstance(payload, dict),
                   "authenticated incident read denied or invalid")
        return payload

    def ready(self):
        payload = self.list_incidents()
        return isinstance(payload.get("items"), list)

    def list_incidents(self):
        payload = self._get("/api/incidents?limit=500&offset=0")
        items, total = payload.get("items"), payload.get("total")
        tx.require(isinstance(items, list) and type(total) is int and
                   0 <= total == len(items) <= 500 and
                   all(type(item.get("id")) is int for item in items if isinstance(item, dict)) and
                   all(isinstance(item, dict) for item in items),
                   "authenticated incident baseline incomplete")
        return payload

    def get_incident(self, incident_id):
        tx.require(type(incident_id) in (int, str) and
                   re.fullmatch(r"[0-9]+", str(incident_id)) is not None,
                   "incident id invalid")
        return self._get("/api/incidents/" + str(incident_id))


class NoFaultOperator:
    """Scoped CAS and bounded no-fault verification; injection deliberately absent."""
    def __init__(self, transport, exclusive_authority=None, health=None, timeout=180, interval=2):
        tx.require(0 < timeout <= 600 and 0 < interval <= 10, "unbounded verifier")
        self.transport = transport
        self.exclusive_authority = exclusive_authority
        self.health = health
        self.timeout = timeout
        self.interval = interval

    def exclusive_window(self):
        # A named Lease only excludes participants that honor it. An independent
        # coordinator must attest that other workers have stopped.
        return bool(self.exclusive_authority and self.exclusive_authority() is True)

    def get(self, namespace, kind, name):
        return self.transport.object(namespace, kind, name)

    def patch(self, namespace, kind, name, operations):
        return self.transport.patch(namespace, kind, name, operations)

    def wait_until(self, predicate, label):
        deadline = time.monotonic() + self.timeout
        while True:
            if predicate() is True:
                return
            if time.monotonic() >= deadline:
                raise tx.UnsafeState(label + " timed out")
            time.sleep(min(self.interval, max(0, deadline - time.monotonic())))

    def rollout(self, name):
        tx.require(name in ("inventory", "prometheus"), "rollout scope invalid")
        namespace = tx.TARGET if name == "inventory" else tx.OBS
        # Health callbacks must observe a fresh Deployment and ready Pod, not
        # merely old ReplicaSet availability. This operator only reads here.
        tx.require(self.health is not None, "rollout health reader unavailable")
        self.wait_until(lambda: self.health.rollout(namespace, name), name + " rollout")

    def verify_fields(self, expected):
        tx.require(len(expected) == 2, "expected two objects")
        for index, key in enumerate(tx.OBJECTS):
            current = self.get(*key)
            tx.require(current["metadata"]["uid"] == expected[index]["metadata"]["uid"] and
                       tx.field_value(current, index) == tx.field_value(expected[index], index),
                       "exact scoped state differs")

    def verify_candidate(self, desired, report):
        self.verify_fields(desired)
        tx.require(self.health is not None and self.health.candidate(report) is True,
                   "candidate health/rule verification unavailable")

    def verify_restored(self, before, report):
        self.verify_fields(before)
        tx.require(self.health is not None and self.health.restored(report) is True,
                   "baseline health/3x200 verification unavailable")

    def fault(self, *, mode, duration_seconds, pod_uid) -> None:
        raise tx.UnsafeState("fault transport not authorized in no-fault adapter")

    def clear_fault(self) -> None:
        raise tx.UnsafeState("fault reset not authorized in no-fault adapter")


class PreparedOperator(NoFaultOperator):
    """Injectable transaction adapter; CLI deliberately does not instantiate it.

    Caller must supply independent exclusivity, a credential-safe loopback
    boundary and concrete rule/health readers. Constructor has no side effects.
    """
    def __init__(self, transport, fault_boundary=None, **kwargs):
        super().__init__(transport, **kwargs)
        self.fault_boundary = fault_boundary

    def preflight(self):
        return gate.preflight()

    def current_pod_uid(self):
        return gate.pod()[0]["metadata"]["uid"]

    def faults_clear(self):
        return bool(self.fault_boundary and self.fault_boundary.faults_clear() is True)

    def fault(self, *, mode, duration_seconds, pod_uid) -> None:
        if self.fault_boundary is None:
            raise tx.UnsafeState("fault boundary unavailable")
        self.fault_boundary.fault(pod_uid=pod_uid, mode=mode,
                                  duration_seconds=duration_seconds)

    def clear_fault(self) -> None:
        if self.fault_boundary is None:
            raise tx.UnsafeState("fault boundary unavailable")
        self.fault_boundary.clear_fault()


class ReadOnlyHealth:
    """Fresh bounded rollout/restore checks, with fail-closed external signals."""
    def __init__(self, rule_loaded=None, faults_clear=None):
        self.rule_loaded = rule_loaded
        self.faults_clear = faults_clear

    def rollout(self, namespace, name):
        dep = gate.obj(namespace, "deployment", name)
        return gate.deployment_ready(dep)

    def candidate(self, report):
        dep = gate.obj(tx.TARGET, "deployment", "inventory")
        pod, status = gate.pod()
        c = gate.inventory_container(dep)
        return (gate.deployment_ready(dep) and gate.profile(dep) == "chaos" and
                pod["metadata"].get("uid") != report.get("pod_uid") and
                status.get("imageID", "").endswith(c["image"].split("@")[-1]) and
                self.rule_loaded is not None and self.rule_loaded("0s") is True and
                self.faults_clear is not None and self.faults_clear() is True and
                gate.deployment_ready(gate.obj(tx.OBS, "deployment", "prometheus")) and
                gate.deployment_ready(gate.obj(tx.OBS, "deployment", "alertmanager")) and
                gate.deployment_ready(gate.obj("liveprobe-system", "deployment", "liveprobe-api")))

    def restored(self, report):
        dep = gate.obj(tx.TARGET, "deployment", "inventory")
        pod, status = gate.pod()
        c = gate.inventory_container(dep)
        gate.require(gate.deployment_ready(dep) and gate.profile(dep) is None and
                     not dep["spec"]["template"]["spec"].get("volumes") and
                     status.get("imageID", "").endswith(c["image"].split("@")[-1]),
                     "baseline inventory not ready")
        gate.require(self.rule_loaded is not None and
                     self.rule_loaded(report.get("baseline_oom_rule_for", "30s")) is True and
                     self.faults_clear is not None and self.faults_clear() is True,
                     "baseline rule or fault state uncertain")
        for ns, name in ((tx.TARGET, "order"), (tx.OBS, "prometheus"),
                         (tx.OBS, "alertmanager"), ("liveprobe-system", "liveprobe-api")):
            gate.require(gate.deployment_ready(gate.obj(ns, "deployment", name)),
                         "baseline deployment unhealthy")
        gate.node_gate()
        gate.baseline()  # three independent order→inventory→DB HTTP 200 checks
        return True
