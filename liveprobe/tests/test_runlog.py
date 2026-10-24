"""P5: run feed, operator steering, pause/stop at node boundaries."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import needs_db


@pytest.fixture()
def iid(db_url):
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint="run", namespace="liveprobe-target", workload="inventory", status="analyzing",
                       title="t", signal_reasons=[])
        s.add(inc); s.flush()
        return inc.id


@needs_db
def test_events_are_ordered_per_incident_and_replayable(iid):
    from app import runlog
    for i in range(3):
        runlog.emit(iid, "note", f"step {i}")
    evs = runlog.events_since(iid)
    assert [e["seq"] for e in evs] == [1, 2, 3]
    assert [e["seq"] for e in runlog.events_since(iid, since=2)] == [3]


@needs_db
def test_emit_redacts_secrets_and_unknown_kind_becomes_note(iid):
    from app import runlog
    runlog.emit(iid, "bogus_kind", "token=abc123supersecretvalue and more", {"password": "hunter2"})
    e = runlog.events_since(iid)[0]
    assert e["kind"] == "note" and "abc123supersecretvalue" not in e["summary"] and "hunter2" not in str(e["detail"])


@needs_db
def test_message_is_applied_once_at_next_boundary_and_marked_as_instruction(iid):
    from app import runlog
    runlog.add_message(iid, "inventory 대신 order 쪽 로그를 먼저 보세요")
    msgs = runlog.take_messages(iid, "에이전트 signal_analyst 시작")
    assert msgs == ["inventory 대신 order 쪽 로그를 먼저 보세요"]
    assert runlog.take_messages(iid, "다음") == []          # never applied twice
    p = runlog.with_operator_instructions("BASE", msgs)
    assert p.startswith("BASE") and "NOT evidence" in p and "order 쪽" in p
    kinds = [e["kind"] for e in runlog.events_since(iid)]
    assert kinds == ["operator_message", "operator_applied"]
    assert runlog.with_operator_instructions("BASE", []) == "BASE"


@needs_db
def test_gate_blocks_while_paused_and_continues_on_resume(iid):
    from app import runlog
    runlog.set_control(iid, "pause")
    passed = threading.Event()
    t = threading.Thread(target=lambda: (runlog.gate(iid, "다음 단계", sleep=lambda s: time.sleep(0.05)), passed.set()))
    t.start()
    time.sleep(0.3)
    assert not passed.is_set()                               # held at the boundary
    runlog.set_control(iid, "resume")
    t.join(3)
    assert passed.is_set()


@needs_db
def test_stop_raises_at_boundary_and_is_final(iid):
    from app import runlog
    runlog.set_control(iid, "stop")
    with pytest.raises(runlog.RunStopped):
        runlog.gate(iid, "에이전트 verifier")
    assert runlog.set_control(iid, "resume") == "stopping"   # a stop request cannot be undone by resume


@needs_db
def test_pause_beyond_limit_stops(iid):
    from app import runlog
    runlog.set_control(iid, "pause")
    with pytest.raises(runlog.RunStopped):
        runlog.gate(iid, "x", sleep=lambda s: None, max_wait_s=3)


@needs_db
def test_gate_is_open_by_default(iid):
    from app import runlog
    assert runlog.control_state(iid) == "running"
    runlog.gate(iid, "아무 단계")


# ---------------------------------------------------------------- engine integration (fake LLM, no cluster)
@needs_db
def test_engine_call_gates_instructs_and_logs(iid, monkeypatch):
    from app import runlog
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.llm import AGENT_SCHEMA, AgentOutput

    seen = {"calls": 0}

    class Prov:
        name = "codex_cli"
        def complete(self, role, prompt, schema, model, effort, timeout):
            from app.llm.base import LLMResult, Usage
            seen["prompt"] = prompt
            seen["calls"] += 1
            return LLMResult(text="{}", data={"summary": "원인은 이미지 태그", "root_cause_candidates": [
                {"cause": "bad tag", "confidence": 0.9, "evidence_refs": ["E1"]}], "counter_evidence": [], "confidence": 0.9,
                "next_action": "handoff", "recommended_steps": []}, usage=Usage(input_tokens=5, output_tokens=7),
                duration_ms=11, provider="codex_cli", model=model, effort=effort)

    cfg = AppConfig()
    eng = Engine(cfg, Settings(), kube=None, provider=Prov(), rag=None)
    runlog.add_message(iid, "태그만 확인해 주세요")
    r = eng._call(iid, "signal_analyst", "ANALYSE", AGENT_SCHEMA, AgentOutput, hits=[])
    assert r.ok and "태그만 확인해 주세요" in seen["prompt"]
    kinds = [e["kind"] for e in runlog.events_since(iid)]
    assert kinds == ["operator_message", "operator_applied", "agent_started", "agent_finished"]
    fin = runlog.events_since(iid)[-1]
    assert fin["detail"]["model"] and fin["detail"]["root_cause_candidates"][0]["cause"] == "bad tag"

    runlog.set_control(iid, "stop")
    with pytest.raises(runlog.RunStopped):
        eng._call(iid, "result_verifier", "VERIFY", AGENT_SCHEMA, AgentOutput, hits=[])
    assert seen["calls"] == 1                                # the stopped run never reached the provider again


# ---------------------------------------------------------------- API
@pytest.fixture()
def api(db_url, monkeypatch):
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config
    config.get_settings.cache_clear(); config.get_app_config.cache_clear()
    from app.main import app
    with TestClient(app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        yield c
    config.get_settings.cache_clear()


@needs_db
def test_api_feed_message_control_roundtrip(api, iid):
    assert api.get(f"/api/incidents/{iid}/events").json() == {"events": [], "control": "running"}
    r = api.post(f"/api/incidents/{iid}/messages", json={"body": "로그부터 보세요"})
    assert r.status_code == 202 and r.json()["applies"] == "next_node_boundary"
    assert api.post(f"/api/incidents/{iid}/control", json={"action": "pause"}).json() == {"state": "paused"}
    body = api.get(f"/api/incidents/{iid}/events").json()
    # P15: every chat message is followed by the AI's reading of it (steering_interpreted)
    assert [e["kind"] for e in body["events"]] == ["operator_message", "steering_interpreted", "paused"]
    assert body["control"] == "paused"
    assert [e["seq"] for e in api.get(f"/api/incidents/{iid}/events?since=2").json()["events"]] == [3]


@needs_db
def test_api_validation_and_finished_incident(api, iid):
    assert api.post(f"/api/incidents/{iid}/messages", json={"body": "   "}).status_code == 422
    assert api.post(f"/api/incidents/{iid}/messages", json={"body": "x" * 2001}).status_code == 422
    assert api.post(f"/api/incidents/{iid}/control", json={"action": "explode"}).status_code == 422
    assert api.post("/api/incidents/99999/messages", json={"body": "hi"}).status_code == 404
    from app import db
    from app.models import Incident
    with db.session() as s:
        s.get(Incident, iid).status = "resolved"
    # P15: post-handoff messages go to commands; without the isolated executor they fail closed (503).
    assert api.post(f"/api/incidents/{iid}/messages", json={"body": "hi"}).status_code == 503
    assert api.post(f"/api/incidents/{iid}/control", json={"action": "stop"}).status_code == 409


@needs_db
def test_api_steering_is_guarded(api, iid):
    for path, body in ((f"/api/incidents/{iid}/messages", {"body": "hi"}), (f"/api/incidents/{iid}/control", {"action": "pause"})):
        assert api.post(path, json=body, headers={"Host": "evil.example"}).status_code == 403
        assert api.post(path, json=body, headers={"X-LiveProbe-Client": ""}).status_code == 403
        assert api.post(path, json=body, headers={"Origin": "https://attacker.invalid"}).status_code == 403


# ---------------------------------------------------------------- P6: quota waiting in the engine
def _quota_engine(provider, **settings):
    from app.config import AppConfig, Settings
    from app.engine import Engine
    eng = Engine(AppConfig(), Settings(quota_recheck_s=1, **settings), kube=None, provider=provider, rag=None)
    eng._sleep = lambda s: None            # no real waiting in tests
    return eng


class _QuotaThenOk:
    name = "codex_cli"
    def __init__(self, n_quota):
        self.n, self.calls = n_quota, 0
    def next_available_in(self): return 1
    def status(self): return {"active": None, "accounts": []}
    def drain_events(self): return []
    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMError, LLMResult, Usage
        self.calls += 1
        if self.calls <= self.n:
            raise LLMError("quota", "codex usage limit reached")
        return LLMResult(text="{}", data={"summary": "ok", "root_cause_candidates": [], "counter_evidence": [],
                                          "confidence": 0.8, "next_action": "handoff", "recommended_steps": []},
                         usage=Usage(1, 1), duration_ms=1, provider="codex_cli", model=model, effort=effort)


@needs_db
def test_engine_waits_on_quota_then_resumes_without_another_provider(iid):
    from app import runlog
    from app.llm import AGENT_SCHEMA, AgentOutput
    prov = _QuotaThenOk(3)
    r = _quota_engine(prov)._call(iid, "signal_analyst", "P", AGENT_SCHEMA, AgentOutput, hits=[])
    assert r.ok and prov.calls == 4
    kinds = [e["kind"] for e in runlog.events_since(iid)]
    assert kinds.count("waiting_quota") == 1 and "resumed" in kinds and kinds[-1] == "agent_finished"


@needs_db
def test_engine_stop_during_quota_wait_ends_the_wait(iid):
    from app import runlog
    from app.llm import AGENT_SCHEMA, AgentOutput
    prov = _QuotaThenOk(10**6)
    eng = _quota_engine(prov)
    state = {"n": 0}
    def sleep(_):
        state["n"] += 1
        if state["n"] == 2:
            runlog.set_control(iid, "stop")
    eng._sleep = sleep
    with pytest.raises(runlog.RunStopped):
        eng._call(iid, "signal_analyst", "P", AGENT_SCHEMA, AgentOutput, hits=[])
    assert prov.calls <= 3                                  # it stopped waiting, it did not keep hammering


@needs_db
def test_engine_gives_up_after_the_wait_limit_with_a_failed_call_not_a_fallback(iid):
    from app import runlog
    from app.llm import AGENT_SCHEMA, AgentOutput
    prov = _QuotaThenOk(10**6)
    r = _quota_engine(prov, quota_wait_max_s=3)._call(iid, "signal_analyst", "P", AGENT_SCHEMA, AgentOutput, hits=[])
    assert not r.ok and r.error_kind == "quota"             # surfaced as a failed agent run -> handoff, state kept
    assert [e["kind"] for e in runlog.events_since(iid)].count("waiting_quota") == 1
