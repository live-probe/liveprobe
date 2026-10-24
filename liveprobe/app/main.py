"""FastAPI application. Shapes follow .hermes/plans/KAN-34-api-contract.md exactly."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError, InterfaceError
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from . import db
from .auth import require_user, require_webhook
from .config import get_app_config, get_settings
from .incidents import Signal
from .models import (Action, AgentRun, AuditLog, Citation, Evidence, Incident, IncidentEvent, RagChunk, RoutingDecision,
                     Setting)
from .model_settings import ModelSettingsIn, RevisionConflict, read_settings, save_settings
from .lifecycle import transition, update_run
from .redact import redact_obj
from . import safety
from .safety import POLICY_MODES
from .sse import broker

log = logging.getLogger("liveprobe")

STATE: dict[str, Any] = {"engine": None, "watchers": None, "kube": None, "ready": False, "rag_chunks": 0,
                         "flows": None, "collector": None}
rag_ingest_stop = threading.Event()


def _start_background() -> None:
    """Heavy init (embedding model, kube client) + threads, off the request path."""
    settings, cfg = get_settings(), get_app_config()
    from .engine import Engine
    from .k8s import Kube
    from .llm import make_provider
    from .rag import RAG, FastEmbedder, index_runbooks
    from .rag_ingest import index_documents, load_public_documents, closed_incident_documents
    from .watcher import Watchers

    rag = None
    try:
        emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
        with db.session() as s:
            new = index_runbooks(s, emb, settings.runbooks_dir)
            STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                            .where(RagChunk.retired_at.is_(None)))
        log.info("RAG runbooks ready: %s new chunks, %s active", new, STATE["rag_chunks"])
        rag = RAG(emb)
        try:
            with db.session() as s:
                result = index_documents(s, emb, load_public_documents())
                STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                                .where(RagChunk.retired_at.is_(None)))
            log.info("RAG licensed public corpus ready: %s documents, %s new chunks", result.documents,
                     result.new_chunks)
        except Exception as e:  # noqa: BLE001
            log.error("RAG public corpus rejected; internal runbooks remain available: %s", type(e).__name__)

        def ingest_closed() -> None:
            while True:
                try:
                    with db.session() as s:
                        result = index_documents(s, emb, closed_incident_documents(s))
                        STATE["rag_chunks"] = s.scalar(select(func.count()).select_from(RagChunk)
                                                        .where(RagChunk.retired_at.is_(None)))
                    if result.documents:
                        log.info("RAG sanitized resolved incidents indexed: %s documents", result.documents)
                except Exception as e:  # noqa: BLE001
                    log.warning("RAG closed incident ingestion skipped: %s", type(e).__name__)
                if rag_ingest_stop.wait(60):
                    break

        threading.Thread(target=ingest_closed, name="rag-closed-ingest", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.error("RAG init failed: %s: %s", type(e).__name__, e)

    kube = Kube(in_cluster=settings.kube_in_cluster)
    from . import policy as _policy
    prof = _policy.resolve_profile(settings.policy_profile, settings.poc_cluster_uid, kube)
    safety.set_profile(prof.name)
    STATE["profile"] = {"active": prof.name, "requested": prof.requested, "downgraded_reason": prof.reason}
    engine = Engine(cfg, settings, kube, make_provider(cfg, settings), rag)
    from .command_api import recover_pending_commands
    from .commands import CommandService
    from .operator_kube import OperatorKube
    from .secret_writer import SecretWriter
    recover_pending_commands()
    STATE.update(commands=None, secret_writer=None)
    try:
        operator = OperatorKube.from_token_file(settings.operator_executor_token_file)
        writer = None
        try:
            writer = SecretWriter.from_token_file(settings.secret_writer_token_file)
        except Exception as e:  # noqa: BLE001 - Secret changes fail closed, ordinary commands remain available
            log.warning("secret writer unavailable (%s)", type(e).__name__)
        STATE.update(commands=CommandService(cfg, engine.provider, operator, secret_writer=writer), secret_writer=writer)
    except Exception as e:  # noqa: BLE001 - never borrow the read-only API or autonomous action identity
        log.warning("operator executor unavailable (%s)", type(e).__name__)
    log.info("safe-action executor identity %s",
             "enabled" if engine.attach_executor(settings.executor_token_file) else "not mounted (typed actions denied)")
    from .interpret import Interpreter, InterpretWorker
    interpret = InterpretWorker(Interpreter(cfg, engine.provider))
    interpret.start()
    engine.interpret_worker = interpret
    STATE["interpret"] = interpret
    engine.recover_on_startup()
    engine.start()
    store = None
    if settings.hubble_relay:
        from .flows import FlowStore, HubbleCollector
        store = FlowStore()
        collector = HubbleCollector(store, settings.hubble_relay)
        collector.start()
        STATE.update(flows=store, collector=collector)
    watchers = Watchers(kube, cfg, engine.on_signal, flow_store=store)
    watchers.start()
    STATE.update(engine=engine, watchers=watchers, kube=kube, ready=True)
    log.info("LiveProbe background services started (provider=%s, target=%s)", cfg.llm.provider,
             cfg.target_namespace)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "kubernetes", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    url = settings.sqlalchemy_url()
    db.run_migrations(url)
    db.init_db(url)
    broker.bind_loop(asyncio.get_running_loop())
    if settings.background:
        rag_ingest_stop.clear()
        threading.Thread(target=_start_background, name="bootstrap", daemon=True).start()
    yield
    rag_ingest_stop.set()
    if STATE.get("collector"):
        STATE["collector"].stop()
    if STATE.get("watchers"):
        STATE["watchers"].stop()
    if STATE.get("engine"):
        STATE["engine"].stop()
    if STATE.get("interpret") and hasattr(STATE["interpret"], "stop"):
        STATE["interpret"].stop()
    for client in (getattr(STATE.get("commands"), "kube", None), STATE.get("secret_writer")):
        if client is not None and hasattr(client, "close"):
            client.close()


app = FastAPI(title="LiveProbe", version="0.1.0", lifespan=lifespan, docs_url=None,
              openapi_url=None)
from .command_api import command_router
from .commands import CommandError
app.include_router(command_router(STATE))


@app.exception_handler(CommandError)
async def command_error(request: Request, exc: CommandError):
    return JSONResponse({"detail": exc.code}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})

@app.get("/api/openapi.json", dependencies=[Depends(require_user)], include_in_schema=False)
def authenticated_openapi():
    return app.openapi()

@app.get("/api/docs", dependencies=[Depends(require_user)], include_in_schema=False)
def authenticated_docs():
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="LiveProbe API")

@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def database_unavailable(request: Request, exc: Exception):
    log.warning("database unavailable: %s", type(exc).__name__)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


def iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ health
@app.get("/api/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/readyz")
def readyz():
    ok = db.db_ok()
    body = {"status": "ok" if ok else "unavailable", "db": ok, "background": STATE["ready"],
            "rag_chunks": STATE["rag_chunks"]}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/api/llm/accounts")
def llm_accounts(user: str = Depends(require_user)):
    """Codex account state for the status bar. Names and availability only: no paths, no credentials."""
    eng = STATE.get("engine")
    prov = getattr(eng, "provider", None)
    if prov is None:
        return {"mode": "not_ready", "accounts": []}
    if hasattr(prov, "status"):
        return {"mode": "pool", **prov.status()}
    return {"mode": "single", "active": "primary", "accounts": [{"name": "primary", "state": "available"}],
            "note": "spare account not configured"}


@app.get("/api/policy/profile")
def policy_profile(user: str = Depends(require_user)):
    return STATE.get("profile") or {"active": safety.active_profile()}


@app.get("/api/review-ledger")
def review_ledger(limit: int = 200, offset: int = 0, reason: str | None = None, incident_id: int | None = None,
                  user: str = Depends(require_user)):
    from . import policy
    return policy.list_entries(limit, offset, reason, incident_id)


@app.get("/api/review-ledger.csv")
def review_ledger_csv(user: str = Depends(require_user)):
    from fastapi.responses import PlainTextResponse
    from . import policy
    return PlainTextResponse(policy.to_csv(policy.list_entries(1000)["items"]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=review-ledger.csv"})


# ------------------------------------------------------------------ run feed and steering (P5)
class MessageIn(BaseModel):
    body: str


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "stop"]


@app.get("/api/incidents/{iid}/events")
def run_events(iid: int, since: int = 0, user: str = Depends(require_user)):
    from . import runlog
    with db.session() as s:
        if s.get(Incident, iid) is None:
            raise HTTPException(404, "incident not found")
    return {"events": runlog.events_since(iid, max(0, since)), "control": runlog.control_state(iid)}


@app.post("/api/incidents/{iid}/messages", status_code=202)
def post_message(iid: int, body: MessageIn, tasks: BackgroundTasks, user: str = Depends(require_user)):
    """Investigation steering at the next boundary; mutations and post-handoff instructions need confirmation."""
    from . import steering
    with db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "incident not found")
        finished = inc.status in ("resolved", "handed_off", "failed")
    if not body.body.strip():
        raise HTTPException(422, "message is empty")
    if len(body.body) > 2000:
        raise HTTPException(422, "message too long (max 2000)")
    if finished or steering.classify(body.body)["intent"] == "cluster_change":
        from .command_api import queue_command
        return queue_command(STATE, tasks, iid, body.body, user)
    out = steering.post(iid, body.body)
    return {**out, "applies": "next_node_boundary"}


@app.post("/api/incidents/{iid}/control")
def control(iid: int, body: ControlIn, user: str = Depends(require_user)):
    from . import runlog
    with db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "incident not found")
        if inc.status in ("resolved", "handed_off", "failed"):
            raise HTTPException(409, f"incident is {inc.status}; nothing is running")
    return {"state": runlog.set_control(iid, body.action)}


# ------------------------------------------------------------------ topology (P3)
_TOPO_CACHE: dict[str, Any] = {"at": 0.0, "inventory": None}


def _inventory(kube) -> dict:
    """Cluster inventory, cached 5s so many dashboards do not each hit the Kubernetes API."""
    import time as _t
    now = _t.monotonic()
    if _TOPO_CACHE.get("inventory") is None or now - _TOPO_CACHE.get("at", 0.0) > 5:
        _TOPO_CACHE["inventory"], _TOPO_CACHE["at"] = kube.cluster_inventory(), now
    return _TOPO_CACHE["inventory"]


@app.get("/api/topology")
def topology(user: str = Depends(require_user)):
    """Cluster resources (Kubernetes API) merged with observed network flows (Hubble). Structure and
    observation are separate edge lists; `freshness` says how current the flow view is."""
    from .flows import FlowStore, build_topology
    kube, store = STATE.get("kube"), STATE.get("flows")
    if kube is None:
        raise HTTPException(503, "cluster client not ready")
    store = store or FlowStore()  # collection disabled: every flow field stays empty and freshness says never_connected
    with db.session() as s:
        incidents = [{"id": i.id, "namespace": i.namespace, "workload": i.workload, "status": i.status,
                      "severity_level": i.severity_level, "title": i.title,
                      "claimed": bool((i.handoff or {}).get("claimed_by")),
                      "updated_ts": i.updated_at.timestamp() if i.updated_at else 0}
                     for i in s.scalars(select(Incident).where(Incident.status.not_in(("resolved", "failed"))))]
    return build_topology(_inventory(kube), store.snapshot(), store.freshness(), incidents)


# ------------------------------------------------------------------ operations overview (P15)
_OV_METRICS: dict[str, Any] = {"at": 0.0, "url": None, "values": {}, "errors": {}}
_OV_LOCK = threading.Lock()


@app.get("/api/overview")
def operations_overview(user: str = Depends(require_user)):
    from datetime import timedelta
    from time import monotonic
    from zoneinfo import ZoneInfo
    from . import overview as ov
    from .models import RunEvent
    from .narrative import stage_of
    kube = STATE.get("kube")
    if kube is None:
        raise HTTPException(503, "cluster client not ready")
    settings = get_settings()
    # Only source queries are cached: incident pins and AI stages must change on every request.
    with _OV_LOCK:
        if _OV_METRICS["url"] != settings.prometheus_url or monotonic() - _OV_METRICS["at"] > 10:
            values, errors = ov.fetch_metrics(settings)
            _OV_METRICS.update(at=monotonic(), url=settings.prometheus_url, values=values, errors=errors)
        values, errors = _OV_METRICS["values"], dict(_OV_METRICS["errors"])
    try:
        inv = _inventory(kube)
    except Exception as e:  # noqa: BLE001 - keep source failures visible even if inventory is unavailable
        inv = {}
        errors["inventory"] = type(e).__name__
    try:
        events = kube.cluster_warning_events()
    except Exception as e:  # noqa: BLE001
        events = []
        errors["events"] = type(e).__name__
    with db.session() as s:
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        rows = list(s.scalars(select(Incident).where(or_(
            Incident.status.in_(("open", "analyzing", "action_pending", "acting")),
            (Incident.status == "handed_off") & (Incident.updated_at >= recent))).order_by(Incident.id.desc()).limit(200)))
        ids = [i.id for i in rows]
        routes = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id.in_(ids)).order_by(RoutingDecision.id))) if ids else []
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id.in_(ids)).order_by(AgentRun.id))) if ids else []
        evs = list(s.scalars(select(RunEvent).where(RunEvent.incident_id.in_(ids)).order_by(RunEvent.seq))) if ids else []
        incidents = [{"id": i.id, "namespace": i.namespace, "workload": i.workload, "status": i.status,
                      "severity_level": i.severity_level, "title": i.title, "created_at": iso(i.created_at),
                      "updated_ts": i.updated_at.timestamp() if i.updated_at else 0,
                      "claimed": bool((i.handoff or {}).get("claimed_by")),
                      "stage": stage_of(i, [r for r in routes if r.incident_id == i.id],
                                        [r for r in runs if r.incident_id == i.id], [e for e in evs if e.incident_id == i.id])}
                     for i in rows]
        day_start = now.astimezone(ZoneInfo("Asia/Seoul")).replace(hour=0, minute=0, second=0, microsecond=0)
        counts = dict(s.execute(select(Incident.status, func.count()).where(Incident.created_at >= day_start)
                                .group_by(Incident.status)).all())
        today = {"total": sum(counts.values()), **{k: counts.get(k, 0) for k in ("resolved", "handed_off", "failed")}}
    return redact_obj(ov.assemble(inv, values, events=events, incidents=incidents, today=today,
                                 errors=errors, store=STATE.get("flows")))


# ------------------------------------------------------------------ incidents
def _agg(s, ids: list[int]) -> dict[int, tuple[int, float | None]]:
    if not ids:
        return {}
    rows = s.execute(select(AgentRun.incident_id, func.count(AgentRun.id),
                            func.count(AgentRun.estimated_cost_usd),
                            func.sum(AgentRun.estimated_cost_usd))
                     .where(AgentRun.incident_id.in_(ids)).group_by(AgentRun.incident_id)).all()
    return {r[0]: (int(r[1]), float(r[3]) if r[1] == r[2] else None) for r in rows}


def _summary(i: Incident, agg: dict[int, tuple[int, float | None]]) -> dict:
    n, cost = agg.get(i.id, (0, 0.0))
    return {
        "id": i.id, "title": i.title, "namespace": i.namespace, "workload": i.workload, "status": i.status,
        "severity_level": i.severity_level, "signal_count": i.signal_count, "created_at": iso(i.created_at),
        "updated_at": iso(i.updated_at), "closed_at": iso(i.closed_at), "total_cost_usd": round(cost, 6) if cost is not None else None,
        "agent_run_count": n,
    }


@app.get("/api/incidents")
def list_incidents(status: str | None = None, limit: int = 50, offset: int = 0, search: str | None = None,
                   user: str = Depends(require_user)):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if search is not None and len(search) > 120:
        raise HTTPException(422, "search must be at most 120 characters")
    with db.session() as s:
        q = select(Incident)
        cq = select(func.count()).select_from(Incident)
        if status:
            q, cq = q.where(Incident.status == status), cq.where(Incident.status == status)
        if search and search.strip():
            term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            match = or_(Incident.title.ilike(f"%{term}%", escape="\\"),
                        Incident.workload.ilike(f"%{term}%", escape="\\"))
            q, cq = q.where(match), cq.where(match)
        total = s.scalar(cq) or 0
        incs = list(s.scalars(q.order_by(Incident.created_at.desc(), Incident.id.desc()).offset(offset).limit(limit)))
        agg = _agg(s, [i.id for i in incs])
        return {"items": [_summary(i, agg) for i in incs], "total": total}


def action_dict(a: Action) -> dict:
    return {"id": a.id, "type": a.type, "target": a.target, "params": a.params, "policy_mode": a.policy_mode,
            "status": a.status, "before_snapshot": a.before_snapshot, "after_state": a.after_state,
            "reason": a.reason, "executed": bool(a.executed), "created_at": iso(a.created_at),
            "updated_at": iso(a.updated_at)}


@app.get("/api/incidents/{iid}")
def get_incident(iid: int, user: str = Depends(require_user)):
    with db.session() as s:
        inc = s.get(Incident, iid)
        if not inc:
            raise HTTPException(404, "incident not found")
        incident = {**_summary(inc, _agg(s, [iid])), "summary": inc.summary, "status_reason": inc.status_reason,
                    "signal_reasons": inc.signal_reasons or [], "handoff": inc.handoff,
                    "family_hint": inc.family_hint}
        evidence = [{"id": e.id, "source": e.source, "target": e.target, "collected_at": iso(e.collected_at),
                     "payload": e.payload, "error": e.error}
                    for e in s.scalars(select(Evidence).where(Evidence.incident_id == iid).order_by(Evidence.id))]
        routing = [{"id": r.id, "level": r.level, "workflow": r.workflow, "features": r.features,
                    "rationale": r.rationale, "policy": r.policy, "confidence": r.confidence, "decided_by": r.decided_by,
                    "created_at": iso(r.created_at)}
                   for r in s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)
                                      .order_by(RoutingDecision.id))]
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
        cites: dict[int, list] = {}
        if runs:
            for c, ch in s.execute(select(Citation, RagChunk).join(RagChunk, RagChunk.id == Citation.chunk_id)
                                   .where(Citation.agent_run_id.in_([r.id for r in runs])).order_by(Citation.id)):
                cites.setdefault(c.agent_run_id, []).append(
                    {"chunk_id": ch.id, "doc": ch.doc, "source": ch.source, "chunk_ix": ch.chunk_ix,
                     "score": c.score, "content": ch.content,
                     "source_uri": (ch.provenance or {}).get("source_uri") if ch.document_id else None,
                     "license": (ch.provenance or {}).get("license") if ch.document_id else None,
                     "attribution": (ch.provenance or {}).get("attribution") if ch.document_id else None,
                     "version_hash": (ch.provenance or {}).get("version_hash") if ch.document_id else None,
                     "locator": ch.locator,
                     "section": ch.section, "line_start": ch.line_start, "line_end": ch.line_end,
                     "char_start": ch.char_start, "char_end": ch.char_end})
        agent_runs = [{"id": r.id, "role": r.role, "provider": r.provider, "model": r.model,
                       "reasoning_effort": r.reasoning_effort, "input_tokens": r.input_tokens,
                       "output_tokens": r.output_tokens, "estimated_cost_usd": r.estimated_cost_usd,
                       "duration_ms": r.duration_ms, "status": r.status, "attempts": r.attempts,
                       "output": r.output, "error": r.error, "created_at": iso(r.created_at),
                       "citations": cites.get(r.id, [])} for r in runs]
        actions = [action_dict(a) for a in
                   s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id))]
        audit_rows = [{"id": a.id, "ts": iso(a.ts), "actor": a.actor, "event": a.event, "detail": a.detail}
                      for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == iid).order_by(AuditLog.id))]
        return redact_obj({"incident": incident, "evidence": evidence, "routing": routing,
                           "agent_runs": agent_runs, "actions": actions, "audit": audit_rows})


# ------------------------------------------------------------------ operator handoff
@app.get("/api/incidents/{iid}/narrative")
def incident_narrative(iid: int, user: str = Depends(require_user)):
    """P15 war-room story: Korean headline, raw items (verbatim) with Korean readings, agent flow graph."""
    from .narrative import build_narrative
    with db.session() as s:
        out = build_narrative(s, iid)
    if out is None:
        raise HTTPException(404, "incident not found")
    worker = STATE.get("interpret")
    if worker is not None:
        if hasattr(worker, "seen"):
            worker.seen(iid)
        if out.get("interpreting"):
            worker.request(iid, idle=True)
    return redact_obj(out)


@app.post("/api/incidents/{iid}/handoff/claim")
def claim_handoff(iid: int, user: str = Depends(require_user)):
    """Acknowledge ownership of an existing handoff; never authorize a cluster action."""
    with db.session() as s:
        inc = s.scalar(select(Incident).where(Incident.id == iid).with_for_update())
        if inc is None:
            raise HTTPException(404, "incident not found")
        if inc.status != "handed_off":
            raise HTTPException(409, "incident is not handed off")
        handoff = inc.handoff or {}
        if handoff.get("claimed_by"):
            if handoff["claimed_by"] != user:
                raise HTTPException(409, "handoff already claimed by another operator")
            return redact_obj(handoff)
        handoff = {**handoff, "claimed_by": user, "claimed_at": iso(datetime.now(timezone.utc))}
        inc.handoff = handoff
        db.audit(s, user, "handoff_claimed", iid)
    broker.publish(iid, "updated", "handed_off")
    return redact_obj(handoff)

# ------------------------------------------------------------------ actions
class RejectIn(BaseModel):
    reason: str | None = None


@app.post("/api/actions/{aid}/approve")
def approve(aid: int, user: str = Depends(require_user)):
    with db.session() as s:
        act = s.get(Action, aid)
        if not act:
            raise HTTPException(404, "action not found")
        if act.status != "pending_approval":
            raise HTTPException(409, f"action is {act.status}, not pending_approval")
        inc = s.get(Incident, act.incident_id)
        iid, level = inc.id, inc.severity_level
        if level == "HIGH_RISK" and not safety.high_risk_may_execute():
            act.status, act.reason = "denied", "HIGH_RISK incidents never execute actions"
            db.audit(s, user, "action_denied", iid, action_id=aid, reason=act.reason)
            denied_reason = act.reason
        else:
            denied_reason = None
            if STATE.get("engine") is None:
                raise HTTPException(503, "engine not ready")
            db.audit(s, user, "action_approved", iid, action_id=aid)
    if denied_reason:
        broker.publish(iid, "action")
        raise HTTPException(403, denied_reason)
    engine = STATE.get("engine")
    if engine is None:
        raise HTTPException(503, "engine not ready")
    res = engine.run_action(iid, aid, actor=user, level=level)  # re-runs all checks server-side
    with db.session() as s:
        act = s.get(Action, aid)
        out = action_dict(act)
    if res.status == "denied":
        raise HTTPException(403, res.reason or "denied by policy")
    return redact_obj(out)


@app.post("/api/actions/{aid}/reject")
def reject(aid: int, body: RejectIn | None = Body(default=None), user: str = Depends(require_user)):
    reason = (body.reason if body else None) or f"rejected by {user}"
    with db.session() as s:
        act = s.get(Action, aid)
        if not act:
            raise HTTPException(404, "action not found")
        if act.status != "pending_approval":
            raise HTTPException(409, f"action is {act.status}, not pending_approval")
        act.status, act.reason = "rejected", reason[:500]
        inc = s.get(Incident, act.incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        transition(inc, "handed_off")
        inc.status_reason = f"action rejected by {user}"
        update_run(s, inc.id, "succeeded", "handed_off", inc.status_reason)
        db.audit(s, user, "action_rejected", inc.id, action_id=aid, reason=reason[:500])
        s.flush()
        out, iid = action_dict(act), inc.id
    broker.publish(iid, "handed_off", "handed_off")
    return redact_obj(out)


# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    automation_mode: Literal["auto", "approval", "suggest_only"]


def _mode(s) -> str:
    row = s.get(Setting, "policy_mode")
    v = (row.value or {}).get("mode") if row else None
    return v if v in POLICY_MODES else "auto"


@app.get("/api/settings")
def get_settings_api(user: str = Depends(require_user)):
    with db.session() as s:
        return {"automation_mode": _mode(s)}


@app.put("/api/settings")
def put_settings_api(body: SettingsIn, user: str = Depends(require_user)):
    with db.session() as s:
        row = s.get(Setting, "policy_mode")
        if row:
            row.value = {"mode": body.automation_mode}
        else:
            s.add(Setting(key="policy_mode", value={"mode": body.automation_mode}))
        db.audit(s, user, "automation_mode_changed", mode=body.automation_mode)
    return {"automation_mode": body.automation_mode}


# Role placement is stored separately from policy mode and ConfigMap safety configuration.
@app.get("/api/model-settings")
def get_model_settings(user: str = Depends(require_user)):
    with db.session() as s:
        return read_settings(s, get_app_config())


@app.put("/api/model-settings")
def put_model_settings(body: ModelSettingsIn, user: str = Depends(require_user)):
    try:
        with db.session() as s:
            return save_settings(s, get_app_config(), body, user)
    except RevisionConflict as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


# Operational extra (not used by the dashboard): runtime status of watchers/RAG/LLM mapping.
@app.get("/api/status")
def status(user: str = Depends(require_user)):
    cfg = get_app_config()
    w = STATE.get("watchers")
    return {"ready": STATE["ready"], "rag_chunks": STATE["rag_chunks"], "watchers": w.status if w else {},
            "llm": {"provider": cfg.llm.provider,
                    "roles": {k: v.model_dump() for k, v in cfg.llm.roles.items()}},
            "target_namespace": cfg.target_namespace, "allowlist": cfg.allowlist}


# ------------------------------------------------------------------ webhook
ALERT_KIND = {
    "TargetWorkloadImagePullFailing": "pod",
    "TargetWorkloadCrashLooping": "pod",
    "TargetWorkloadOOMKilled": "pod",
    "TargetServiceNoEndpoints": "endpoint",
    "OrderHigh5xxRate": "app",
    "InventoryHigh5xxRate": "app",
    "TargetHighLatency": "app",
    "TargetConnectionPoolExhausted": "app",
}


def alert_signal(alert: dict, cfg, kube) -> Signal | None:
    labels = alert.get("labels") or {}
    name, ns = labels.get("alertname"), labels.get("namespace")
    if ns != cfg.target_namespace or name not in ALERT_KIND:
        return None
    kind = ALERT_KIND[name]
    if kind == "endpoint" and labels.get("endpoint"):
        return Signal(ns, "Service", labels["endpoint"], name, "alertmanager", {"labels": labels})
    if kind == "app" and labels.get("app"):
        return Signal(ns, "App", labels["app"], name, "alertmanager", {"labels": labels})
    if kind == "pod" and labels.get("pod") and kube is not None:
        from .k8s import deployment_for_pod
        pod = kube.get_pod(ns, labels["pod"])
        if not pod or (pod.get("metadata") or {}).get("deletionTimestamp"):
            return None
        dep = deployment_for_pod(kube, ns, pod)
        if dep:
            return Signal(ns, "Deployment", dep, name, "alertmanager", {"labels": labels})
    return None


@app.post("/api/hooks/alertmanager", dependencies=[Depends(require_webhook)])
async def alertmanager_hook(request: Request):
    payload = await request.json()
    cfg = get_app_config()
    engine = STATE.get("engine")
    accepted = 0
    for alert in payload.get("alerts") or []:
        if alert.get("status") != "firing":
            continue
        sig = await asyncio.to_thread(alert_signal, alert, cfg, STATE.get("kube"))
        if sig and engine:
            await asyncio.to_thread(engine.on_signal, sig)
            accepted += 1
    return {"accepted": accepted}


# ------------------------------------------------------------------ SSE
def _current_status(iid: int) -> str | None:
    with db.session() as s:
        inc = s.get(Incident, iid)
        return inc.status if inc else None


@app.get("/api/stream")
async def stream(request: Request, user: str = Depends(require_user)):
    raw = request.headers.get("last-event-id") or request.query_params.get("since")
    if raw is not None and (not raw.isascii() or not raw.isdecimal() or len(raw) > 19):
        raise HTTPException(400, "invalid event cursor")
    with db.session() as s:
        cursor = int(raw) if raw is not None else (s.scalar(select(func.max(IncidentEvent.id))) or 0)
        latest = s.scalar(select(func.max(IncidentEvent.id))) or 0
        oldest = s.scalar(select(func.min(IncidentEvent.id))) or 0
    if cursor > latest:
        raise HTTPException(409, "event cursor ahead of server; refetch snapshot")
    if raw is not None and oldest and cursor < oldest - 1:
        raise HTTPException(409, "event history expired; refetch snapshot")

    async def gen():
        nonlocal cursor
        last_ping = asyncio.get_running_loop().time()
        while not await request.is_disconnected():
            def fetch():
                with db.session() as s:
                    return [(r.id, r.incident_id, r.status, r.event) for r in s.scalars(
                        select(IncidentEvent).where(IncidentEvent.id > cursor).order_by(IncidentEvent.id).limit(200))]
            rows = await asyncio.to_thread(fetch)
            for rid, iid, status, event in rows:
                cursor = rid
                yield ServerSentEvent(id=str(rid), event="incident", data=broker.encode(
                    {"incident_id": iid, "status": status, "event": event}))
            now = asyncio.get_running_loop().time()
            if now - last_ping >= 15:
                yield ServerSentEvent(comment="ping")
                last_ping = now
            await asyncio.sleep(0.5 if rows else 1)

    return EventSourceResponse(gen(), ping=3600)
