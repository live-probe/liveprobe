"""P15: operator chat steering while an analysis runs.

A chat message is classified once (deterministic rules, no LLM: stop/reroute must act immediately and predictably)
into one intent and applied at the next agent boundary:

* instruction     -> appended to the next agent's prompt as an operator instruction (never evidence)
* escalate        -> the next agent runs one step higher on the model/effort ladder
* reroute         -> the current group stops at the next boundary and the requested group re-analyses the same
                     evidence (HIGH_RISK is never lowered; same level is refused)
* stop            -> the run stops at the next boundary (same as the stop control)
* cluster_change  -> during analysis it is passed to agents as an instruction; after the incident is finished the
                     operator issues it as a command (plan -> dry-run -> side effects -> confirm)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from . import db, runlog
from .roles_ko import GROUP_KO, role_info

LEVEL_ORDER = {"SIMPLE": 0, "NORMAL": 1, "COMPLEX": 2, "HIGH_RISK": 3}


class RerouteRequested(Exception):
    """Raised at an agent boundary when the operator asked for another group."""

    def __init__(self, level: str, message_id: int, body: str):
        super().__init__(level)
        self.level, self.message_id, self.body = level, message_id, body


_NEG = re.compile(r"하지\s*(마|말)|말고|금지|않게|don'?t|do not|never", re.I)
_STOP = re.compile(r"중단|멈춰|멈추|정지|그만|\bstop\b|\babort\b", re.I)
_LEVELS = [(re.compile(r"고위험|high[\s_-]?risk", re.I), "HIGH_RISK"), (re.compile(r"복잡|complex", re.I), "COMPLEX"),
           (re.compile(r"보통|normal", re.I), "NORMAL"), (re.compile(r"단순|simple", re.I), "SIMPLE")]
_GROUPISH = re.compile(r"그룹|group|수준|등급|레벨|level|(으로|로)\s*(바꿔|전환|변경|다시|재분석|분석)|전환|재분류", re.I)
_ESCALATE = re.compile(r"(높은|상위|강한|좋은|고성능|상급|큰)\s*(모델|model)|모델\S*\s*(올려|높여|승격|바꿔)|escalat|"
                       r"upgrade|추론\S*\s*(높여|올려|강도)", re.I)
_K8S = re.compile(r"롤백|rollback|되돌려|재시작|restart|재배포|redeploy|scale|스케일|레플리카|replica|메모리|memory|\bcpu\b|"
                  r"limit|한도|삭제|지워|delete|생성|create|배포|deploy|패치|patch|apply|cordon|drain|configmap|"
                  r"secret|시크릿|이미지\s*(바꿔|변경)|image", re.I)
_IMPERATIVE = re.compile(r"(해|해줘|해라|하세요|하라|하자|올려|늘려|줄여|내려|바꿔|변경|설정|적용|만들어|지워|돌려|줘)\s*[.!]?\s*$|"
                         r"(해|해줘|올려|늘려|줄여|내려|바꿔)\b", re.I)

ACK = {
    "instruction": "다음 단계 Agent에게 운영자 지시로 전달합니다. 근거가 아니라 지시로 취급하며 안전 규칙보다 우선하지 않습니다.",
    "escalate": "다음에 실행되는 Agent를 한 단계 높은 모델(또는 추론 강도)로 실행합니다.",
    "stop": "다음 단계가 시작되기 전에 분석을 멈추고 운영자에게 인계합니다.",
    "cluster_change": ("클러스터 변경 지시입니다. 분석 중에는 Agent에게 지시로 전달합니다. 분석이 끝난 뒤 같은 지시를 보내면 실행 계획과 "
                       "부작용을 먼저 보여 드리고 확인을 받은 다음 실행합니다."),
}


def classify(text: str) -> dict:
    t = (text or "").strip()
    if _NEG.search(t):
        return {"intent": "instruction", "target_level": None, "ack_ko": ACK["instruction"]}
    if _STOP.search(t):
        return {"intent": "stop", "target_level": None, "ack_ko": ACK["stop"]}
    if _GROUPISH.search(t):
        for pat, level in _LEVELS:
            if pat.search(t):
                return {"intent": "reroute", "target_level": level,
                        "ack_ko": f"다음 단계부터 {GROUP_KO[level]}으로 바꿔 같은 근거를 다시 분석합니다. 고위험 수준은 낮출 수 없습니다."}
    if _ESCALATE.search(t):
        return {"intent": "escalate", "target_level": None, "ack_ko": ACK["escalate"]}
    if _K8S.search(t) and _IMPERATIVE.search(t):
        return {"intent": "cluster_change", "target_level": None, "ack_ko": ACK["cluster_change"]}
    return {"intent": "instruction", "target_level": None, "ack_ko": ACK["instruction"]}


def reroute_allowed(current: str | None, target: str) -> bool:
    if current is None or current == target or target not in LEVEL_ORDER:
        return False
    return current != "HIGH_RISK"


def escalate_model(cfg, model: str, effort: str | None) -> tuple[str, str | None] | None:
    vendor = "anthropic" if cfg.llm.provider in ("claude_cli", "anthropic_api") else "openai"
    ladder = cfg.workflows.model_ladder.get(vendor) or []
    if model in ladder and ladder.index(model) + 1 < len(ladder):
        return ladder[ladder.index(model) + 1], effort
    efforts = cfg.workflows.effort_ladder
    if effort in efforts and efforts.index(effort) + 1 < len(efforts):
        return model, efforts[efforts.index(effort) + 1]
    return None


def post(iid: int, body: str, actor: str = "local-operator") -> dict:
    """Store the chat message, classify it, record the reading; a stop request takes effect immediately."""
    from .models import OperatorMessage
    mid = runlog.add_message(iid, body, actor=actor)
    c = classify(body)
    with db.session() as s:
        row = s.get(OperatorMessage, mid)
        row.intent = c["intent"]
        row.intent_detail = {"target_level": c["target_level"]}
    runlog.emit(iid, "steering_interpreted", c["ack_ko"], {"message_id": mid, **c}, actor="liveprobe")
    if c["intent"] == "stop":
        runlog.set_control(iid, "stop", actor=actor)
    return {"message_id": mid, **c}


@dataclass
class Steer:
    texts: list[str] = field(default_factory=list)
    escalate: bool = False
    reroute: RerouteRequested | None = None


def take(iid: int, role: str, level: str | None) -> Steer:
    """Consume pending chat at the boundary before `role`. A reroute stops consumption: later messages wait for the
    first agent of the new group. Before routing (level None) a reroute message stays pending."""
    from .models import OperatorMessage
    out, consumed, refused = Steer(), [], []
    with db.session() as s:
        rows = s.scalars(select(OperatorMessage).where(OperatorMessage.incident_id == iid, OperatorMessage.applied_at.is_(None))
                         .order_by(OperatorMessage.id)).all()
        for m in rows:
            intent = m.intent or "instruction"
            if intent == "reroute":
                target = (m.intent_detail or {}).get("target_level")
                if level is None:
                    break
                m.applied_at = datetime.now(timezone.utc)
                consumed.append(m.id)
                if reroute_allowed(level, target):
                    out.reroute = RerouteRequested(target, m.id, m.body)
                    break
                refused.append((m.id, target))
                continue
            m.applied_at = datetime.now(timezone.utc)
            consumed.append(m.id)
            if intent == "escalate":
                out.escalate = True
            elif intent == "instruction":
                out.texts.append(m.body)
            # cluster_change must use dry-run / side-effects / explicit confirmation, never the autonomous path.
    info = role_info(role)
    for mid, target in refused:
        why = ("고위험 수준은 자동으로 낮출 수 없어" if level == "HIGH_RISK" else f"이미 {GROUP_KO.get(level, level)}로 분석 중이라")
        runlog.emit(iid, "steering_interpreted", f"Group 변경 요청을 따르지 않았습니다: {why} 현재 {GROUP_KO.get(level, level)}를 유지합니다.",
                    {"message_id": mid, "intent": "reroute", "target_level": target, "refused": True})
    if consumed:
        runlog.emit(iid, "operator_applied", f"운영자 메시지 {len(consumed)}건을 {info['role_ko']}({role}) 시작 전에 반영했습니다.",
                    {"message_ids": consumed, "applied_to_role": role, "escalate": out.escalate,
                     "reroute": out.reroute.level if out.reroute else None})
    return out
