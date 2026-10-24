"""KAN-41 router and graph behavior with fake agent outputs."""
from datetime import datetime, timedelta, timezone

import pytest

from app.evidence import Bundle
from app.router import decide
from app.workflows import AgentCall, build_high_risk_graph, build_simple_graph

NOW = datetime.now(timezone.utc)


def family1():
    b = Bundle("liveprobe-target", "inventory")
    b.pods = [{"name": "p", "containers": [{"waiting_reason": "ImagePullBackOff", "ready": False}]}]
    b.deployment = {"current_revision": 2, "current_revision_created": NOW.isoformat(),
                    "images": ["inventory:bad"], "revisions": [
                        {"revision": 2, "images": ["inventory:bad"]},
                        {"revision": 1, "images": ["inventory:dev"]}]}
    return b


def family4():
    b = Bundle("liveprobe-target", "inventory")
    b.service = {"selector": {"app": "broken"}, "matching_pods": [],
                 "pods_labeled_app_eq_service": ["inventory-pod"], "ready_endpoints": 0}
    b.callers = [{"name": "order", "http_5xx_rate_per_s": 1}]
    return b


def test_router_family1_simple_and_unknown_complex():
    assert decide(family1(), 15).level == "SIMPLE"
    assert decide(Bundle("liveprobe-target", "unknown"), 15).decided_by == "router_failure"


def test_reused_replicaset_uses_deployment_progress_timestamp():
    b = family1()
    b.deployment["current_revision_created"] = (NOW - timedelta(hours=1)).isoformat()
    b.deployment["recent_rollout_at"] = NOW.isoformat()
    assert decide(b, 15).level == "SIMPLE"


def test_transient_caller_error_log_with_healthy_endpoint_keeps_simple():
    b = family1()
    b.logs = ["[order] transient startup timeout"]
    b.callers = [{"name": "order", "http_5xx_rate_per_s": 0}]
    b.service = {"selector": {"app": "inventory"}, "matching_pods": ["healthy"],
                 "pods_labeled_app_eq_service": ["healthy"], "ready_endpoints": 1}
    b.pods.append({"name": "healthy", "containers": [{"waiting_reason": None, "ready": True}]})
    assert decide(b, 15).level == "SIMPLE"


def test_router_family4_high_risk_cannot_be_lowered():
    d = decide(family4(), 15, lambda prompt: {"level": "SIMPLE", "rationale": "wrong", "confidence": 1})
    assert d.level == "HIGH_RISK" and d.decided_by == "rules+llm"
    assert "rules win" in d.rationale


@pytest.mark.parametrize("risk", ["network", "protected", "routing"])
@pytest.mark.parametrize("suggested", ["SIMPLE", "NORMAL", "COMPLEX", "HIGH_RISK"])
def test_deterministic_high_risk_remains_high_even_when_classifier_agrees(risk, suggested):
    bundle = family1() if risk != "routing" else family4()
    signals = ["NetworkDrop"] if risk == "network" else []
    if risk == "protected":
        bundle.add("incident", "liveprobe-target/Node/worker", {})
    decision = decide(bundle, 15, lambda _: {
        "level": suggested, "rationale": "classifier result", "confidence": 1.0,
    }, signal_reasons=signals)
    assert decision.level == "HIGH_RISK"
    assert decision.workflow == "high_risk_group"


def test_router_llm_failure_keeps_rules():
    def fail(prompt):
        raise RuntimeError("offline")
    d = decide(family1(), 15, fail)
    assert d.level == "HIGH_RISK" and d.decided_by == "router_failure"
    assert d.policy and d.policy["handoff_only"]


OUT = {"summary": "bad image", "root_cause_candidates": [{"cause": "bad tag", "confidence": 0.9,
        "evidence_refs": ["E1", "R1"]}], "counter_evidence": [], "confidence": 0.9,
       "next_action": "rollout_undo", "recommended_steps": ["inspect revision"]}
STATE = {"incident": {"id": 1, "workload": "inventory"}, "features": {}, "evidence": {}, "rag": [{"ref": "R1", "source": "internal-runbook",
         "doc": "image-pull-crashloop.md", "score": 0.9, "content": "rollback"}],
         "has_previous_revision": True, "min_confidence": 0.6}


def test_simple_graph_two_agents_agree_and_cite_rag():
    calls = []
    def run(role, prompt):
        calls.append(role)
        assert "source=internal-runbook" in prompt
        return AgentCall(True, OUT, len(calls))
    result = build_simple_graph(run).invoke(STATE)
    assert calls == ["signal_analyst", "result_verifier"]
    assert result["outcome"] == "propose_action"


@pytest.mark.parametrize("output,previous", [(OUT, False), ({**OUT, "confidence": 0.4}, True)])
def test_simple_graph_requires_revision_and_confidence(output, previous):
    result = build_simple_graph(lambda role, prompt: AgentCall(True, output, 1)).invoke(
        {**STATE, "has_previous_revision": previous})
    assert result["outcome"] == "handoff"


def test_simple_graph_agent_error_hands_off_without_verifier():
    roles = []
    def fail(role, prompt):
        roles.append(role)
        return AgentCall(False, None, 1, "invalid_output after retry")
    result = build_simple_graph(fail).invoke(STATE)
    assert result["outcome"] == "handoff" and roles == ["signal_analyst"]


def test_high_risk_graph_always_hands_off_even_if_agent_proposes_action():
    roles = []
    def run(role, prompt):
        roles.append(role)
        return AgentCall(True, OUT, len(roles))
    result = build_high_risk_graph(run).invoke(STATE)
    assert result["outcome"] == "handoff"
    assert roles == ["evidence_curator", "safety_verifier"]
    assert len(result["handoff"]["root_cause_candidates"]) <= 3


def test_high_risk_provider_error_still_hands_off():
    result = build_high_risk_graph(lambda role, prompt: AgentCall(False, None, 1, "provider offline")).invoke(STATE)
    assert result["outcome"] == "handoff"
    assert "provider offline" in result["reason"]
