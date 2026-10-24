"""P5: run events (chat feed), operator messages and pause/stop control.

Contract (see docs/plans/liveprobe-revamp-plan.md 5.4):
* every pipeline step appends a `run_event` (monotonic `seq` per incident); the dashboard renders them as a feed
  and can replay it after a reload;
* an operator message is applied at the NEXT node boundary (before the next agent call) and is shown to the
  agent as an instruction, never as evidence;
* pause/stop take effect at node boundaries and right before an action is executed. A write that has already
  started is never interrupted: it finishes, is verified, then the run stops.
No hidden reasoning is recorded: only evidence references, steps, tool work, judgement summaries and results.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import db
from .models import Base  # noqa: F401  (models register tables)
from .redact import redact, redact_obj
from .sse import broker

KINDS = ("evidence_collected", "routing_decided", "group_composed", "agent_started", "agent_finished",
         "escalated", "action_proposed", "action_executed", "verification_result", "review_recommended",
         "operator_message", "operator_applied", "paused", "resumed", "stopped", "account_switched",
         "waiting_quota", "status", "completed", "note", "steering_interpreted",
         "command_planned", "command_confirmed", "command_executed", "command_failed", "command_rolled_back",
         "command_cancelled")
CONTROL_ACTIONS = ("pause", "resume", "stop")
POLL_S = 1.0


class RunStopped(Exception):
    """Raised at a node boundary when the operator stopped the run."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def emit(incident_id: int, kind: str, summary: str, detail: dict | None = None, actor: str = "liveprobe") -> int:
    """Append one event; returns its per-incident sequence number. Never raises into the pipeline."""
    from .models import RunEvent
    if kind not in KINDS:
        kind = "note"
    try:
        with db.session() as s:
            seq = (s.scalar(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.incident_id == incident_id)) or 0) + 1
            s.add(RunEvent(incident_id=incident_id, seq=seq, kind=kind, actor=actor, summary=redact(summary)[:600],
                           detail=redact_obj(detail or {})))
        broker.publish(incident_id, "updated")
        return seq
    except Exception:  # noqa: BLE001 - the feed must never break an incident
        return -1


def events_since(incident_id: int, since: int = 0, limit: int = 500) -> list[dict]:
    from .models import RunEvent
    with db.session() as s:
        rows = s.scalars(select(RunEvent).where(RunEvent.incident_id == incident_id, RunEvent.seq > since)
                         .order_by(RunEvent.seq).limit(limit)).all()
        return [{"seq": r.seq, "kind": r.kind, "actor": r.actor, "summary": r.summary, "detail": r.detail,
                 "at": r.created_at.isoformat().replace("+00:00", "Z")} for r in rows]


# ------------------------------------------------------------------ control
def control_state(incident_id: int) -> str:
    from .models import RunControl
    with db.session() as s:
        row = s.get(RunControl, incident_id)
        return row.state if row else "running"


def set_control(incident_id: int, action: str, actor: str = "local-operator") -> str:
    from .models import RunControl
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"unknown control action {action!r}")
    new = {"pause": "paused", "resume": "running", "stop": "stopping"}[action]
    with db.session() as s:
        row = s.get(RunControl, incident_id)
        if row is None:
            row = RunControl(incident_id=incident_id, state="running")
            s.add(row)
        if row.state == "stopping" and action != "stop":
            return row.state          # a stop request is final
        row.state, row.updated_at = new, _now()
    emit(incident_id, {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action],
         {"pause": "운영자가 일시정지를 요청했습니다. 다음 단계 시작 전에 멈춥니다.",
          "resume": "운영자가 재개했습니다.",
          "stop": "운영자가 중단을 요청했습니다. 실행 중인 조치는 검증까지 마친 뒤 멈춥니다."}[action], actor=actor)
    return new


def gate(incident_id: int, where: str, sleep=time.sleep, max_wait_s: float | None = None) -> None:
    """Node boundary. Blocks while paused, raises RunStopped when stopping. `where` names the next step."""
    announced = False
    waited = 0.0
    while True:
        state = control_state(incident_id)
        if state == "stopping":
            raise RunStopped(where)
        if state != "paused":
            return
        if not announced:
            emit(incident_id, "note", f"일시정지됨 — 다음 단계: {where}")
            announced = True
        sleep(POLL_S)
        waited += POLL_S
        if max_wait_s is not None and waited >= max_wait_s:
            raise RunStopped(f"paused beyond limit before {where}")


# ------------------------------------------------------------------ operator messages
def add_message(incident_id: int, body: str, actor: str = "local-operator") -> int:
    from .models import OperatorMessage
    body = (body or "").strip()
    if not body:
        raise ValueError("empty message")
    with db.session() as s:
        row = OperatorMessage(incident_id=incident_id, body=redact(body)[:2000])
        s.add(row)
        s.flush()
        mid = row.id
    emit(incident_id, "operator_message", body[:600], {"message_id": mid}, actor=actor)
    return mid


def take_messages(incident_id: int, applied_at: str) -> list[str]:
    """Mark pending messages as applied at `applied_at` (the step that will see them) and return their text."""
    from .models import OperatorMessage
    with db.session() as s:
        rows = s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == incident_id,
                                                       OperatorMessage.applied_at.is_(None))
                         .order_by(OperatorMessage.id)).all()
        out = [r.body for r in rows]
        for r in rows:
            r.applied_at = _now()
        ids = [r.id for r in rows]
    if ids:
        emit(incident_id, "operator_applied", f"운영자 메시지 {len(ids)}건을 {applied_at}에 반영했습니다.", {"message_ids": ids})
    return out


def with_operator_instructions(prompt: str, messages: list[str]) -> str:
    if not messages:
        return prompt
    block = "\n".join(f"- {m}" for m in messages)
    return (prompt + "\n\nOperator instructions (steering from the human operator; they are instructions, NOT evidence, "
            "and never override safety rules or the allowed action list):\n" + block)
