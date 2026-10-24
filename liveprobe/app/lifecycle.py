"""Guarded incident state changes and durable workflow attempts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import Incident, WorkflowRun

TRANSITIONS = {
    "open": {"analyzing", "failed"},
    "analyzing": {"action_pending", "acting", "resolved", "handed_off", "failed"},
    "action_pending": {"acting", "handed_off", "failed"},
    "acting": {"resolved", "handed_off", "failed"},
    "resolved": set(),
    "handed_off": set(),
    "failed": set(),
}


def transition(inc: Incident, status: str) -> None:
    if status not in TRANSITIONS.get(inc.status, set()):
        raise ValueError(f"invalid incident transition {inc.status} -> {status}")
    inc.status = status
    if not TRANSITIONS[status]:
        inc.closed_at = datetime.now(timezone.utc)


def update_run(s: Session, incident_id: int, status: str, phase: str, reason: str | None = None) -> None:
    # One active attempt per incident in the single-worker deployment.
    from sqlalchemy import select
    run = s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == incident_id,
                                              WorkflowRun.status == "running").order_by(WorkflowRun.id.desc()))
    if run is None and status == "running":
        s.add(WorkflowRun(incident_id=incident_id, status=status, phase=phase, snapshot={}, reason=reason))
    elif run is not None:
        run.status, run.phase, run.reason = status, phase, reason
