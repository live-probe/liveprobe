"""KAN-52 four-level routing, conservative failure and durable scored reasons."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import db
from app.config import AppConfig
from app.evidence import Bundle, EvidenceItem
from app.models import Incident, RoutingDecision, Action
from app.router import decide, score_models
from conftest import needs_db
from test_router_workflows import family1, family4


def family2():
    b = Bundle("liveprobe-target", "inventory")
    b.pods = [{"name": "p", "containers": [{"waiting_reason": "OOMKilled", "ready": False}]}]
    b.deployment = {"current_revision": 1, "revisions": []}
    b.events = [{"reason": "OOMKilling"}]
    return b


def family3():
    b = Bundle("liveprobe-target", "inventory")
    b.pods = [{"name": "p", "containers": [{"waiting_reason": None, "ready": True}]}]
    b.deployment = {"current_revision": 1, "revisions": []}
    b.events = [{"reason": "HighLatency"}]
    return b


@pytest.mark.parametrize("factory,expected", [(family1, "SIMPLE"), (family2, "NORMAL"),
                                            (family3, "NORMAL"), (family4, "HIGH_RISK")])
def test_family_patterns_without_catalog_access(factory, expected):
    decision = decide(factory(), 15)
    assert decision.level == expected
    assert decision.workflow == expected.lower() + "_group"
    assert decision.policy and decision.policy["candidate_scores"]
    assert decision.policy["selected_model"]


def test_conflict_protected_resource_and_incomplete_evidence():
    b = family1()
    b.items.append(EvidenceItem("k8s_event", "liveprobe-target/Node/n", {}))
    assert decide(b, 15).level == "HIGH_RISK"
    b = family1()
    b.items.append(EvidenceItem("deployment", "inventory", error="unavailable"))
    d = decide(b, 15)
    assert d.decided_by == "router_failure" and d.level == "HIGH_RISK"
    assert d.features["evidence_errors"] == ["deployment"]


@pytest.mark.parametrize("reply", [{"level": "UNKNOWN", "rationale": "?", "confidence": .9},
                                   {"level": "SIMPLE", "rationale": "?", "confidence": 2}, None])
def test_bad_classifier_handoffs(reply):
    assert decide(family1(), 15, lambda _: reply).decided_by == "router_failure"


def test_score_constraints_and_determinism():
    cfg = AppConfig()
    a = score_models("NORMAL", cfg)
    assert a == score_models("NORMAL", cfg)
    assert all(c["quality"] >= cfg.router.min_quality["NORMAL"] for c in a["candidate_scores"] if c["eligible"])
    cfg.llm.prices = {}
    assert decide(family1(), 15, cfg=cfg).decided_by == "router_failure"
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"router": {"quality_weight": .8}})


def test_tier_selection_per_level():
    # low tier for SIMPLE, mid tier for NORMAL/COMPLEX, high tier only for HIGH_RISK (2026-10-18 lineup, router v2)
    cfg = AppConfig()
    picked = {lvl: score_models(lvl, cfg)["selected_model"] for lvl in ("SIMPLE", "NORMAL", "COMPLEX", "HIGH_RISK")}
    assert picked == {"SIMPLE": "gpt-6-luna", "NORMAL": "gpt-6.1-sol", "COMPLEX": "gpt-6.1-sol",
                      "HIGH_RISK": "gpt-6-astra"}
    # the log-scale cost term keeps the previous lineup's v1 choices
    old = AppConfig.model_validate({
        "llm": {"prices": {"gpt-6-luna": {"input": .1, "output": .5}, "gpt-5.6-terra": {"input": 2, "output": 12},
                           "gpt-6-sol": {"input": 2, "output": 10}}},
        "router": {"model_quality": {"gpt-6-luna": .5, "gpt-5.6-terra": .75, "gpt-6-sol": 1.0},
                   "model_throughput": {"gpt-6-luna": 1.0, "gpt-5.6-terra": .6, "gpt-6-sol": .3}}})
    assert [score_models(lvl, old)["selected_model"] for lvl in ("SIMPLE", "NORMAL", "HIGH_RISK")] == \
        ["gpt-6-luna", "gpt-6-sol", "gpt-6-sol"]


def test_live_alert_names_are_routing_inputs_not_truth_labels():
    b = family3()
    b.events = []
    d = decide(b, 15, signal_reasons=["TargetHighLatency"])
    assert d.level == "NORMAL" and d.features["signal_reasons"] == ["TargetHighLatency"]
    b = family2()
    b.events = []
    b.pods[0]["containers"][0]["waiting_reason"] = None
    d = decide(b, 15, signal_reasons=["TargetWorkloadOOMKilled"])
    assert d.level == "NORMAL" and d.features["oom_killed"]


@needs_db
def test_routing_policy_db_round_trip(db_url):
    d = decide(family2(), 15)
    with db.session() as s:
        inc = Incident(fingerprint="router-test", namespace="liveprobe-target", workload="inventory",
                       status="handed_off", title="test", signal_reasons=[], severity_level=d.level)
        s.add(inc)
        s.flush()
        iid = inc.id
        s.add(RoutingDecision(incident_id=iid, level=d.level, workflow=d.workflow, features=d.features,
                              rationale=d.rationale, policy=d.policy, confidence=d.confidence, decided_by=d.decided_by))
    with db.session() as s:
        stored = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
        assert stored is not None and stored.policy is not None
        assert stored.level == "NORMAL"
        assert stored.features["oom_killed"]
        assert stored.policy["selected_reason"] and stored.policy["version"] == "v2"
        assert stored.policy["selected_model"]


@needs_db
def test_engine_router_failure_persists_and_handoffs_without_action(db_url, monkeypatch):
    from app.config import Settings
    from app.engine import Engine
    from app import engine as engine_module
    b = family1()
    b.items = [EvidenceItem("deployment", "inventory", b.deployment),
               EvidenceItem("pod_status", "inventory", b.pods)]
    monkeypatch.setattr(engine_module, "collect", lambda *args, **kwargs: b)
    engine = Engine(AppConfig(), Settings(), None, None, None)
    monkeypatch.setattr(engine, "_classify", lambda *args: (_ for _ in ()).throw(RuntimeError("offline")))
    with db.session() as s:
        inc = Incident(fingerprint="router-failure", namespace="liveprobe-target", workload="inventory",
                       title="failure", status="open", signal_reasons=["ImagePullBackOff"])
        s.add(inc)
        s.flush()
        iid = inc.id
    engine.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        d = s.scalar(select(RoutingDecision).where(RoutingDecision.incident_id == iid))
        assert inc is not None and inc.status == "handed_off" and inc.severity_level == "HIGH_RISK"
        assert d is not None and d.policy is not None
        assert d.decided_by == "router_failure" and d.features["image_pull_failing"]
        assert d.policy["handoff_only"]
        assert s.scalar(select(Action).where(Action.incident_id == iid)) is None
