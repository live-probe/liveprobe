"""Background watchers: Pod watch, Event watch, Endpoint poller. Each runs in its own daemon thread."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import k8s as K
from .config import AppConfig
from .incidents import Signal
from .flows import FlowStore
from .network_signals import NetworkSignalAdapter

log = logging.getLogger(__name__)

SIGNAL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CrashLoopBackOff"}
EVENT_REASON_MAP = {"Failed": None, "BackOff": None}  # message decides the signal reason

Emit = Callable[[Signal], None]


class Watchers:
    def __init__(self, kube: K.Kube, cfg: AppConfig, emit: Emit, *, flow_store: FlowStore | None = None):
        self.kube, self.cfg, self.emit = kube, cfg, emit
        self.ns = cfg.target_namespace
        self._stop = threading.Event()
        self._empty_since: dict[str, float] = {}
        self.status: dict[str, dict] = {}
        self.network = NetworkSignalAdapter(flow_store, kube, cfg, emit) if flow_store is not None else None

    def start(self) -> None:
        if len(self.cfg.target_namespaces) > 1:
            for ns in self.cfg.target_namespaces:
                child = Watchers(self.kube, self.cfg.model_copy(update={"target_namespaces": [ns]}), self.emit)
                child._stop, child.status = self._stop, self.status
                child._start_loops(suffix="/" + ns)
            if self.network is not None:
                threading.Thread(target=self._guard, args=("network_poller", self._network_loop), name="network_poller", daemon=True).start()
        else:
            self._start_loops()

    def _start_loops(self, suffix: str = "") -> None:
        loops = [("pod_watch", self._pod_loop), ("event_watch", self._event_loop),
                 ("endpoint_poller", self._endpoint_loop)]
        if self.network is not None:
            loops.append(("network_poller", self._network_loop))
        for name, fn in loops:
            name += suffix
            threading.Thread(target=self._guard, args=(name, fn), name=name, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _guard(self, name: str, fn: Callable[[], None]) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.status[name] = {"state": "running", "since": datetime.now(timezone.utc).isoformat()}
                fn()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                self.status[name] = {"state": "reconnecting", "error": f"{type(e).__name__}: {str(e)[:200]}"}
                log.warning("%s error: %s; reconnect in %.0fs", name, type(e).__name__, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    # --- pods ----------------------------------------------------------------
    def handle_pod(self, pod: dict) -> None:
        if (pod.get("metadata") or {}).get("deletionTimestamp"):
            return
        reasons = [r for r in K.pod_waiting_reasons(pod) if r in SIGNAL_REASONS]
        if not reasons:
            return
        dep = K.deployment_for_pod(self.kube, self.ns, pod)
        if not dep:
            return
        for r in sorted(set(reasons)):
            self.emit(Signal(self.ns, "Deployment", dep, r, "pod_watch", {"pod": pod["metadata"]["name"]}))

    def _pod_loop(self) -> None:
        from kubernetes import watch
        from kubernetes.client import ApiException
        lst = self.kube.core.list_namespaced_pod(self.ns)
        rv = lst.metadata.resource_version
        for p in lst.items:
            self.handle_pod(self.kube.to_dict(p))
        w = watch.Watch()
        while not self._stop.is_set():
            try:
                for ev in w.stream(self.kube.core.list_namespaced_pod, self.ns, resource_version=rv,
                                   timeout_seconds=300, allow_watch_bookmarks=True):
                    obj = ev["object"]
                    if ev["type"] == "BOOKMARK":  # raw dict, not a V1Pod: only advance the resume point
                        rv = _bookmark_rv(obj) or rv
                        continue
                    rv = obj.metadata.resource_version
                    if ev["type"] in ("ADDED", "MODIFIED"):
                        self.handle_pod(self.kube.to_dict(obj))
            except ApiException as e:
                if e.status == 410:  # resourceVersion too old -> relist
                    return
                raise

    # --- events ----------------------------------------------------------------
    def handle_event(self, e: dict) -> None:
        io = e.get("involvedObject") or {}
        if io.get("kind") != "Pod" or e.get("type") != "Warning":
            return
        msg = (e.get("message") or "")
        reason = e.get("reason")
        sig = None
        if reason in ("Failed", "BackOff") and ("pull" in msg.lower() or "image" in msg.lower()):
            sig = "ImagePullBackOff" if reason == "BackOff" else "ErrImagePull"
        elif reason == "BackOff" and "restarting failed container" in msg.lower():
            sig = "CrashLoopBackOff"
        if not sig:
            return
        pod = self.kube.get_pod(self.ns, io.get("name"))
        if not pod or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return
        # a same-name Pod recreated since the Event must not inherit it (owner-aware, UID-exact)
        if io.get("uid") and (pod.get("metadata") or {}).get("uid") not in (None, io["uid"]):
            return
        # only signal if the pod is *currently* in a failing state (ignores stale events)
        if not set(K.pod_waiting_reasons(pod)) & SIGNAL_REASONS:
            return
        dep = K.deployment_for_pod(self.kube, self.ns, pod)
        if dep:
            self.emit(Signal(self.ns, "Deployment", dep, sig, "event_watch", {"pod": io.get("name"), "event": reason}))

    def _event_loop(self) -> None:
        from kubernetes import watch
        from kubernetes.client import ApiException
        lst = self.kube.core.list_namespaced_event(self.ns)
        rv = lst.metadata.resource_version  # start from now: do not replay history
        w = watch.Watch()
        while not self._stop.is_set():
            try:
                for ev in w.stream(self.kube.core.list_namespaced_event, self.ns, resource_version=rv,
                                   timeout_seconds=300):
                    obj = ev["object"]
                    if ev["type"] == "BOOKMARK":
                        rv = _bookmark_rv(obj) or rv
                        continue
                    rv = obj.metadata.resource_version
                    if ev["type"] in ("ADDED", "MODIFIED"):
                        self.handle_event(self.kube.to_dict(obj))
            except ApiException as e:
                if e.status == 410:
                    return
                raise

    # --- optional Hubble/Retina flows -----------------------------------------
    def poll_network_once(self) -> int:
        if self.network is None:
            return 0
        emitted = self.network.poll_once()
        self.status["network_poller"] = {
            "state": "degraded" if self.network.last_errors else
                     "stale" if self.network.store.freshness()["stale"] else "running",
            "errors": list(self.network.last_errors), "emitted": emitted,
        }
        return emitted

    def _network_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_network_once()
            self._stop.wait(self.cfg.endpoint_poll_interval_seconds)

    # --- endpoints -----------------------------------------------------------
    def poll_endpoints_once(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        pods = self.kube.list_pods(self.ns)
        for svc in self.kube.list_services(self.ns):
            name = svc["metadata"]["name"]
            selector = (svc.get("spec") or {}).get("selector") or {}
            if not selector:
                continue
            ready = K.ready_addresses(self.kube.get_endpoints(self.ns, name))
            if ready > 0:
                self._empty_since.pop(name, None)
                continue
            first = self._empty_since.setdefault(name, now)
            if now - first < self.cfg.endpoint_grace_seconds:
                continue
            matching = [p for p in pods if K.labels_match(selector, p["metadata"].get("labels") or {})]
            young = [p for p in matching
                     if (K.parse_ts(p["metadata"].get("creationTimestamp")) or datetime.now(timezone.utc))
                     > datetime.now(timezone.utc).replace(microsecond=0) - _td(180)]
            if matching and young:
                continue  # pods still starting up; not an endpoint-loss incident yet
            self.emit(Signal(self.ns, "Service", name, "NoEndpoints", "endpoint_poller",
                             {"selector": selector, "matching_pods": len(matching),
                              "empty_for_s": int(now - first)}))

    def _endpoint_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_endpoints_once()
            self._stop.wait(self.cfg.endpoint_poll_interval_seconds)


def _bookmark_rv(obj) -> str | None:
    md = (obj.get("metadata") if isinstance(obj, dict) else None) or {}
    return md.get("resourceVersion")


def _td(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)
