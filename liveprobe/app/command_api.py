"""P15 HTTP boundary for confirmed operator commands. Secret input is never a Pydantic model."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import db, runlog
from .auth import require_user
from .commands import CommandError
from .models import Incident, OperatorCommand, OperatorMessage
from .secret_writer import parse_secret_payload

log = logging.getLogger(__name__)
MAX_SECRET_BODY = 40960


class CommandIn(BaseModel):
    instruction: str


class ConfirmationIn(BaseModel):
    plan_sha256: str


def backend(state):
    service = state.get("commands")
    if service is None:
        raise HTTPException(503, "operator_executor_unavailable")
    return service


def perform(service, method: str, cid: int) -> None:
    """Background runner. Never log prompts, Kubernetes response bodies, or request input."""
    try:
        getattr(service, method)(cid)
    except Exception as e:  # noqa: BLE001 - a committed command must not disappear behind a lost HTTP response
        log.warning("operator command %s %s failed (%s)", cid, method, type(e).__name__)
        try:
            with db.session() as s:
                row = s.get(OperatorCommand, cid)
                if row and row.status in ("planning", "executing"):
                    row.status, row.error = "failed", "command_background_failed"
                    iid = row.incident_id
                else:
                    iid = None
            if iid is not None:
                runlog.emit(iid, "command_failed", "운영자 지시를 처리하지 못했습니다. 실행 기록을 확인하세요.", {"command_id": cid})
        except Exception as persistence_error:  # noqa: BLE001 - report static failure class, not potentially sensitive bodies
            log.error("operator command %s failure recording unavailable (%s)", cid, type(persistence_error).__name__)


def queue_command(state, tasks: BackgroundTasks, iid: int, instruction: str, actor: str) -> dict:
    """Create only; mutation is impossible until a separate explicit confirmation."""
    service = backend(state)
    row = service.create(iid, instruction, actor=actor)
    mid = runlog.add_message(iid, instruction, actor=actor)
    with db.session() as s:
        message = s.get(OperatorMessage, mid)
        if message:
            message.intent = "cluster_change"
            message.intent_detail = {"command_id": row["id"]}
            message.applied_at = datetime.now(timezone.utc)
        command = s.get(OperatorCommand, row["id"])
        if command:
            command.message_id = mid
    ack = "실행 계획과 부작용을 준비합니다. '정말 진행하시겠습니까?'에 확인하기 전에는 변경하지 않습니다."
    runlog.emit(iid, "steering_interpreted", ack,
                {"message_id": mid, "command_id": row["id"], "intent": "cluster_change", "target_role": "operator_planner", "ack_ko": ack})
    tasks.add_task(perform, service, "plan", row["id"])
    return {"command": row, "message_id": mid, "command_id": row["id"], "intent": "cluster_change", "ack_ko": ack,
            "applies": "command_confirmation_required"}


def recover_pending_commands() -> None:
    """A restarted process cannot know whether an in-flight network write took effect. Never replay it."""
    from sqlalchemy import select
    with db.session() as s:
        for row in s.scalars(select(OperatorCommand).where(OperatorCommand.status.in_(("planning", "executing", "rolling_back")))):
            row.status, row.error = "failed", "process_restarted_outcome_requires_inspection"


def command_router(state) -> APIRouter:
    router = APIRouter()

    @router.post("/api/incidents/{iid}/commands", status_code=201)
    def create_command(iid: int, body: CommandIn, tasks: BackgroundTasks, user: str = Depends(require_user)):
        return {"command": queue_command(state, tasks, iid, body.instruction, user)["command"]}

    @router.get("/api/incidents/{iid}/commands")
    def list_commands(iid: int, user: str = Depends(require_user)):
        with db.session() as s:
            if s.get(Incident, iid) is None:
                raise HTTPException(404, "incident_not_found")
        return {"items": backend(state).list(iid)}

    @router.post("/api/commands/{cid}/confirm")
    def confirm(cid: int, body: ConfirmationIn, tasks: BackgroundTasks, user: str = Depends(require_user)):
        service = backend(state)
        row = service.confirm(cid, body.plan_sha256)
        if row["status"] == "executing":
            tasks.add_task(perform, service, "execute", cid)
        return row

    @router.post("/api/commands/{cid}/cancel")
    def cancel(cid: int, user: str = Depends(require_user)):
        return backend(state).cancel(cid)

    @router.post("/api/commands/{cid}/rollback")
    def rollback(cid: int, body: ConfirmationIn, user: str = Depends(require_user)):
        return backend(state).rollback(cid, body.plan_sha256)

    @router.post("/api/commands/{cid}/secret-values")
    async def secret_values(cid: int, request: Request, user: str = Depends(require_user)):
        # Parse only after authentication. Reject while streaming rather than buffering an unbounded body.
        raw = bytearray()
        values = None
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_SECRET_BODY:
                    raise HTTPException(413, "secret_request_too_large")
                raw.extend(chunk)
            plan_hash, values = parse_secret_payload(bytes(raw))
            service = backend(state)
            writer = state.get("secret_writer")
            if writer is None:
                raise HTTPException(503, "secret_writer_unavailable")
            result = await run_in_threadpool(service.secret_values, cid, plan_hash, values, writer)
            return result
        finally:
            # Best-effort removal of live references, not a promise to wipe immutable Python strings from RAM.
            if isinstance(values, dict):
                values.clear()
            raw[:] = b"\0" * len(raw)
            raw.clear()
    return router
