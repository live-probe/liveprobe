"""Signals -> incidents: fingerprinting, merge window, cooldown suppression."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import audit
from .models import INCIDENT_OPEN_STATES, Incident

log = logging.getLogger(__name__)

_ingest_lock = threading.Lock()

# reason classes: signals of the same class after an incident closed are treated as residue
REASON_CLASS = {
    "ImagePullBackOff": "image", "ErrImagePull": "image", "InvalidImageName": "image",
    "TargetWorkloadImagePullFailing": "image",
    "CrashLoopBackOff": "crash",
    "NoEndpoints": "endpoints", "TargetServiceNoEndpoints": "endpoints",
    "High5xx": "http5xx", "OrderHigh5xxRate": "http5xx",
    "InventoryHigh5xxRate": "http5xx", "TargetHigh5xxRate": "http5xx", "ApplicationException": "http5xx",
}
FAMILY_HINT = {"image": "image_pull", "crash": "crashloop", "endpoints": "endpoint_loss", "http5xx": "http_5xx"}


@dataclass
class Signal:
    namespace: str
    kind: str          # Deployment | Service | App
    name: str
    reason: str
    source: str        # pod_watch | event_watch | endpoint_poller | alertmanager
    detail: dict = field(default_factory=dict)


def fingerprint(sig: Signal, cfg: AppConfig) -> tuple[str, str]:
    """Returns (fingerprint, workload). Caller-side 5xx maps to its (single) callee via the dependency map."""
    workload = sig.name
    # Preserve pre-P18 caller/callee correlation only for the archived single-target installation.
    # In multi-environment deployments a 5xx and an exception identify the SAME emitting workload.
    if cfg.target_namespaces == ["liveprobe-target"] and REASON_CLASS.get(sig.reason) == "http5xx" and sig.reason != "ApplicationException":
        callees = cfg.dependencies.get(sig.name) or []
        if len(callees) == 1:
            workload = callees[0]
    return f"{sig.namespace}/{workload}", workload


def ingest(s: Session, cfg: AppConfig, sig: Signal, now: datetime | None = None, *, kube=None) -> tuple[Incident | None, str]:
    """Create or merge. Returns (incident, outcome) with outcome in created|merged|suppressed."""
    now = now or datetime.now(timezone.utc)
    fp, workload = fingerprint(sig, cfg)
    rclass = REASON_CLASS.get(sig.reason, sig.reason)
    # The thread lock alone was released before the caller's commit, so a webhook
    # thread and a watch thread (or two replicas) could both miss each other's
    # uncommitted insert. This transaction-scoped advisory lock is held until
    # commit/rollback, serialising same-fingerprint ingest across sessions. It is
    # taken before the thread lock so a waiter never blocks unrelated fingerprints.
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "incident:" + fp})
    with _ingest_lock:
        open_inc = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_OPEN_STATES),
                                   Incident.created_at >= now - timedelta(seconds=cfg.merge_window_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if not open_inc:
            # Past the merge window, an incident that is STILL open for the same reason class absorbs the repeat
            # (a stalled analysis must not fan out into one incident per alert repeat).
            reasons = [r for r, cls in REASON_CLASS.items() if cls == rclass] + [rclass]
            open_inc = s.scalars(
                select(Incident).where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_OPEN_STATES),
                                       or_(*(Incident.signal_reasons.contains([r]) for r in reasons)))
                .order_by(Incident.id.desc()).limit(1)
            ).first()
        if open_inc:
            open_inc.signal_count += 1
            if sig.reason not in open_inc.signal_reasons:
                open_inc.signal_reasons = [*open_inc.signal_reasons, sig.reason]
                audit(s, "watcher", "signal_merged", open_inc.id, source=sig.source, reason=sig.reason,
                      object=f"{sig.kind}/{sig.name}")
            open_inc.updated_at = now
            return open_inc, "merged"

        # Caller 5xx is supporting evidence for a dependency incident, not
        # sufficient by itself to open an inventory incident. Alertmanager can
        # repeat a stale firing alert after the selector has been repaired.
        if cfg.target_namespaces == ["liveprobe-target"] and rclass == "http5xx" and sig.reason != "ApplicationException":
            return None, "suppressed"

        # Interrupted analysis is deliberately never replayed. Suppress the
        # same still-firing signal for a full merge window after restart,
        # rather than creating a second incident when the short normal
        # post-resolution cooldown expires.
        interrupted = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.status == "failed",
                                   Incident.status_reason.like("LiveProbe restarted while incident%"),
                                   Incident.closed_at >= now - timedelta(seconds=cfg.merge_window_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if interrupted and any(REASON_CLASS.get(r, r) == rclass for r in interrupted.signal_reasons):
            return interrupted, "suppressed"

        recent_closed = s.scalars(
            select(Incident).where(Incident.fingerprint == fp, Incident.closed_at.is_not(None),
                                   Incident.closed_at >= now - timedelta(seconds=cfg.close_cooldown_seconds))
            .order_by(Incident.id.desc()).limit(1)
        ).first()
        if recent_closed and any(REASON_CLASS.get(r, r) == rclass for r in recent_closed.signal_reasons):
            return recent_closed, "suppressed"

        system = None
        if kube is not None:
            try:
                dep = kube.get_deployment(sig.namespace, workload)
                system = (((dep or {}).get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/part-of") or None
            except Exception:
                log.warning("system label unavailable ns=%s workload=%s", sig.namespace, workload)
        inc = Incident(
            system=system, fingerprint=fp, family_hint=FAMILY_HINT.get(rclass), namespace=sig.namespace, workload=workload,
            status="open", title=f"{sig.reason} on {sig.kind}/{sig.name}", signal_count=1,
            signal_reasons=[sig.reason], created_at=now, updated_at=now,
        )
        s.add(inc)
        s.flush()
        audit(s, "watcher", "incident_created", inc.id, source=sig.source, reason=sig.reason,
              object=f"{sig.kind}/{sig.name}", fingerprint=fp)
        return inc, "created"
