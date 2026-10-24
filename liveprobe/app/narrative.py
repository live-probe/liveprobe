"""P15: the incident story for the war room — Korean headline, raw items with readings, flow graph."""
from __future__ import annotations

from sqlalchemy import select

from .flowgraph import ACTION_KO, build_flow, signal_ko
from .config import get_app_config
from .remediation import remediation_label
from .interpret import incident_items, readings
from .models import INCIDENT_CLOSED_STATES, Action, AgentRun, Incident, OperatorCommand, RoutingDecision, RunEvent
from .roles_ko import GROUP_KO, LEVEL_KO, role_info

STAGE_KO = {"routing": "사건 수준 판단 중", "analyzing": "Agent 분석 중", "verifying": "결과 확인 중", "acting": "조치 실행 중",
            "approval": "운영자 승인 대기", "resolved": "자동 복구 완료", "handed_off": "운영자 인계", "failed": "처리 실패",
            "closed": "운영자 종료", "collecting": "근거 수집 중"}


def stage_of(inc: Incident, routing: list, runs: list, events: list) -> dict:
    st = inc.status
    if st in INCIDENT_CLOSED_STATES:
        key = st
    elif st == "action_pending":
        key = "approval"
    elif st == "acting":
        key = "acting"
    elif not routing:
        key = "routing" if any(e.kind == "evidence_collected" for e in events) else "collecting"
    else:
        last_started = next((e.detail.get("role") for e in reversed(events) if e.kind == "agent_started"
                             and isinstance(e.detail, dict)), None)
        key = "verifying" if last_started in ("result_verifier", "group_verifier", "safety_verifier") else "analyzing"
    label = STAGE_KO[key]
    if key in ("analyzing", "verifying"):
        role = next((e.detail.get("role") for e in reversed(events) if e.kind == "agent_started" and isinstance(e.detail, dict)), None)
        if role:
            label = f"{label} — {role_info(role)['role_ko']}"
    return {"key": key, "label": label}


def _impact(items: list[dict], inc: Incident) -> str:
    parts = []
    dep = next((i for i in items if i["ref"] == "E1_deployment"), None)
    if dep and dep.get("ko"):
        parts.append(dep["ko"])
    svc = next((i for i in items if i["ref"] == "E3_service"), None)
    if svc and svc.get("ko") and "0개" in svc["ko"]:
        parts.append(svc["ko"])
    if any(i["ref"] == "E7_error_logs" and i.get("ko") and ("503" in i["raw"] or "정상 대상" in i["ko"]) for i in items):
        parts.append("호출하는 서비스에서 요청 실패(5xx)가 함께 관측되었습니다.")
    if not parts:
        parts.append(f"{inc.namespace}/{inc.workload}에서 이상 신호가 관측되었습니다. 영향 범위는 근거 수집 후 표시됩니다.")
    return " ".join(parts[:3])


def _next(inc: Incident, actions: list, commands: list) -> str:
    pending_cmd = next((c for c in commands if c.status in ("awaiting_confirmation", "awaiting_secret")), None)
    if pending_cmd:
        return "운영자 지시의 실행 계획과 부작용을 확인하고 진행 여부를 결정하세요."
    if inc.status == "resolved":
        a = next((x for x in reversed(actions) if x.status == "succeeded"), None)
        what = ACTION_KO.get(a.type, a.type) if a else "조치"
        return f"AI가 '{what}'을(를) 실행하고 검증까지 마쳤습니다. 재발 여부만 지켜보면 됩니다."
    if inc.status == "closed":
        return "운영자가 종료했습니다. 클러스터는 변경하지 않았고 이 장애에는 더 이상 지시를 받지 않습니다. 같은 증상이 다시 감지되면 새 장애로 접수됩니다."
    if inc.status == "handed_off":
        return "AI가 자동 조치를 멈추고 인계했습니다. 오른쪽 채팅으로 AI에게 작업을 지시할 수 있습니다(실행 전 부작용을 확인합니다)."
    if inc.status == "failed":
        return "처리 중 오류가 발생했습니다. 기록을 확인한 뒤 채팅으로 AI에게 후속 작업을 지시하세요."
    if inc.status == "action_pending":
        return "AI가 제안한 조치를 승인하거나 거절하세요."
    return "AI가 분석 중입니다. 채팅으로 조사 방향을 지시하면 다음 단계부터 반영됩니다."


def build_narrative(s, iid: int) -> dict | None:
    inc = s.get(Incident, iid)
    if inc is None:
        return None
    routing = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid).order_by(RoutingDecision.id)))
    runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
    events = list(s.scalars(select(RunEvent).where(RunEvent.incident_id == iid).order_by(RunEvent.seq)))
    actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
    commands = list(s.scalars(select(OperatorCommand).where(OperatorCommand.incident_id == iid).order_by(OperatorCommand.id)))

    cited: dict[str, list[str]] = {}
    from .flowgraph import refs_of
    for r in runs:
        for ref in refs_of(r.output):
            cited.setdefault(ref, [])
            if r.role not in cited[ref]:
                cited[ref].append(r.role)
    items = readings(s, iid, incident_items(s, iid))
    for it in items:
        it["cited_by"] = list(cited.get(it["ref"], [])) if it["kind"] != "agent" else []
    order = {"error": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: (i["kind"] == "agent", order.get(i["severity"], 3)))

    level = inc.severity_level
    lvl = f"{LEVEL_KO.get(level, level)} 수준 · {GROUP_KO.get(level, '')}" if level else "수준 판단 전"
    remediation = (inc.handoff or {}).get("remediation")
    what = f"{inc.namespace}/{inc.workload}: {signal_ko(inc.signal_reasons)} ({lvl})"
    label = remediation_label(remediation)
    if label:
        what = label + " · " + what
    pending = [i for i in items if i.get("ko") is None and i["kind"] in ("event", "log", "agent")]
    last_work = max((r.id for r in runs if r.role != "interpreter"), default=0)
    last_interp = max((r.id for r in runs if r.role == "interpreter"), default=0)
    interpreting = bool(pending) and (inc.status not in INCIDENT_CLOSED_STATES or last_interp < last_work)
    total_cost = sum(r.estimated_cost_usd or 0 for r in runs) if runs else None
    incident = {"id": inc.id, "title": inc.title, "namespace": inc.namespace, "workload": inc.workload, "status": inc.status,
                "severity_level": level, "signal_count": inc.signal_count,
                "system": inc.system, "environment": get_app_config().environment(inc.namespace),
                "created_at": inc.created_at.isoformat() if inc.created_at else None,
                "updated_at": inc.updated_at.isoformat() if inc.updated_at else None,
                "closed_at": inc.closed_at.isoformat() if inc.closed_at else None,
                "total_cost_usd": round(total_cost, 6) if total_cost is not None else None, "agent_run_count": len(runs)}
    return {
        "incident": incident,
        "headline": {"what": what, "impact": _impact(items, inc), "stage": stage_of(inc, routing, runs, events),
                     "next": _next(inc, actions, commands), "remediation": remediation, "source": "rules"},
        "raw": items,
        "flow": build_flow(s, iid),
        "interpreting": interpreting,
    }
