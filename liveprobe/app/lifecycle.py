"""Guarded incident state changes and durable workflow attempts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import INCIDENT_CLOSED_STATES, Incident, WorkflowRun

TRANSITIONS = {
    "open": {"analyzing", "failed"},
    "analyzing": {"action_pending", "acting", "resolved", "handed_off", "failed"},
    # An operator may close an approval nobody will act on (e.g. an action that already finished elsewhere).
    "action_pending": {"acting", "handed_off", "failed", "closed"},
    "acting": {"resolved", "handed_off", "failed"},
    "resolved": set(),
    # The AI stopped and a person owns it; only that person's explicit close ends it.
    "handed_off": {"closed"},
    "failed": set(),
    "closed": set(),
}


def transition(inc: Incident, status: str) -> None:
    if status not in TRANSITIONS.get(inc.status, set()):
        raise ValueError(f"invalid incident transition {inc.status} -> {status}")
    inc.status = status
    if status in INCIDENT_CLOSED_STATES:
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
