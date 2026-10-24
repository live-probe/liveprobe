"""P15 W7: operator chat steering — intent classification and how each intent changes the running analysis."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import needs_db


@pytest.mark.parametrize("text,intent,level", [
    ("로그부터 다시 봐", "instruction", None),
    ("order 서비스 쪽 원인을 먼저 확인해", "instruction", None),
    ("더 높은 모델로 분석해", "escalate", None),
    ("상위 모델로 올려서 다시 봐줘", "escalate", None),
    ("고위험으로 바꿔서 분석해", "reroute", "HIGH_RISK"),
    ("복잡 그룹으로 다시 분석해", "reroute", "COMPLEX"),
    ("보통 Group으로 전환", "reroute", "NORMAL"),
    ("분석 중단해", "stop", None),
    ("멈춰", "stop", None),
    ("중단하지 말고 계속 봐", "instruction", None),
    ("inventory 메모리 1Gi로 올려", "cluster_change", None),
    ("이전 버전으로 롤백해", "cluster_change", None),
    ("롤백은 하지 마", "instruction", None),
])
def test_classify_operator_chat(text, intent, level):
    from app.steering import classify
    c = classify(text)
    assert c["intent"] == intent, c
    assert c.get("target_level") == level
    assert any("\uac00" <= ch <= "\ud7a3" for ch in c["ack_ko"])


def test_operator_escalation_climbs_model_then_effort():
    from app.config import AppConfig
    from app.steering import escalate_model
    cfg = AppConfig()
    assert escalate_model(cfg, "gpt-6-luna", "low") == ("gpt-6.1-sol", "low")
    assert escalate_model(cfg, "gpt-6-astra", "high") == ("gpt-6-astra", "xhigh")
    assert escalate_model(cfg, "gpt-6-astra", "xhigh") is None


def test_reroute_never_lowers_high_risk():
    from app.steering import reroute_allowed
    assert reroute_allowed("SIMPLE", "COMPLEX") and reroute_allowed("COMPLEX", "NORMAL")
    assert reroute_allowed("NORMAL", "HIGH_RISK")
    assert not reroute_allowed("HIGH_RISK", "SIMPLE") and not reroute_allowed("HIGH_RISK", "COMPLEX")
    assert not reroute_allowed("COMPLEX", "COMPLEX")


# ---------------------------------------------------------------- DB / engine
@pytest.fixture()
def iid(db_url):
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint="steer", namespace="liveprobe-target", workload="inventory", status="analyzing",
                       title="t", signal_reasons=[])
        s.add(inc)
        s.flush()
        return inc.id


class Prov:
    name = "codex_cli"

    def __init__(self, on_call=None):
        self.calls: list[tuple] = []
        self.prompts: list[str] = []
        self.on_call = on_call

    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMResult, Usage
        self.calls.append((role, model, effort))
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call(role)
        return LLMResult(text="{}", data={"summary": "s", "root_cause_candidates": [], "counter_evidence": [], "confidence": 0.9,
                                          "next_action": "handoff", "recommended_steps": []},
                         usage=Usage(input_tokens=5, output_tokens=7), duration_ms=1, provider="codex_cli", model=model, effort=effort)


def _engine(prov):
    from app.config import AppConfig, Settings
    from app.engine import Engine
    return Engine(AppConfig(), Settings(), kube=None, provider=prov, rag=None)


@needs_db
def test_instruction_is_applied_to_the_next_agent_and_recorded_with_its_role(iid):
    from app import runlog, steering
    from app.llm import AGENT_SCHEMA, AgentOutput
    steering.post(iid, "로그부터 다시 봐")
    prov = Prov()
    eng = _engine(prov)
    eng._call(iid, "router", "ROUTE", AGENT_SCHEMA, AgentOutput, hits=[])          # the router never consumes chat
    assert "로그부터 다시 봐" not in prov.prompts[0]
    eng._call(iid, "signal_analyst", "ANALYSE", AGENT_SCHEMA, AgentOutput, hits=[])
    assert "로그부터 다시 봐" in prov.prompts[1]
    ev = runlog.events_since(iid)
    kinds = [e["kind"] for e in ev]
    assert kinds[:2] == ["operator_message", "steering_interpreted"]
    applied = next(e for e in ev if e["kind"] == "operator_applied")
    assert applied["detail"]["applied_to_role"] == "signal_analyst" and "신호 분석" in applied["summary"]


@needs_db
def test_escalate_runs_the_next_agent_one_model_higher(iid):
    from app import runlog, steering
    from app.llm import AGENT_SCHEMA, AgentOutput
    steering.post(iid, "더 높은 모델로 분석해")
    prov = Prov()
    eng = _engine(prov)
    base = eng.cfg.llm.roles["signal_analyst"].model
    eng._call(iid, "signal_analyst", "ANALYSE", AGENT_SCHEMA, AgentOutput, hits=[])
    assert prov.calls[0][1] != base
    esc = next(e for e in runlog.events_since(iid) if e["kind"] == "escalated")
    assert esc["detail"]["by"] == "operator" and esc["detail"]["from_model"] == base and esc["detail"]["role"] == "signal_analyst"
    eng._call(iid, "result_verifier", "VERIFY", AGENT_SCHEMA, AgentOutput, hits=[])
    assert prov.calls[1][1] == eng.cfg.llm.roles["result_verifier"].model   # one escalation, one agent


@needs_db
def test_cluster_change_chat_never_becomes_an_autonomous_agent_instruction(iid):
    from app import steering
    from app.llm import AGENT_SCHEMA, AgentOutput
    steering.post(iid, "inventory 메모리 1Gi로 올려")
    prov = Prov()
    _engine(prov)._call(iid, "signal_analyst", "ANALYSE", AGENT_SCHEMA, AgentOutput, hits=[])
    assert "1Gi" not in prov.prompts[0]     # only a separately confirmed command may execute this instruction


@needs_db
def test_stop_via_chat_stops_at_the_next_boundary(iid):
    from app import runlog, steering
    from app.llm import AGENT_SCHEMA, AgentOutput
    steering.post(iid, "분석 중단해")
    assert runlog.control_state(iid) == "stopping"
    with pytest.raises(runlog.RunStopped):
        _engine(Prov())._call(iid, "signal_analyst", "ANALYSE", AGENT_SCHEMA, AgentOutput, hits=[])


@needs_db
def test_reroute_at_boundary_raises_only_when_allowed(iid):
    from app import runlog, steering
    from app.llm import AGENT_SCHEMA, AgentOutput
    steering.post(iid, "복잡 그룹으로 다시 분석해")
    eng = _engine(Prov())
    eng._current_level = "SIMPLE"
    with pytest.raises(steering.RerouteRequested) as e:
        eng._call(iid, "result_verifier", "VERIFY", AGENT_SCHEMA, AgentOutput, hits=[])
    assert e.value.level == "COMPLEX"
    steering.post(iid, "단순 그룹으로 바꿔")
    eng._current_level = "HIGH_RISK"
    eng._call(iid, "safety_verifier", "VERIFY", AGENT_SCHEMA, AgentOutput, hits=[])      # refused, analysis continues
    refused = [e for e in runlog.events_since(iid) if e["kind"] == "steering_interpreted" and e["detail"].get("refused")]
    assert refused and "고위험" in refused[-1]["summary"]


def _simple_incident(signal="ImagePullBackOff"):
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint=f"steer-{signal}", namespace="liveprobe-target", workload="inventory", title="t",
                       status="open", signal_reasons=[signal])
        s.add(inc)
        s.flush()
        return inc.id


@needs_db
def test_mid_run_reroute_switches_to_the_requested_group(db_url, monkeypatch):
    from sqlalchemy import select
    from app import db, engine as engine_module, steering
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.models import AgentRun, Incident, RoutingDecision
    from test_agent_groups import FakeProvider, _simple_bundle, good, verifier
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: _simple_bundle())
    iid = _simple_incident()
    posted = {"done": False}

    class P(FakeProvider):
        def complete(self, role, prompt, schema, model, effort, timeout):
            from app.llm.base import LLMResult, Usage
            self.calls.append((role, model, effort))
            if role == "router":
                data = {"level": "SIMPLE", "rationale": "image", "confidence": 0.9}
            elif role == "signal_analyst":
                if not posted["done"]:
                    posted["done"] = True
                    steering.post(iid, "복잡 그룹으로 다시 분석해")
                data = {"summary": "bad tag", "root_cause_candidates": [{"cause": "bad tag", "confidence": 0.9,
                        "evidence_refs": ["E1"]}], "counter_evidence": [], "confidence": 0.9, "next_action": "handoff",
                        "recommended_steps": []}
            elif role in ("group_verifier", "challenger"):
                data = verifier()
            else:
                data = good(evidence=[{"ref": "E2_pods", "quote": "ImagePullBackOff", "supports": "c0"}],
                            root_cause_candidates=[{"cause": "bad tag", "confidence": 0.9, "evidence_refs": ["E2_pods"]}])
            return LLMResult(text="", data=data, usage=Usage(input_tokens=100, output_tokens=50), duration_ms=1,
                             provider="fake", model=model, effort=effort)

    prov = P({})
    Engine(AppConfig(), Settings(), None, prov, None).process(iid)
    roles = [c[0] for c in prov.calls]
    assert "signal_analyst" in roles and "result_verifier" not in roles     # SIMPLE stopped at its next boundary
    assert "k8s_log_analyst" in roles                                        # COMPLEX ran instead
    with db.session() as s:
        rds = list(s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid).order_by(RoutingDecision.id)))
        assert [r.level for r in rds] == ["SIMPLE", "COMPLEX"] and rds[1].decided_by == "operator"
        assert "복잡" in rds[1].rationale
        inc = s.get(Incident, iid)
        assert inc.severity_level == "COMPLEX" and inc.status in ("handed_off", "resolved")
        assert s.query(AgentRun).filter_by(incident_id=iid, role="k8s_log_analyst").count() >= 1


@needs_db
def test_api_message_returns_intent_and_ack(db_url, monkeypatch, iid):
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    from app.main import app
    with TestClient(app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        r = c.post(f"/api/incidents/{iid}/messages", json={"body": "고위험으로 바꿔서 분석해"})
        assert r.status_code == 202
        body = r.json()
        assert body["intent"] == "reroute" and body["target_level"] == "HIGH_RISK" and body["ack_ko"]
        assert body["applies"] == "next_node_boundary"
    config.get_settings.cache_clear()
