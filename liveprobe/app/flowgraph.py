"""P15: the incident's agent flow as nodes and edges for the war-room graph.

Nodes come only from persisted facts (routing decisions, agent runs, run events, actions, operator messages and
commands); not-yet-run roles of the active group are shown as `planned`. Layout hints: `depth` is the column
(left -> right), `lane` the row inside a column. No hidden reasoning is shown, only recorded outputs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select

from .models import (Action, AgentRun, Evidence, Incident, Interpretation, OperatorCommand, OperatorMessage, RoutingDecision,
                     RunEvent)
from .roles_ko import GROUP_KO, role_info

STAGES: dict[str, list[list[str]]] = {
    "SIMPLE": [["signal_analyst"], ["result_verifier"]],
    "NORMAL": [["k8s_analyst", "log_summarizer"], ["coordinator"], ["group_verifier"]],
    "COMPLEX": [["k8s_log_analyst"], ["cause_reasoner"], ["challenger", "recovery_planner"], ["group_verifier"]],
    "HIGH_RISK": [["evidence_curator"], ["safety_verifier"]],
}
VERIFY_ROLES = {"result_verifier", "group_verifier", "safety_verifier"}
ASSIST_ROLES = {"router", "interpreter", "steering", "operator_planner"}
FULL_REF = {"E1": "E1_deployment", "E2": "E2_pods", "E3": "E3_service", "E4": "E4_callers", "E5": "E5_recent_events",
            "E6": "E6_metrics", "E7": "E7_error_logs", "E8": "E8_node", "E9": "E9_config"}
ACTION_KO = {"rollout_undo": "이전 버전으로 롤백", "rollout_restart": "Deployment 재시작", "pod_delete": "Pod 재생성",
             "scale_replicas": "레플리카 수 조정", "set_memory_limit": "메모리 한도 조정", "configmap_restore": "ConfigMap 복원"}
SIGNAL_KO = {
    "TargetWorkloadOOMKilled": "메모리 초과 강제 종료(OOMKilled)", "OOMKilled": "메모리 초과 강제 종료(OOMKilled)",
    "TargetWorkloadImagePullFailing": "이미지 내려받기 실패", "ErrImagePull": "이미지 내려받기 실패",
    "ImagePullBackOff": "이미지 내려받기 실패", "TargetWorkloadCrashLooping": "반복 재시작(CrashLoop)",
    "CrashLoopBackOff": "반복 재시작(CrashLoop)", "NoEndpoints": "Service 연결 대상 없음",
    "TargetServiceNoEndpoints": "Service 연결 대상 없음", "TargetHighLatency": "응답 지연",
    "TargetConnectionPoolExhausted": "DB 연결 풀 고갈", "OrderHigh5xxRate": "5xx 오류 증가",
    "InventoryHigh5xxRate": "5xx 오류 증가", "NetworkDrop": "네트워크 차단",
}
COMMAND_STATUS = {"planning": "running", "awaiting_confirmation": "running", "awaiting_secret": "running",
                  "executing": "running", "succeeded": "done", "rolled_back": "done", "rolling_back": "running",
                  "failed": "failed", "rejected": "failed", "cancelled": "skipped"}


def signal_ko(reasons: list[str] | None) -> str:
    seen: list[str] = []
    for r in reasons or []:
        ko = SIGNAL_KO.get(str(r), str(r))
        if ko not in seen:
            seen.append(ko)
    return ", ".join(seen) or "이상 신호"


def refs_of(output: dict | None) -> list[str]:
    out: list[str] = []
    o = output or {}
    raw = [e.get("ref") for e in (o.get("evidence") or []) if isinstance(e, dict)]
    for c in (o.get("root_cause_candidates") or o.get("candidates") or []):
        raw += list(c.get("evidence_refs") or [])
    for r in raw:
        r = str(r or "").strip()
        base = r.split("_", 1)[0].split("#", 1)[0]
        full = FULL_REF.get(base) or (r if r.startswith("R") else None)
        if full and full not in out:
            out.append(full)
    return out


def _conclusion(o: dict | None) -> str | None:
    if not o:
        return None
    text = str(o.get("summary") or "")
    for c in (o.get("root_cause_candidates") or o.get("candidates") or [])[:3]:
        cause = c.get("cause") or c.get("statement")
        if cause:
            text += f"\n- {cause} (confidence {c.get('confidence')})"
    return text.strip() or None


class _G:
    def __init__(self):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.at_depth: dict[int, int] = defaultdict(int)

    def node(self, nid: str, type_: str, label: str, depth: int, *, lane: int | None = None, status: str = "done",
             role: str | None = None, group: str | None = None, **extra) -> dict:
        info = role_info(role) if role else {"role_ko": label, "job_ko": ""}
        if lane is None:
            lane = self.at_depth[depth]
        self.at_depth[depth] = max(self.at_depth[depth], lane + 1)
        n = {"id": nid, "type": type_, "label": label, "role": role, "role_ko": info["role_ko"], "job_ko": info["job_ko"],
             "status": status, "model": None, "effort": None, "tokens": None, "cost_usd": None, "duration_ms": None,
             "inputs": [], "conclusion_ko": None, "conclusion_raw": None, "lane": lane, "depth": depth, "group": group}
        n.update(extra)
        self.nodes.append(n)
        return n

    def edge(self, a: str, b: str, kind: str = "flow", label: str | None = None) -> None:
        eid = f"{a}->{b}:{kind}"
        if a != b and not any(e["id"] == eid for e in self.edges):
            self.edges.append({"id": eid, "source": a, "target": b, "kind": kind, "label": label})


def _run_node(g: _G, r: AgentRun, depth: int, lane: int | None, group: str | None, ko: dict[str, str],
              status: str | None = None) -> dict:
    o = r.output or {}
    st = status or ("failed" if r.status == "failed" else "done")
    return g.node(f"run:{r.id}", "verify" if r.role in VERIFY_ROLES else "agent", role_info(r.role)["role_ko"], depth,
                  lane=lane, status=st, role=r.role, group=group, model=r.model, effort=r.reasoning_effort,
                  tokens={"in": r.input_tokens, "out": r.output_tokens} if r.input_tokens is not None else None,
                  cost_usd=r.estimated_cost_usd, duration_ms=r.duration_ms, inputs=refs_of(o),
                  conclusion_raw=_conclusion(o) or (r.error if r.status == "failed" else None),
                  conclusion_ko=ko.get(f"agent:{r.id}"), agent_run_id=r.id)


def build_flow(s, iid: int) -> dict[str, Any]:
    inc = s.get(Incident, iid)
    if inc is None:
        return {"nodes": [], "edges": []}
    routing = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid).order_by(RoutingDecision.id)))
    runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
    events = list(s.scalars(select(RunEvent).where(RunEvent.incident_id == iid).order_by(RunEvent.seq)))
    actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
    commands = list(s.scalars(select(OperatorCommand).where(OperatorCommand.incident_id == iid).order_by(OperatorCommand.id)))
    messages = list(s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == iid).order_by(OperatorMessage.id)))
    n_evidence = s.scalar(select(Evidence.id).where(Evidence.incident_id == iid).limit(1)) is not None
    ko = {r.item_key: r.ko for r in s.scalars(select(Interpretation).where(Interpretation.incident_id == iid))
          if r.item_key.startswith("agent:")}
    finished = inc.status in ("resolved", "handed_off", "failed")
    g = _G()

    g.node("signal", "signal", f"감지 신호: {signal_ko(inc.signal_reasons)}", 0, status="done")
    ev_count = s.query(Evidence).filter_by(incident_id=iid).count()
    g.node("evidence", "evidence", f"근거 수집 {ev_count}건" if n_evidence else "근거 수집", 1,
           status="done" if n_evidence else ("failed" if finished else "running"))
    g.edge("signal", "evidence")
    for r in runs:
        if r.role == "interpreter":
            g.node(f"run:{r.id}", "interpret", "한글 해석(표시용)", 1, status="done" if r.status == "succeeded" else "failed",
                   role="interpreter", model=r.model, effort=r.reasoning_effort,
                   tokens={"in": r.input_tokens, "out": r.output_tokens} if r.input_tokens is not None else None,
                   cost_usd=r.estimated_cost_usd, duration_ms=r.duration_ms, agent_run_id=r.id)
            g.edge("evidence", f"run:{r.id}", "verify", "표시용")
            break
    router_run = next((r for r in runs if r.role == "router"), None)
    started = Counter(e.detail.get("role") for e in events if e.kind == "agent_started" and isinstance(e.detail, dict))
    finished_runs = Counter(r.role for r in runs)
    if router_run is not None:
        _run_node(g, router_run, 2, 0, None, ko)
        g.nodes[-1].update(id="router", type="router", label="사건 수준 판단")
    else:
        g.node("router", "router", "사건 수준 판단", 2, role="router",
               status="done" if routing else ("running" if started.get("router") else ("skipped" if finished else "planned")))
    g.edge("evidence", "router")

    escal_left = Counter(e.detail.get("role") for e in events if e.kind == "escalated" and isinstance(e.detail, dict))
    work = [r for r in runs if r.role not in ASSIST_ROLES]
    depth = 3
    prev_tail: list[str] = ["router"]
    last_role_node: dict[str, dict] = {}
    for si, rd in enumerate(routing):
        nxt = routing[si + 1].created_at if si + 1 < len(routing) else None
        seg = [r for r in work if (rd.created_at is None or r.created_at is None or r.created_at >= rd.created_at or si == 0)
               and (nxt is None or r.created_at is None or r.created_at < nxt)]
        if si > 0:
            seg = [r for r in seg if r.created_at is None or r.created_at >= rd.created_at]
        level = rd.level if rd.level in STAGES else "SIMPLE"
        gid = f"group:{rd.id}"
        g.node(gid, "group", f"{GROUP_KO.get(level, level)} 선택", depth, status="done" if (finished or si + 1 < len(routing)) else "running",
               group=level, decided_by=rd.decided_by, rationale=rd.rationale, confidence=rd.confidence)
        for t in prev_tail:
            g.edge(t, gid, "reroute" if si else "flow", ("운영자 재지정" if rd.decided_by == "operator" else "수준 재판단") if si else None)
        stages = STAGES[level]
        base = depth + 1
        pointer, shift = -1, 0
        stage_nodes: dict[int, list[dict]] = defaultdict(list)
        seg_role_node: dict[str, dict] = {}
        for r in seg:
            prev = seg_role_node.get(r.role)
            if prev is not None and escal_left.get(r.role, 0) > 0:
                escal_left[r.role] -= 1
                prev["status"] = "escalated"
                n = _run_node(g, r, prev["depth"], None, level, ko)
                g.edge(prev["id"], n["id"], "escalate", "상위 모델 승격")
                stage_nodes[prev["depth"]].append(n)
            else:
                sidx = next((i for i, roles in enumerate(stages) if r.role in roles and i > pointer), None)
                if sidx is None and prev is None:
                    sidx = next((i for i, roles in enumerate(stages) if r.role in roles), None)
                if sidx is None:      # a repeat outside its stage (e.g. COMPLEX revise) gets its own column
                    shift += 1
                    d = base + max(pointer, 0) + shift
                    lane = 0
                else:
                    pointer = max(pointer, sidx)
                    d = base + sidx + shift
                    lane = stages[sidx].index(r.role) if r.role in stages[sidx] else None
                    if lane is not None and any(x["lane"] == lane for x in stage_nodes[d]):
                        lane = None
                n = _run_node(g, r, d, lane, level, ko)
                stage_nodes[d].append(n)
            seg_role_node[r.role] = n
            last_role_node[r.role] = n
        live = si + 1 == len(routing) and not finished
        if live:
            for i, roles in enumerate(stages):
                for role in roles:
                    if role in seg_role_node:
                        continue
                    running = started.get(role, 0) > finished_runs.get(role, 0)
                    d = base + i + shift
                    n = g.node(f"plan:{rd.id}:{role}", "verify" if role in VERIFY_ROLES else "agent", role_info(role)["role_ko"], d,
                               lane=roles.index(role) if not any(x["lane"] == roles.index(role) for x in stage_nodes[d]) else None,
                               status="running" if running else "planned", role=role, group=level)
                    stage_nodes[d].append(n)
                    seg_role_node[role] = n
                    last_role_node.setdefault(role, n)
        cols = sorted(stage_nodes)
        tails = [gid]
        for d in cols:
            heads = [n for n in stage_nodes[d] if n["status"] != "escalated" or not any(
                e["source"] == n["id"] and e["kind"] == "escalate" for e in g.edges)]
            entry = [n for n in stage_nodes[d] if not any(e["target"] == n["id"] and e["kind"] == "escalate" for e in g.edges)]
            for t in tails:
                for n in entry:
                    g.edge(t, n["id"])
            tails = [n["id"] for n in heads] or tails
        prev_tail = tails
        depth = (max(cols) if cols else depth) + 1

    # operator steering ------------------------------------------------------------
    applied_to: dict[int, str | None] = {}
    for e in events:
        if e.kind == "operator_applied" and isinstance(e.detail, dict):
            for mid in e.detail.get("message_ids") or []:
                applied_to[mid] = e.detail.get("applied_to_role")
    for m in messages:
        target = None
        role = applied_to.get(m.id)
        if role and role in last_role_node:
            target = last_role_node[role]
        elif m.applied_at is not None:
            target = next((n for n in g.nodes if n.get("agent_run_id") and any(
                r.id == n["agent_run_id"] and r.created_at and r.created_at >= m.applied_at for r in runs)), None)
        if target is None and m.applied_at is None:
            target = next((n for n in g.nodes if n["status"] in ("running", "planned") and n["type"] in ("agent", "verify")), None)
        if target is None:
            continue
        sn = g.node(f"steer:{m.id}", "steer", f"운영자 지시: {m.body[:40]}", target["depth"], lane=-1 - sum(
            1 for x in g.nodes if x["type"] == "steer" and x["depth"] == target["depth"]),
            status="done" if m.applied_at else "planned", intent=m.intent, conclusion_raw=m.body)
        g.edge(sn["id"], target["id"], "steer", "반영" if m.applied_at else "반영 예정")

    # actions and outcome -------------------------------------------------------------
    for a in actions:
        st = {"succeeded": "done", "failed": "failed", "denied": "failed", "rejected": "failed",
              "pending_approval": "running", "executing": "running", "approved": "running"}.get(a.status, "planned")
        n = g.node(f"action:{a.id}", "action", f"조치: {ACTION_KO.get(a.type, a.type)}", depth, lane=0, status=st,
                   conclusion_raw=f"{a.type} {a.target} {a.params or ''} -> {a.status}", action_type=a.type)
        for t in prev_tail:
            g.edge(t, n["id"])
        prev_tail = [n["id"]]
        depth += 1
    label = {"resolved": "자동 복구 완료", "handed_off": "운영자 인계", "failed": "처리 실패"}.get(inc.status, "결과 대기")
    g.node("outcome", "outcome", label, depth, lane=0,
           status="done" if inc.status in ("resolved", "handed_off") else "failed" if inc.status == "failed" else "planned",
           conclusion_raw=inc.status_reason)
    for t in prev_tail:
        g.edge(t, "outcome")
    depth += 1
    prev = "outcome"
    for c in commands:
        n = g.node(f"command:{c.id}", "command", f"운영자 지시 실행: {c.instruction[:40]}", depth, lane=0,
                   status=COMMAND_STATUS.get(c.status, "planned"), conclusion_raw=c.instruction, command_status=c.status)
        g.edge(prev, n["id"], "steer")
        prev = n["id"]
        depth += 1
    return {"nodes": g.nodes, "edges": g.edges}
