"""Incident engine: signal intake, settle window, evidence, routing, workflows, safety, persistence."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from . import db
from .config import AppConfig, Settings
from .db import audit
from .evidence import Bundle, collect, compact_summary
from .incidents import Signal, ingest
from .llm import AGENT_SCHEMA, ROUTER_SCHEMA, AgentOutput, LLMError, RouterOutput, estimate_cost
from .models import Action, AgentRun, Citation, Evidence, Incident, RoutingDecision, WorkflowRun
from .lifecycle import transition, update_run
from .rag import RAG
from .agent_groups import (GROUP_SCHEMA, EvidenceStore, GroupController, GroupMessage, StopGroup, check_citations,
                           run_group)
from .config import RoleModel
from .redact import redact, redact_obj
from . import router as R
from . import safety
from .sse import broker
from . import runlog
from .evalguard import evaluation_lock_active
from .proposals import bounds_text
from .workflows import AgentCall, build_high_risk_graph, build_simple_graph, build_single_graph

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: AppConfig, settings: Settings, kube: Any, provider: Any, rag: RAG | None):
        self.cfg, self.settings, self.kube, self.provider, self.rag = cfg, settings, kube, provider, rag
        self.q: "queue.Queue[int]" = queue.Queue()
        self._scheduled: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Evaluation harness only (app/evalrun.py): restrict RAG kinds, capture the evidence store of the last run.
        self.exec_kube = None    # SafeActionKube (separate write identity) or None => typed actions are denied
        self.rag_kinds: list[str] | None = None
        self.capture: dict | None = None
        self.model_snapshot: dict | None = None
        self.interpret_worker = None   # P15 display-only Korean readings (InterpretWorker); None in evaluation

    # ------------------------------------------------------------------ intake
    def on_signal(self, sig: Signal) -> None:
        if evaluation_lock_active():     # evaluation campaign owns the fault namespace; signals are its own
            return
        with db.session() as s:
            inc, outcome = ingest(s, self.cfg, sig)
            iid = inc.id if inc else None
        if outcome == "created" and iid:
            broker.publish(iid, "created", "open")
            self.schedule(iid, delay=self.cfg.settle_seconds)
        elif outcome == "merged" and iid:
            broker.publish(iid, "updated")

    def schedule(self, incident_id: int, delay: float = 0) -> None:
        with self._lock:
            if incident_id in self._scheduled:
                return
            self._scheduled.add(incident_id)
        if delay > 0:
            t = threading.Timer(delay, self.q.put, args=(incident_id,))
            t.daemon = True
            t.start()
        else:
            self.q.put(incident_id)

    def start(self) -> None:
        threading.Thread(target=self._worker, name="incident-worker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def recover_on_startup(self) -> None:
        """Explicit handling of incidents interrupted by a restart (never silent, never replay actions).

        Every recovery transition is also published as a durable SSE event (after the recovery
        commit) so connected dashboards learn of it and cursor replay includes it.
        """
        failed_ids: list[int] = []
        action_ids: list[int] = []
        run_ids: list[int] = []
        with db.session() as s:
            for inc in s.scalars(select(Incident).where(Incident.status.in_(("analyzing", "acting")))):
                prev = inc.status
                transition(inc, "failed")
                inc.status_reason = f"LiveProbe restarted while incident was '{prev}'; marked failed (no replay)"
                update_run(s, inc.id, "failed", prev, inc.status_reason)
                audit(s, "liveprobe", "incident_interrupted", inc.id, previous_status=prev)
                failed_ids.append(inc.id)
            for act in s.scalars(select(Action).where(Action.status.in_(("executing", "approved")))):
                act.status = "failed"
                act.reason = "LiveProbe restarted during action; outcome indeterminate, inspect target manually"
                audit(s, "liveprobe", "action_interrupted", act.incident_id, action_id=act.id)
                action_ids.append(act.incident_id)
            for run in s.scalars(select(WorkflowRun).where(WorkflowRun.status == "running")):
                run.status, run.reason = "failed", "LiveProbe restarted; work not replayed"
                run_ids.append(run.incident_id)
            open_ids = list(s.scalars(select(Incident.id).where(Incident.status == "open")))
        for iid in dict.fromkeys(failed_ids):
            broker.publish(iid, "failed", "failed")
        # Interrupted actions/runs whose incident was not itself failed above (e.g. action_pending).
        for iid in dict.fromkeys(action_ids):
            if iid not in failed_ids:
                broker.publish(iid, "action")
        for iid in dict.fromkeys(run_ids):
            if iid not in failed_ids and iid not in action_ids:
                broker.publish(iid, "updated")
        for iid in open_ids:
            self.schedule(iid, delay=self.cfg.settle_seconds)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                iid = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(iid)
            except runlog.RunStopped as stop:
                self._set_status(iid, "handed_off", status_reason=f"운영자 중단 ({stop})",
                                 handoff={"summary": "운영자가 실행을 중단했습니다.", "root_cause_candidates": [],
                                          "recommended_steps": ["분석 기록을 검토한 뒤 필요하면 수동으로 처리하세요."],
                                          "notes": ["operator stopped the run"]})
                runlog.emit(iid, "completed", "운영자 중단으로 실행을 종료했습니다.", {"outcome": "stopped"})
            except Exception as e:  # noqa: BLE001
                log.exception("incident %s processing crashed", iid)
                with db.session() as s:
                    inc = s.get(Incident, iid)
                    if inc and inc.status not in ("resolved", "handed_off", "failed"):
                        transition(inc, "failed")
                        inc.status_reason = redact(f"internal error: {type(e).__name__}: {e}")[:500]
                        update_run(s, iid, "failed", "error", inc.status_reason)
                        audit(s, "liveprobe", "incident_failed", iid, error=inc.status_reason)
                broker.publish(iid, "failed", "failed")
            finally:
                with self._lock:
                    self._scheduled.discard(iid)

    # ------------------------------------------------------------ processing
    def _set_status(self, iid: int, status: str, **fields) -> None:
        with db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None:
                raise ValueError("incident not found")
            transition(inc, status)
            for k, v in fields.items():
                setattr(inc, k, v)
            update_run(s, iid, "failed" if status == "failed" else "succeeded" if status in ("resolved", "handed_off") else "running", status, fields.get("status_reason"))
            audit(s, "liveprobe", "incident_status", iid, status=status, reason=fields.get("status_reason"))
        broker.publish(iid, status if status in ("resolved", "handed_off", "failed") else "updated", status)
        if status in ("resolved", "handed_off", "failed"):
            runlog.emit(iid, "completed", {"resolved": "해결됨", "handed_off": "운영자 인계", "failed": "실패"}[status] +
                        (f": {redact(str(fields.get('status_reason') or ''))[:300]}" if fields.get("status_reason") else ""),
                        {"outcome": status})
        elif status == "action_pending":
            runlog.emit(iid, "status", "운영자 승인을 기다립니다.", {"status": status})

    def process(self, iid: int, *, arm: dict | None = None) -> None:
        # Never mutate the engine-wide config: active calls retain their original placement.
        from copy import copy
        from .model_settings import snapshot_config
        runner = copy(self)
        runner.model_snapshot = None
        if arm is None:
            with db.session() as s:
                runner.cfg, runner.model_snapshot = snapshot_config(s, self.cfg)
        runner._process(iid, arm=arm)

    def _process(self, iid: int, *, arm: dict | None = None) -> None:
        """Run one incident. `arm` is set only by the evaluation harness (liveprobe/app/evalrun.py):
        {"strategy": "single"|"fixed_group"|"adaptive", "role": RoleModel (single arm), "level": "COMPLEX" (fixed)}."""
        arm = arm or {}
        with db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None or inc.status != "open":
                return
            transition(inc, "analyzing")
            update_run(s, iid, "running", "analysis")
            ns, workload, created = inc.namespace, inc.workload, inc.created_at
            incident_view = {"id": inc.id, "namespace": ns, "workload": workload, "title": inc.title,
                             "signals": inc.signal_reasons, "created_at": inc.created_at.isoformat()}
            audit(s, "liveprobe", "analysis_started", iid)
        broker.publish(iid, "updated", "analyzing")

        # evidence -----------------------------------------------------------
        bundle = collect(self.kube, self.cfg, self.settings, ns, workload, since=created - timedelta(seconds=60))
        with db.session() as s:
            for it in bundle.items:
                s.add(Evidence(incident_id=iid, source=it.source, target=it.target, payload=it.payload, error=it.error,
                               collected_at=it.collected_at))
        broker.publish(iid, "updated", "analyzing")
        runlog.emit(iid, "evidence_collected", f"근거 {len(bundle.items)}건 수집", {
            "sources": sorted({it.source for it in bundle.items}),
            "errors": [{"source": it.source, "error": str(it.error)[:160]} for it in bundle.items if it.error][:5]})

        # routing ------------------------------------------------------------
        strategy = arm.get("strategy", "adaptive")
        # Collection/LLM calls take time: incorporate signals merged since analysis began.
        with db.session() as s:
            current = s.get(Incident, iid)
            incident_view["signals"] = list(current.signal_reasons) if current else incident_view["signals"]
        decision = R.decide(bundle, self.cfg.recent_rollout_minutes,
                            llm_classify=(lambda p: self._classify(iid, p)) if strategy == "adaptive" else None,
                            cfg=self.cfg, signal_reasons=incident_view["signals"])
        if strategy == "fixed_group" and decision.decided_by != "router_failure":
            # Fixed Multi-Agent arm: the same COMPLEX group for every incident (no routing).
            decision = R.Decision(arm["level"], R.WORKFLOW[arm["level"]], decision.features,
                                  "fixed multi-agent arm: group fixed regardless of incident", 1.0, "fixed",
                                  llm=None, policy=R.score_models(arm["level"], self.cfg))
        base_cfg = self.cfg
        self._record_decision(iid, decision)

        if decision.decided_by == "router_failure":
            self._set_status(iid, "handed_off", status_reason=decision.rationale,
                             handoff={"summary": decision.rationale, "root_cause_candidates": [],
                                      "recommended_steps": ["inspect evidence and router failure"],
                                      "notes": ["router failure; no automatic action"]})
            return
        from . import steering
        for _ in range(3):        # P15: an operator may move the analysis to another group mid-run (bounded)
            try:
                self._run_decision(iid, decision, bundle, incident_view, strategy, arm)
                return
            except steering.RerouteRequested as rr:
                self.cfg = base_cfg
                decision = R.Decision(rr.level, R.WORKFLOW[rr.level], decision.features,
                                      f"운영자 채팅으로 {rr.level} Group 재지정: {redact(rr.body)[:200]}", 1.0, "operator",
                                      llm=None, policy=R.score_models(rr.level, self.cfg))
                self._record_decision(iid, decision)
        self._set_status(iid, "handed_off", status_reason="운영자 Group 재지정이 반복되어 분석을 멈췄습니다.",
                         handoff={"summary": "Group 재지정이 3회를 넘었습니다.", "root_cause_candidates": [],
                                  "recommended_steps": ["분석 기록을 검토한 뒤 채팅으로 지시하세요."], "notes": ["reroute limit"]})

    def _record_decision(self, iid: int, decision: R.Decision) -> None:
        from .model_settings import apply_group
        self.cfg, decision.policy = apply_group(self.cfg, getattr(self, 'model_snapshot', None),
                                                decision.level, decision.policy)
        self._current_level = decision.level
        with db.session() as s:
            s.add(RoutingDecision(incident_id=iid, level=decision.level, workflow=decision.workflow,
                                  features=redact_obj(decision.features), rationale=redact(decision.rationale),
                                  policy=redact_obj(decision.policy),
                                  confidence=decision.confidence, decided_by=decision.decided_by))
            inc = s.get(Incident, iid)
            inc.severity_level = decision.level
            audit(s, "router", "routed", iid, level=decision.level, workflow=decision.workflow,
                  decided_by=decision.decided_by)
        broker.publish(iid, "updated", "analyzing")
        runlog.emit(iid, "routing_decided", f"사건 수준 {decision.level} → {decision.workflow}",
                    {"level": decision.level, "workflow": decision.workflow, "decided_by": decision.decided_by,
                     "rationale": redact(decision.rationale)[:400], "confidence": decision.confidence})

    def _run_decision(self, iid: int, decision: R.Decision, bundle: Bundle, incident_view: dict, strategy: str,
                      arm: dict) -> None:
        workload = incident_view.get("workload")
        if strategy != "single" and decision.level in ("NORMAL", "COMPLEX"):
            self._run_group(iid, decision, bundle, incident_view)
            return

        # workflow -----------------------------------------------------------
        query = self._rag_query(bundle, decision)
        hits = self._rag(query)
        state = {
            "incident": incident_view, "features": decision.features, "evidence": compact_summary(bundle),
            "rag": hits, "has_previous_revision": decision.features.get("has_previous_revision", False),
            "min_confidence": self.cfg.min_confidence, "allowlist": list(self.cfg.allowlist),
            "bounds": bounds_text(self.cfg),
        }
        if strategy == "single":
            # Single arm (evaluation only): ONE model/effort performs the whole analysis, verification and safety review.
            rm = arm["role"]
            run_agent = lambda role, prompt: self._call(iid, "signal_analyst", prompt, AGENT_SCHEMA, AgentOutput, hits,  # noqa: E731
                                                        model_override=rm.model, effort_override=rm.effort, role_model=rm)
        else:
            run_agent = self._agent_runner(iid, hits, selected_model=(decision.policy or {})["selected_model"])
        if strategy == "single":
            out = build_single_graph(run_agent).invoke(state)
        elif decision.level == "SIMPLE":
            out = build_simple_graph(run_agent).invoke(state)
        else:
            out = build_high_risk_graph(run_agent).invoke(state)

        final = out.get("verifier") or out.get("safety") or out.get("analyst") or out.get("curator") or {}
        summary = (final or {}).get("summary")

        # KAN-53: every cited evidence/runbook ref must resolve to a source that was actually shown.
        # (SIMPLE/HIGH_RISK keep the MVP-0 message without verbatim quotes, so only ref resolution applies.)
        store = EvidenceStore(state["evidence"], hits)
        if self.capture is not None:
            self.capture["store"] = store
        cite = {role: check_citations(store, out.get(role) or {}).as_dict()
                for role in ("analyst", "verifier", "curator", "safety") if out.get(role)}
        unknown = sorted({r for c in cite.values() for r in c["unknown_refs"]})
        audit_detail = {"level": decision.level, "roles_checked": sorted(cite), "unknown_refs": unknown}
        with db.session() as s:
            audit(s, "workflow", "wf_citation_check", iid, **audit_detail)
        if isinstance(out.get("handoff"), dict):
            out["handoff"]["citation_check"] = audit_detail
        if unknown and out.get("outcome") == "propose_action":
            out = {**out, "outcome": "handoff", "reason": f"cited evidence refs do not exist: {unknown[:5]}",
                   "handoff": {"summary": summary, "notes": ["citation check failed; automatic action withheld"],
                               "citation_check": audit_detail}}
        if decision.level == "HIGH_RISK":
            from . import policy as _pol
            _pol.record(iid, "high_risk_level", profile=safety.active_profile(), level="HIGH_RISK", workload=workload,
                        proposed={"outcome": out.get("outcome")}, rationale=str(out.get("reason") or decision.rationale or ""),
                        impact="traffic-routing / cross-service blast radius",
                        outcome="held_for_operator" if not safety.high_risk_may_execute() else "proposed_for_approval")
        if decision.level == "HIGH_RISK" and not safety.high_risk_may_execute():
            # Assessment profile: record the suggestion (if any) as a denied action for auditability; never execute.
            with db.session() as s:
                inc = s.get(Incident, iid)
                inc.summary = summary
            self._set_status(iid, "handed_off", status_reason=out.get("reason") or "HIGH_RISK policy: handoff",
                             handoff=redact_obj(out.get("handoff")))
            return

        if out.get("outcome") != "propose_action":
            with db.session() as s:
                s.get(Incident, iid).summary = summary
            self._set_status(iid, "handed_off", status_reason=out.get("reason"),
                             handoff=redact_obj(out.get("handoff") or {"summary": summary, "notes": [out.get("reason")]}))
            return

        # safety -------------------------------------------------------------
        if self._propose_and_run(iid, decision.level, out["action"], summary):
            return
        self._set_status(iid, "handed_off", status_reason="action was not executable; operator handoff",
                         handoff={"summary": summary, "notes": ["action was not executable; see audit"]})

    def _kube_for(self, aid: int):
        """rollout_undo goes through the rollback controller (observe client + signed grant); only the five typed
        actions use the separate write identity. Mixing them up fails (the executor client has no request_rollback)."""
        from . import actions as typed
        with db.session() as s:
            a = s.get(Action, aid)
            is_typed = bool(a) and a.type in typed.TYPED
        return (self.exec_kube or self.kube) if is_typed else self.kube

    def attach_executor(self, token_file: str) -> bool:
        """Wire the dedicated write identity. Without it typed actions are denied (fail closed). Shared by the API
        startup and the evaluation runner so both exercise the same execution path."""
        try:
            self.exec_kube = self.kube.executor(token_file)
        except Exception as e:  # noqa: BLE001 - never block startup
            log.warning("safe-action executor unavailable: %s", type(e).__name__)
            self.exec_kube = None
        return self.exec_kube is not None

    def _network_handoff(self, iid: int, aid: int | None = None) -> bool:
        """Recheck late network risk before proposal or execution; never replay an action."""
        reason = 'NetworkDrop: 트래픽 경로 이상 신호가 추가되어 자동 조치를 중단하고 운영자에게 인계합니다.'
        with db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None or not safety.has_network_risk(inc) or inc.status not in ('open', 'analyzing', 'action_pending', 'acting'):
                return False
            if inc.severity_level != 'HIGH_RISK':
                inc.severity_level = 'HIGH_RISK'
                s.add(RoutingDecision(incident_id=iid, level='HIGH_RISK', workflow=R.WORKFLOW['HIGH_RISK'],
                                      features={'network_drop_signal': True, 'signal_reasons': inc.signal_reasons},
                                      rationale=reason, policy={**R.score_models('HIGH_RISK', self.cfg),
                                                                'late_network_drop': True},
                                      confidence=1.0, decided_by='rules'))
            if aid is not None:
                action = s.get(Action, aid)
                if action and action.status in ('proposed', 'pending_approval', 'approved'):
                    action.status, action.reason = 'denied', reason
                    audit(s, 'liveprobe', 'action_denied', iid, action_id=aid, reason=reason)
            audit(s, 'liveprobe', 'network_risk_rechecked', iid, action_id=aid, reason=reason)
        self._set_status(iid, 'handed_off', status_reason=reason,
                         handoff={'summary': reason, 'root_cause_candidates': [],
                                  'recommended_steps': ['네트워크 차단 경로와 정책을 확인하세요.'],
                                  'notes': ['late NetworkDrop; no automatic action']})
        return True

    def _propose_and_run(self, iid: int, level: str, action: dict, summary: str | None) -> bool:
        """One agent-proposed Kubernetes action through the validated executor. True when the incident was finished
        or moved to approval; False when the proposal was denied (caller hands off)."""
        from . import actions as typed
        if self._network_handoff(iid):
            return True
        with db.session() as s:
            inc = s.get(Incident, iid)
            inc.summary = summary
            if action["type"] == "rollout_undo":
                act = safety.propose(s, self.cfg, inc, level, "rollout_undo", "Deployment", action["name"])
            else:
                act = typed.propose_typed(s, self.cfg, self.exec_kube or self.kube, inc, level, action["type"], action["kind"],
                                          action["name"], action["params"])
            aid, astatus, reason = act.id, act.status, act.reason
        broker.publish(iid, "action")
        runlog.emit(iid, "action_proposed", f"조치 제안: {action['type']} → {action['kind']}/{action['name']}",
                    {"action_id": aid, "type": action["type"], "params": action["params"], "status": astatus,
                     "why": action.get("why")})
        if astatus == "pending_approval":
            self._set_status(iid, "action_pending", status_reason="waiting for operator approval")
            return True
        if astatus != "proposed":
            runlog.emit(iid, "note", f"조치가 거부되었습니다: {reason}", {"action_id": aid})
            return False
        self.run_action(iid, aid, actor="liveprobe", level=level)
        return True

    def run_action(self, iid: int, aid: int, actor: str, level: str | None = None) -> safety.ExecResult:
        runlog.gate(iid, "조치 실행")        # last chance to pause/stop; once executing, the action runs to verification
        if self._network_handoff(iid, aid):
            return safety.ExecResult(aid, 'denied', 'NetworkDrop: operator handoff')
        runlog.emit(iid, "action_proposed", f"조치 #{aid} 실행 시작", {"action_id": aid, "actor": actor, "level": level})
        self._set_status(iid, "acting")
        res = safety.execute(db.session, self.cfg, self._kube_for(aid), aid, actor, level=level)
        runlog.emit(iid, "action_executed", f"조치 #{aid} 결과: {res.status}", {"action_id": aid, "status": res.status,
                                                                          "reason": redact(str(res.reason or ""))[:300]})
        runlog.emit(iid, "verification_result", "롤아웃 검증 " + ("통과" if res.status == "succeeded" else "미통과"),
                    {"action_id": aid, "ok": res.status == "succeeded"})
        if res.status == "succeeded":
            with db.session() as s:
                atype = (s.get(Action, aid).type if s.get(Action, aid) else None) or "action"
            self._set_status(iid, "resolved", status_reason=f"{atype} succeeded and verified")
        else:
            self._set_status(iid, "handed_off", status_reason=f"action {res.status}: {res.reason}",
                             handoff={"notes": [f"automatic action {res.status}: {res.reason}"]})
        return res

    # ------------------------------------------------------ NORMAL / COMPLEX (KAN-53)
    def _run_group(self, iid: int, decision: R.Decision, bundle: Bundle, incident_view: dict) -> None:
        """Run the NORMAL/COMPLEX Agents Group. Always ends in an operator handoff (no automatic action)."""
        hits = self._rag(self._rag_query(bundle, decision))
        store = EvidenceStore(compact_summary(bundle), hits)
        if self.capture is not None:
            self.capture["store"] = store

        def call(role: str, prompt: str, rm: RoleModel) -> AgentCall:
            return self._call(iid, role, prompt, GROUP_SCHEMA, GroupMessage, hits, model_override=rm.model,
                              effort_override=rm.effort, role_model=rm)

        def on_event(kind: str, ev: dict) -> None:
            with db.session() as s:
                audit(s, "workflow", f"wf_{kind}", iid, **redact_obj(ev))
            broker.publish(iid, "updated", "analyzing")
            ek = {"group_started": "group_composed", "escalated": "escalated"}.get(kind)
            if ek:
                runlog.emit(iid, ek, f"{kind}: " + ", ".join(f"{k}={v}" for k, v in list(ev.items())[:4])[:300],
                            {k: v for k, v in ev.items() if isinstance(v, (str, int, float, bool, list))})

        ctl = GroupController(decision.level, self.cfg, call, on_event,
                              primary_model=(decision.policy or {}).get("selected_model"))
        try:
            res = run_group(decision.level, ctl, store, incident_view, decision.features)
        except StopGroup as e:  # configuration problems before the first node
            res = {"status": "stopped", "reason": f"{e.kind}: {e.detail}", "root_cause_candidates": [],
                   "recommended_steps": [], "notes": [f"{decision.level}: workflow could not start"],
                   "budget": ctl.budget()}
        handoff = {"summary": res.get("summary"), "root_cause_candidates": res.get("root_cause_candidates", [])[:3],
                   "recommended_steps": res.get("recommended_steps", []), "notes": res.get("notes", []),
                   "group": res.get("group"), "workflow_status": res.get("status"),
                   "counter_evidence": res.get("counter_evidence", []), "verification": res.get("verification"),
                   "budget": res.get("budget"), "confidence": res.get("confidence")}
        with db.session() as s:
            s.get(Incident, iid).summary = res.get("summary")
        proposal = res.get("action") or {}
        if proposal.get("propose") and incident_view.get("workload"):
            if self._propose_and_run(iid, decision.level, proposal, res.get("summary")):
                return
            handoff["notes"] = [*handoff.get("notes", []), "proposed action was not executable"]
        self._set_status(iid, "handed_off",
                         status_reason=f"{decision.level} group {res.get('status')}: {res.get('reason')}"[:500],
                         handoff=redact_obj(handoff))

    # ----------------------------------------------------------------- LLM
    def _classify(self, iid: int, prompt: str) -> dict:
        r = self._call(iid, "router", prompt, ROUTER_SCHEMA, RouterOutput, hits=[])
        if not r.ok:
            raise RuntimeError(r.error or "router failed")
        return r.output or {}

    def _agent_runner(self, iid: int, hits: list[dict], selected_model: str):
        def run(role: str, prompt: str) -> AgentCall:
            # Route the primary analysis/curation role; independent verifiers keep their
            # separately configured safety models and effort settings.
            model = selected_model if role in ("signal_analyst", "evidence_curator") else None
            return self._call(iid, role, prompt, AGENT_SCHEMA, AgentOutput, hits, model_override=model)
        return run

    def _call(self, iid: int, role: str, prompt: str, schema: dict, model_cls, hits: list[dict],
              model_override: str | None = None, effort_override: str | None = None,
              role_model: RoleModel | None = None) -> AgentCall:
        rm = role_model or self.cfg.llm.roles.get(role)
        if rm is None:
            return AgentCall(False, None, None, f"no model mapping for role {role}", error_kind="config")
        model = model_override or rm.model
        effort = effort_override if role_model is not None else rm.effort
        # node boundary: honour pause/stop, then apply pending operator chat (P15 steering). The router never
        # consumes chat: a reroute request waits for the first agent after routing.
        runlog.gate(iid, f"에이전트 {role}")
        if role != "router":
            from . import steering
            st = steering.take(iid, role, getattr(self, "_current_level", None))
            if st.reroute is not None:
                raise st.reroute
            prompt = runlog.with_operator_instructions(prompt, st.texts)
            if st.escalate:
                up = steering.escalate_model(self.cfg, model, effort)
                if up is None:
                    runlog.emit(iid, "steering_interpreted", f"{role}은(는) 이미 가장 높은 모델·추론 강도라 올릴 수 없습니다.",
                                {"intent": "escalate", "refused": True, "role": role})
                else:
                    runlog.emit(iid, "escalated", f"운영자 요청으로 {role}을(를) {up[0]}·{up[1]}로 올립니다.",
                                {"role": role, "by": "operator", "from_model": model, "from_effort": effort,
                                 "to_model": up[0], "to_effort": up[1]})
                    model, effort = up
        runlog.emit(iid, "agent_started", f"{role} 시작", {"role": role, "model": model, "effort": effort})
        attempts, err, result = 0, None, None
        err_kind = None
        in_tok = out_tok = 0
        dur = 0
        for attempt in (1, 2):  # invalid output -> one retry
            attempts = attempt
            try:
                result = self._complete_waiting_for_quota(iid, role, prompt, schema, model, effort)
                in_tok += result.usage.input_tokens or 0
                out_tok += result.usage.output_tokens or 0
                dur += result.duration_ms
                model_cls.model_validate(result.data)
                err = err_kind = None
                break
            except LLMError as e:
                in_tok += e.usage.input_tokens or 0
                out_tok += e.usage.output_tokens or 0
                dur += e.duration_ms or 0
                err, err_kind = f"{e.kind}: {e.message}", e.kind
                result = None
                if e.kind != "invalid_output":
                    break
            except ValidationError as e:
                err, err_kind = f"invalid_output: schema validation failed ({e.error_count()} errors)", "invalid_output"
                result = None
        from .llm.base import Usage
        cost = estimate_cost(self.cfg, model, Usage(input_tokens=in_tok, output_tokens=out_tok))
        with db.session() as s:
            run = AgentRun(incident_id=iid, role=role, provider=self.provider.name, model=model,
                           reasoning_effort=effort, input_tokens=in_tok or None, output_tokens=out_tok or None,
                           estimated_cost_usd=cost, duration_ms=dur, status="succeeded" if err is None else "failed",
                           attempts=attempts, output=redact_obj(result.data) if result else None,
                           error=redact(err)[:800] if err else None)
            s.add(run)
            s.flush()
            for h in hits:
                s.add(Citation(agent_run_id=run.id, chunk_id=h["chunk_id"], score=h["score"]))
            audit(s, role, "agent_run", iid, agent_run_id=run.id, status=run.status, model=model)
            rid = run.id
        broker.publish(iid, "agent_run")
        _out = result.data if result else {}
        runlog.emit(iid, "agent_finished",
                    (str(_out.get("summary") or "")[:400] if err is None else f"{role} 실패: {err}"[:400]) or f"{role} 완료",
                    {"role": role, "model": model, "effort": effort, "status": "succeeded" if err is None else "failed",
                     "agent_run_id": rid, "input_tokens": in_tok, "output_tokens": out_tok, "duration_ms": dur,
                     "confidence": _out.get("confidence"),
                     "root_cause_candidates": [{"cause": c.get("cause"), "confidence": c.get("confidence"),
                                                "evidence_refs": c.get("evidence_refs")}
                                               for c in (_out.get("root_cause_candidates") or [])[:3]]})
        usage = dict(cost_usd=cost, model=model, effort=effort, input_tokens=in_tok, output_tokens=out_tok)
        if self.interpret_worker is not None:
            try:
                self.interpret_worker.request(iid)
            except Exception:  # noqa: BLE001 - display helper never affects the analysis
                pass
        if err is None:
            return AgentCall(True, result.data, rid, **usage)
        return AgentCall(False, None, rid, err, error_kind=err_kind, **usage)

    def _complete_waiting_for_quota(self, iid: int, role: str, prompt: str, schema: dict, model: str, effort: str | None):
        """Call the provider; while every Codex account is at its usage limit, save state, wait and retry.

        Never switches to another provider (no API key, no Claude). The wait honours pause/stop, so the operator can
        still hand the incident off. After `quota_wait_max_s` the original quota error is raised and the incident is
        handed off with the analysis so far."""
        waited, announced = 0.0, False
        while True:
            try:
                res = self.provider.complete(role, prompt, schema, model, effort, self.cfg.llm.timeout_s)
                if announced:
                    runlog.emit(iid, "resumed", "Codex 사용 한도가 풀려 분석을 이어갑니다.", {"after_s": round(waited)})
                self._relay_pool_events(iid)
                return res
            except LLMError as e:
                self._relay_pool_events(iid)
                if e.kind != "quota":
                    raise
                limit = self.settings.quota_wait_max_s
                if waited >= limit:
                    raise
                step = float(max(1, min(self.settings.quota_recheck_s, getattr(self.provider, "next_available_in", lambda: 60)() or 60)))
                if not announced:
                    runlog.emit(iid, "waiting_quota", "두 Codex 계정 모두 사용 한도입니다. 상태를 저장하고 대기합니다(API·Claude로 전환하지 않음).",
                                {"retry_in_s": round(step), "max_wait_s": limit,
                                 "pool": getattr(self.provider, "status", lambda: {})()})
                    announced = True
                runlog.gate(iid, f"에이전트 {role} 재시도")   # pause/stop still work while waiting
                self._sleep(step)
                waited += step

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop.is_set():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def _relay_pool_events(self, iid: int) -> None:
        """Turn account switches reported by the pool into feed events (once each)."""
        pool = self.provider
        if not hasattr(pool, "drain_events"):
            return
        for kind, detail in pool.drain_events():
            if kind == "account_switched":
                runlog.emit(iid, "account_switched", f"Codex 계정을 {detail.get('from')} → {detail.get('to')}로 전환했습니다.", detail)
            elif kind == "account_limited":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 사용 한도 도달 (약 {detail.get('blocked_for_s')}초)", detail)
            elif kind == "account_auth_failed":
                runlog.emit(iid, "note", f"Codex 계정 {detail.get('account')} 인증 실패 — 재로그인이 필요합니다.", detail)

    # ----------------------------------------------------------------- RAG
    def _rag_query(self, b: Bundle, d: R.Decision) -> str:
        f = d.features
        parts = [f"{b.workload} incident"]
        if f.get("waiting_reasons"):
            parts.append("pods " + " ".join(f["waiting_reasons"]) + " image pull failure after rollout")
        if f.get("service_selector_matches_zero_pods") or f.get("service_has_no_endpoints"):
            parts.append("service has no endpoints selector mismatch")
        if f.get("dependent_callers_failing"):
            parts.append("callers return 5xx " + " ".join(f["dependent_callers_failing"]))
        if f.get("oom_killed"):
            parts.append("container OOMKilled exit code 137 memory limit")
        if f.get("latency_signal"):
            parts.append("high latency connection pool exhaustion slow responses")
        return "; ".join(parts)

    def _rag(self, query: str) -> list[dict]:
        if self.rag is None:
            return []
        try:
            with db.session() as s:
                kinds = self.rag_kinds or (None if self.cfg.use_reference_guide else
                                           ["runbook", "public_doc", "past_incident"])
                hits = self.rag.search(s, query, k=3, **({"kinds": kinds} if kinds else {}))
        except Exception as e:  # noqa: BLE001
            log.warning("RAG search failed: %s", type(e).__name__)
            return []
        return [{"ref": f"R{i + 1}", "chunk_id": h.chunk_id, "doc": h.doc, "source": h.source,
                 "chunk_ix": h.chunk_ix, "score": h.score, "hybrid_score": h.hybrid_score,
                 "source_uri": h.source_uri, "locator": h.locator, "section": h.section,
                 "license": h.license, "attribution": h.attribution,
                 "content": h.content} for i, h in enumerate(hits)]
