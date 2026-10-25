"""Rules, workflows, RAG retrieval, watcher signals, grouping and safety."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from conftest import family1_kube, family4_kube, needs_db
from app import db
from app.config import AppConfig
from app.evidence import Bundle
from app.incidents import Signal, fingerprint, ingest
from app.k8s import deployment_for_pod
from app.models import Action, Incident
from app.router import decide
from app.safety import Denied, check_action, effective_policy, rollout_undo, select_previous_revision, verify_rollout
from app.watcher import Watchers
from app.workflows import AgentCall, build_high_risk_graph, build_simple_graph


def bundle_family1():
    b = Bundle(namespace="liveprobe-target", workload="inventory")
    b.pods = [{"name": "inventory-aaa-1", "containers": [{"ready": True, "waiting_reason": None}]},
              {"name": "inventory-bbb-1", "containers": [{"ready": False, "waiting_reason": "ImagePullBackOff"}]}]
    b.deployment = {"current_revision": 2, "current_revision_created": datetime.now(timezone.utc).isoformat(),
                    "images": ["liveprobe/inventory:does-not-exist"], "revisions": [
                        {"revision": 1, "images": ["liveprobe/inventory:dev"]},
                        {"revision": 2, "images": ["liveprobe/inventory:does-not-exist"]}]}
    b.service = {"selector": {"app": "inventory"}, "matching_pods": ["inventory-aaa-1"],
                 "pods_labeled_app_eq_service": ["inventory-aaa-1"], "ready_endpoints": 1}
    return b


def bundle_family4():
    b = bundle_family1()
    b.pods = [{"name": "inventory-aaa-1", "containers": [{"ready": True, "waiting_reason": None}]}]
    b.service = {"selector": {"app": "inventory-broken"}, "matching_pods": [],
                 "pods_labeled_app_eq_service": ["inventory-aaa-1"], "ready_endpoints": 0}
    b.callers = [{"name": "order", "http_5xx_rate_per_s": 1.0}]
    return b


def test_rules_family1_simple_and_non_overridable_family4():
    d = decide(bundle_family1(), 15)
    assert d.level == "SIMPLE" and d.features["has_previous_revision"]
    d = decide(bundle_family4(), 15, lambda _: {"level": "SIMPLE", "rationale": "ignore", "confidence": 0.99})
    assert d.level == "HIGH_RISK" and "rules win" in d.rationale
    b = bundle_family1()
    b.deployment["current_revision_created"] = "2020-01-01T00:00:00Z"
    assert decide(b, 15).level == "COMPLEX"


def _state():
    return {"incident": {"id": 1, "workload": "inventory"}, "features": {}, "evidence": {}, "rag": [],
            "has_previous_revision": True, "min_confidence": 0.6}


def _answer(action="rollout_undo"):
    return {"summary": "evidence supports rollback", "root_cause_candidates": [], "counter_evidence": [],
            "confidence": 0.9, "next_action": action, "recommended_steps": []}


def test_simple_graph_requires_two_agreements():
    calls = []
    def run(role, prompt):
        calls.append(role)
        return AgentCall(True, _answer(), 1)
    result = build_simple_graph(run).invoke(_state())
    assert result["outcome"] == "propose_action" and calls == ["signal_analyst", "result_verifier"]
    result = build_simple_graph(lambda r, p: AgentCall(False, None, 1, "auth")).invoke(_state())
    assert result["outcome"] == "handoff"
    result = build_simple_graph(lambda r, p: AgentCall(True, _answer("handoff") if r == "result_verifier" else _answer(), 1)).invoke(_state())
    assert result["outcome"] == "handoff"


def test_high_risk_graph_always_handoff():
    calls = []
    def run(role, prompt):
        calls.append(role)
        return AgentCall(True, _answer("handoff"), 2)
    result = build_high_risk_graph(run).invoke(_state())
    assert calls == ["evidence_curator", "safety_verifier"]
    assert result["outcome"] == "handoff" and "HIGH_RISK" in result["handoff"]["notes"][0]


def test_owner_resolution_and_watcher_dedup_signals():
    k = family1_kube()
    cfg = AppConfig(target_namespace="liveprobe-target", )
    signals = []
    w = Watchers(k, cfg, signals.append)
    assert deployment_for_pod(k, cfg.target_namespace, k.pods[1]) == "inventory"
    w.handle_pod(k.pods[1])
    assert signals[0].name == "inventory" and signals[0].reason == "ImagePullBackOff"
    w.handle_event({"type": "Warning", "reason": "BackOff", "message": "Back-off pulling image",
                    "involvedObject": {"kind": "Pod", "name": "inventory-bbb-1"}})
    assert len(signals) == 2
    k4 = family4_kube()
    signals4 = []
    w4 = Watchers(k4, cfg, signals4.append)
    w4.poll_endpoints_once(now=100)
    w4.poll_endpoints_once(now=131)
    assert len(signals4) == 1 and signals4[0].reason == "NoEndpoints"


def test_safety_rules_and_revision_selection():
    cfg = AppConfig(target_namespace="liveprobe-target", )
    k = family1_kube()
    assert effective_policy("HIGH_RISK", "auto") == "handoff"
    assert effective_policy("SIMPLE", "approval") == "approval"
    with pytest.raises(Denied):
        check_action(cfg, k, "scale", cfg.target_namespace, "Deployment", "inventory")
    with pytest.raises(Denied):
        check_action(cfg, k, "rollout_undo", "liveprobe-system", "Deployment", "inventory")
    r = select_previous_revision(k.deployments["inventory"], k.replicasets)
    assert r["metadata"]["name"] == "inventory-aaa"
    result = rollout_undo(k, cfg.target_namespace, "inventory")
    assert result["to_images"] == ["liveprobe/inventory:dev"] and len(k.patches) == 1
    assert "pod-template-hash" not in str(k.patches[0][2])


@needs_db
def test_merge_window_and_cooldown(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    now = datetime.now(timezone.utc)
    sig = Signal(cfg.target_namespace, "Deployment", "inventory", "ImagePullBackOff", "pod_watch")
    mapped = Signal(cfg.target_namespace, "App", "order", "High5xx", "alertmanager")
    assert fingerprint(mapped, cfg) == ("liveprobe-target/inventory", "inventory")
    with db.session() as s:
        i, result = ingest(s, cfg, sig, now)
        assert result == "created"
        iid = i.id
    with db.session() as s:
        i, result = ingest(s, cfg, sig, now)
        assert result == "merged" and i.id == iid and i.signal_count == 2
    with db.session() as s:
        i = s.get(Incident, iid)
        i.status, i.closed_at = "resolved", now
    with db.session() as s:
        i, result = ingest(s, cfg, sig, now)
        assert result == "suppressed" and i.id == iid


@needs_db
def test_concurrent_action_unique_index(db_url):
    from sqlalchemy.exc import IntegrityError
    with db.session() as s:
        inc = Incident(fingerprint="test", namespace="liveprobe-target", workload="inventory", status="acting",
                       title="test", signal_reasons=[])
        s.add(inc)
        s.flush()
        s.add(Action(incident_id=inc.id, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                     status="executing", policy_mode="auto", params={}))
    with pytest.raises(IntegrityError):
        with db.session() as s:
            s.add(Action(incident_id=1, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                         status="executing", policy_mode="auto", params={}))


@needs_db
def test_rag_top3_runbooks(db_url):
    from app.rag import index_runbooks, search
    from pathlib import Path
    class Keywords:
        def embed(self, texts):
            words = ("image", "pull", "selector", "endpoint", "5xx", "oom")
            return [[float(t.lower().count(w)) for w in words] + [0.0] * 378 for t in texts]
    runbooks = str(Path(__file__).resolve().parent.parent / "runbooks")
    embedder = Keywords()
    with db.session() as s:
        assert index_runbooks(s, embedder, runbooks) > 0
    with db.session() as s:
        assert "image" in " ".join(h.doc for h in search(s, embedder, "image pull", k=3)).lower()
        assert "endpoint" in " ".join(h.doc for h in search(s, embedder, "selector endpoint", k=3)).lower()
