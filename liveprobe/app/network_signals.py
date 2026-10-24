"""Read-only Hubble/Retina flow adapter for the existing incident Signal API.

FlowStore snapshots are rolling windows, NOT monotonically increasing counters.
Only observed DROPPED/ERROR records count; silence is never proof of recovery.
The adapter resolves target Pods through their real ReplicaSet owner, never by
stripping a hash from a name or trusting an arbitrary flow namespace/label.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import replace
from typing import Callable

from . import k8s as K
from .config import AppConfig
from .flows import DROP_ALERT_MIN, FlowStore, aggregate
from .incidents import Signal

log = logging.getLogger(__name__)
POLL_TIMEOUT_S = 10.0


class NetworkSignalAdapter:
    """Read-only bridge to Engine.on_signal (Deployment incidents only).

    The map and this adapter share DROP_ALERT_MIN and workload-level edge folding.
    Each pair emits once per merge window, then needs five *new* bad observations;
    rolling count decreases, forwarded traffic and silence do not rearm old drops.
    UUIDs distinguish Hubble observations from reconnect replay; without a UUID,
    exact duplicate FlowRecs are conservatively treated as one observation.
    Only successful emit callbacks consume records. Failures are retryable and
    surfaced through last_errors plus the watcher's independent network status.
    """

    def __init__(self, store: FlowStore, kube: K.Kube, cfg: AppConfig,
                 emit: Callable[[Signal], None], clock: Callable[[], float] = time.time):
        self.store, self.kube, self.cfg, self.emit, self.clock = store, kube, cfg, emit, clock
        # (last successful emission time, latest consumed bad-flow timestamp).
        # A forwarded heartbeat must never advance the bad-flow watermark.
        self._emitted: dict[tuple[str, str], tuple[float, float]] = {}
        self.last_errors: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self._observation: Future | None = None
        self._delivery: tuple[Future, tuple[str, str], float] | None = None

    def _error(self, stage: str, exc: Exception) -> None:
        self.last_errors.append({"stage": stage, "error": type(exc).__name__})
        log.warning("network signal %s failed: %s", stage, type(exc).__name__)

    def _targets(self, deadline: float) -> tuple[dict[str, str], list[dict[str, str]]]:
        # Worker-private data only: an expired lookup must not emit or update
        # last_errors/watermarks after the polling caller has already returned.
        targets: dict[str, str] = {}
        errors = []
        for ns in self.cfg.target_namespaces:
            try:
                part, failures = self._targets_namespace(ns, deadline)
                targets.update(part)
                errors.extend(failures)
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise
                errors.append({"stage": "namespace_inventory", "namespace": ns, "error": type(exc).__name__})
        return targets, errors

    def _targets_namespace(self, ns: str, deadline: float):
        # The owner resolution and Service selector joins stay namespace-local.
        errors = []
        def read(method, *args):
            if time.monotonic() >= deadline:
                raise TimeoutError("network poll deadline exceeded")
            result = method(*args)
            if time.monotonic() >= deadline:
                raise TimeoutError("network poll deadline exceeded")
            return result
        def error(stage, exc):
            if time.monotonic() >= deadline:
                raise TimeoutError("network poll deadline exceeded") from None
            errors.append({"stage": stage, "error": type(exc).__name__})
        targets: dict[str, str] = {}
        pods = [p for p in read(self.kube.list_pods, ns)
                if (p.get("metadata") or {}).get("namespace", ns) == ns
                and not (p.get("metadata") or {}).get("deletionTimestamp")]
        for pod in pods:
            md = pod.get("metadata") or {}
            try:
                rs_name = K.owner_of(pod, "ReplicaSet")
                rs = read(self.kube.get_replicaset, ns, rs_name) if rs_name else None
                dep = K.owner_of(rs, "Deployment") if rs else None
            except Exception as exc:  # isolate an unavailable owner from other workloads
                error("pod_owner", exc)
                continue
            if not dep:
                continue
            wid = f"{ns}/Deployment/{dep}"
            targets[f"{ns}/pod/{md['name']}"] = wid
            ip = (pod.get("status") or {}).get("podIP")
            if ip:
                targets[f"ip/{ip}"] = wid
        try:
            services = read(self.kube.list_services, ns)
        except Exception as exc:
            error("services", exc)
            services = []
        for svc in services:
            md, spec = svc.get("metadata") or {}, svc.get("spec") or {}
            if md.get("namespace", ns) != ns or md.get("deletionTimestamp"):
                continue
            matching = [p for p in pods if K.labels_match(spec.get("selector") or {},
                                                         (p.get("metadata") or {}).get("labels") or {})]
            owners = {targets.get(f"{ns}/pod/{p['metadata']['name']}") for p in matching}
            if len(owners) != 1 or None in owners:
                continue  # no guess for selector-less/ambiguous/unresolved Services
            wid = owners.pop()
            if wid is None:
                continue
            targets[f"{ns}/Service/{md['name']}"] = wid
            for ip in spec.get("clusterIPs") or [spec.get("clusterIP")]:
                if ip and ip != "None":
                    targets[f"ip/{ip}"] = wid
        return targets, errors

    def _observe_targets(self, deadline: float):
        # Keep at most one in-flight observation, including across timeouts.
        # A hung client therefore cannot create an unbounded worker backlog.
        if self._observation is not None and not self._observation.done():
            raise TimeoutError("previous network observation is still in flight")
        future = Future()
        self._observation = future

        def observe():
            try:
                future.set_result(self._targets(deadline))
            except Exception as exc:
                future.set_exception(exc)

        threading.Thread(target=observe, name="network-observation", daemon=True).start()
        result = future.result(timeout=max(0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError("network poll deadline exceeded")
        return result

    def poll_once(self) -> int:
        """Return successful intake calls; serialize snapshot/emit/watermark updates."""
        deadline = time.monotonic() + POLL_TIMEOUT_S
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return 0  # another caller still owns intake/state; do not mutate it
        try:
            return self._poll_once(deadline)
        finally:
            self._lock.release()

    def _finish_delivery(self, deadline: float) -> bool | None:
        """Reap a single admitted delivery. A timeout is pending, never a retry.

        Intake can have committed before returning, so killing/replaying a timed-out
        callback is unsafe. Its worker only resolves a Future; the polling caller
        alone changes errors/watermarks after observing a definitive outcome.
        """
        assert self._delivery is not None
        future, pair, last_ts = self._delivery
        done, _ = wait((future,), timeout=max(0, deadline - time.monotonic()))
        if future not in done:
            self._error("delivery_pending", TimeoutError("signal delivery remains in flight"))
            return None
        try:
            future.result()
        except Exception as exc:
            self._delivery = None
            self._error("emit", exc)
            return False
        self._delivery = None
        self._emitted[pair] = (self.clock(), last_ts)
        return True

    def _start_delivery(self, sig: Signal, pair: tuple[str, str], last_ts: float) -> None:
        future = Future()
        self._delivery = (future, pair, last_ts)

        def deliver():
            try:
                self.emit(sig)
                future.set_result(None)
            except Exception as exc:
                future.set_exception(exc)

        threading.Thread(target=deliver, name="network-delivery", daemon=True).start()

    def _poll_once(self, deadline: float) -> int:
        self.last_errors = []
        emitted = 0
        if self._delivery is not None:
            outcome = self._finish_delivery(deadline)
            if outcome is None:
                return 0
            emitted += int(outcome)
        now = self.clock()
        retention = self.cfg.merge_window_seconds + self.store.window_s
        self._emitted = {pair: state for pair, state in self._emitted.items()
                         if now - state[0] <= retention}
        try:
            if self.store.freshness()["stale"]:
                return emitted
            flows = self.store.snapshot()
            targets, errors = self._observe_targets(deadline)
            self.last_errors.extend(errors)
        except Exception as exc:  # optional observation must not interrupt other watchers
            self._error("observation", exc)
            return emitted
        bad = []
        seen = set()
        for flow in flows:
            if not now - self.store.window_s <= flow.ts <= now:
                continue  # enforce event-time bounds even for out-of-order stream replay
            identity = flow.flow_id or flow
            if identity in seen:
                continue  # Hubble's reconnect lookback can replay the same observation
            seen.add(identity)
            if flow.verdict not in ("DROPPED", "ERROR"):
                continue
            src, dst = targets.get(flow.src, flow.src), targets.get(flow.dst, flow.dst)
            if flow.src not in targets and flow.dst not in targets:
                continue
            if src == dst:
                continue
            previous = self._emitted.get((src, dst))
            if previous and flow.ts <= previous[1]:
                continue
            bad.append(replace(flow, src=src, dst=dst))
        workloads = set(targets.values())
        for (src, dst), edge in aggregate(bad).items():
            if time.monotonic() >= deadline:
                self._error("observation", TimeoutError("network poll deadline exceeded"))
                break  # never admit a new delivery after the caller's deadline
            previous = self._emitted.get((src, dst))
            if previous and now - previous[0] < self.cfg.merge_window_seconds:
                continue
            if edge.dropped + edge.error < DROP_ALERT_MIN:
                continue
            # One signal per path, preferring the affected destination when both are targets.
            target = dst if dst in workloads else src
            sig = Signal(target.split("/", 2)[0], "Deployment", target.split("/", 2)[2],
                         "NetworkDrop", "hubble", {
                             "from": src, "to": dst, "dropped": edge.dropped, "errors": edge.error,
                             "drop_reasons": dict(edge.drop_reasons), "last_seen": edge.last_ts,
                             "window_s": self.store.window_s,
                         })
            self._start_delivery(sig, (src, dst), edge.last_ts)
            outcome = self._finish_delivery(deadline)
            if outcome is None:
                break
            emitted += int(outcome)
        return emitted
