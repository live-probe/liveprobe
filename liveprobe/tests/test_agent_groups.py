"""KAN-53: NORMAL/COMPLEX Agents Groups, citation checker, escalation, early stop and limits (fake agents)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from app.agent_groups import (
    GROUP_SCHEMA, EvidenceStore, GroupController, GroupMessage, check_citations, run_group, scrub_incident,
)
from app.config import AppConfig, RoleModel, WorkflowPolicy
from app.llm.base import strict_schema
from app.workflows import AgentCall

SUMMARY = {
    "note": "untrusted",
    "E1_deployment": {"name": "inventory", "replicas": 1, "ready_replicas": 0,
                      "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}]},
    "E2_pods": [{"name": "inventory-abc", "containers": [{"name": "inventory", "ready": False,
                 "last_terminated_reason": "OOMKilled", "exit_code": 137, "restart_count": 4}]}],
    "E3_service": {"name": "inventory", "ready_endpoints": 0},
    "E4_callers": [{"name": "order", "http_5xx_rate_per_s": 0.4}],
    "E5_recent_events": [{"reason": "BackOff", "message": "Back-off restarting failed container inventory"}],
    "E6_metrics": {"restarts": {"inventory-abc": 4}},
    "E7_error_logs": ["[order] upstream inventory connection refused"],
    "collection_errors": [],
}
RAG = [{"ref": "R1", "chunk_id": 1, "source": "internal-runbook", "doc": "oom.md", "score": 0.9,
        "content": "When a container is OOMKilled with exit code 137, raise the memory limit or fix the leak."}]


def good(**over):
    msg = {
        "summary": "inventory container is OOMKilled repeatedly",
        "evidence": [{"ref": "E2_pods", "quote": '"last_terminated_reason": "OOMKilled"', "supports": "c0"},
                     {"ref": "R1", "quote": "OOMKilled with exit code 137", "supports": "c0"}],
        "root_cause_candidates": [{"cause": "memory limit too low for workload", "confidence": 0.9,
                                   "evidence_refs": ["E2_pods", "R1"]}],
        "counter_evidence": [], "confidence": 0.9, "verdict": "not_applicable", "candidate_verdicts": [],
        "next_action": "handoff", "recommended_steps": ["inspect memory usage"],
    }
    msg.update(over)
    return msg


def verifier(verdict="supported", idx_verdict="supported"):
    return good(verdict=verdict, candidate_verdicts=[{"index": 0, "verdict": idx_verdict, "reason": "checked E2"}],
                root_cause_candidates=[])


class Fake:
    """Scripted per-role outputs; records (role, model, effort) of every call."""

    def __init__(self, script: dict, cost: float | None = 0.01, delay: float = 0.0):
        self.script = {k: list(v) if isinstance(v, list) else [v] for k, v in script.items()}
        self.calls: list[tuple] = []
        self.prompts: list[str] = []
        self.cost = cost
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, role, prompt, rm: RoleModel) -> AgentCall:
        with self.lock:
            self.calls.append((role, rm.model, rm.effort))
            self.prompts.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            seq = self.script.get(role) or [good()]
            out = seq.pop(0) if len(seq) > 1 else seq[0]
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        if isinstance(out, Exception):
            return AgentCall(False, None, None, str(out), error_kind=getattr(out, "kind", "provider"))
        GroupMessage.model_validate(out)
        return AgentCall(True, out, len(self.calls), cost_usd=self.cost, model=rm.model, effort=rm.effort,
                         input_tokens=1000, output_tokens=100)


class Err(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def run(level, fake, cfg=None, clock=None, primary=None):
    cfg = cfg or AppConfig()
    events = []
    ctl = GroupController(level, cfg, fake, lambda k, d: events.append(d), clock=clock or time.monotonic,
                          primary_model=primary)
    store = EvidenceStore(SUMMARY, RAG)
    res = run_group(level, ctl, store, {"id": 1, "workload": "inventory", "family_hint": "oom"}, {"oom_killed": True})
    return res, events, ctl


def kinds(events):
    return [e["kind"] for e in events]


# ------------------------------------------------------------------ message contract / schema
def test_group_schema_is_strict_and_matches_model():
    s = strict_schema(GROUP_SCHEMA)
    assert s["additionalProperties"] is False and set(s["required"]) == set(GroupMessage.model_fields)
    GroupMessage.model_validate(good())
    with pytest.raises(Exception):
        GroupMessage.model_validate(good(root_cause_candidates=good()["root_cause_candidates"] * 4))


def test_hidden_labels_never_reach_prompts():
    fake = Fake({})
    run("NORMAL", fake)
    assert fake.prompts and not any("family_hint" in p or '"oom"' in p for p in fake.prompts)
    assert scrub_incident({"id": 1, "family_hint": "x", "expected_level": "y", "workload": "w"}) == {"id": 1, "workload": "w"}


# ------------------------------------------------------------------ citation checker
def test_citation_checker_accepts_verbatim_and_rejects_forged_or_unknown():
    store = EvidenceStore(SUMMARY, RAG)
    rep = check_citations(store, good())
    assert rep.clean and rep.grounded == [0] and len(rep.verified_quotes) == 2
    forged = good(evidence=[{"ref": "E2_pods", "quote": "exit code 999 from kernel panic", "supports": "c0"}])
    rep = check_citations(store, forged)
    assert rep.forged_quotes and rep.ungrounded == [0]
    unknown = good(root_cause_candidates=[{"cause": "x", "confidence": 0.9, "evidence_refs": ["E9_secret"]}])
    rep = check_citations(store, unknown)
    assert "E9_secret" in rep.unknown_refs and rep.ungrounded == [0]
    rag_only = good(root_cause_candidates=[{"cause": "x", "confidence": 0.9, "evidence_refs": ["R1"]}])
    assert check_citations(store, rag_only).ungrounded == [0]  # runbook alone is not incident evidence


def test_evidence_views_are_cached_and_reused():
    store = EvidenceStore(SUMMARY, RAG)
    a = store.view(("E1", "E2"))
    assert store.view(("E1", "E2")) is a and store.hits == 1 and store.misses == 1


# ------------------------------------------------------------------ NORMAL
def test_normal_parallel_readers_early_stop_and_verified_handoff():
    fake = Fake({"group_verifier": verifier()}, delay=0.2)
    res, events, ctl = run("NORMAL", fake)
    roles = [c[0] for c in fake.calls]
    assert set(roles[:2]) == {"k8s_analyst", "log_summarizer"} and fake.max_active == 2  # parallel
    assert "coordinator" not in roles  # early stop: confident grounded analyst
    assert roles[-1] == "group_verifier"
    assert res["status"] == "verified" and 1 <= len(res["root_cause_candidates"]) <= 3
    assert res["root_cause_candidates"][0]["verified_quotes"]
    ks = kinds(events)
    assert ks[0] == "group_started" and "early_stop" in ks and ks[-1] == "group_finished"
    assert res["cache"]["view_builds"] >= 1
    # OOM is not fixed by a rollback: the group verified it but must still hand off (policy 2026-10-05)
    assert res["action"]["propose"] is False and any("operator handoff" in n for n in res["notes"])


def test_normal_runs_coordinator_when_not_confident_and_uses_group_models():
    fake = Fake({"k8s_analyst": good(confidence=0.7), "group_verifier": verifier()})
    res, events, _ = run("NORMAL", fake)
    by_role = {c[0]: (c[1], c[2]) for c in fake.calls}
    assert by_role["k8s_analyst"] == ("gpt-6-astra", "high")
    assert by_role["log_summarizer"] == ("gpt-6-luna", "low")
    assert by_role["coordinator"] == ("gpt-6.1-sol", "medium")
    assert res["primary_role"] == "coordinator" and res["status"] == "verified"


def test_router_selected_model_applies_to_primary_role_only():
    fake = Fake({"group_verifier": verifier()})
    run("NORMAL", fake, primary="gpt-6.1-sol")
    by_role = {c[0]: c[1] for c in fake.calls}
    assert by_role["k8s_analyst"] == "gpt-6.1-sol" and by_role["group_verifier"] == "gpt-6.1-sol"
    assert by_role["log_summarizer"] == "gpt-6-luna"


def test_forged_citation_escalates_only_that_agent():
    forged = good(evidence=[{"ref": "E2_pods", "quote": "invented kernel panic", "supports": "c0"}])
    fake = Fake({"log_summarizer": [forged, good()], "group_verifier": verifier()})
    res, events, ctl = run("NORMAL", fake)
    esc = [e for e in events if e["kind"] == "escalated"]
    assert len(esc) == 1 and esc[0]["role"] == "log_summarizer"
    assert (esc[0]["from_model"], esc[0]["to_model"]) == ("gpt-6-luna", "gpt-6.1-sol")
    assert [c for c in fake.calls if c[0] == "k8s_analyst"] == [("k8s_analyst", "gpt-6-astra", "high")]
    assert res["status"] == "verified"


def test_top_of_ladder_escalates_effort_then_records_unavailable():
    ungrounded = good(evidence=[], confidence=0.9)
    fake = Fake({"k8s_analyst": ungrounded, "coordinator": ungrounded, "group_verifier": verifier()})
    res, events, _ = run("NORMAL", fake)
    esc = [e for e in events if e["kind"] == "escalated" and e["role"] == "k8s_analyst"]
    assert esc and esc[0]["to_model"] == "gpt-6-astra" and esc[0]["to_effort"] == "xhigh"
    assert any(e["kind"] == "escalation_unresolved" for e in events)
    assert res["status"] != "verified" and res["root_cause_candidates"] == []


def test_verifier_contradiction_triggers_one_revision_then_handoff():
    fake = Fake({"k8s_analyst": good(confidence=0.7),
                 "group_verifier": [verifier("contradicted", "contradicted"), verifier("contradicted", "contradicted")]})
    res, events, _ = run("NORMAL", fake)
    roles = [c[0] for c in fake.calls]
    assert roles.count("coordinator") == 2 and roles.count("group_verifier") == 2
    assert kinds(events).count("contradiction") == 2
    esc = [e for e in events if e["kind"] == "escalated" and e["role"] == "coordinator"]
    assert esc and esc[0]["reason"] == "conclusion contradicted by reviewer"
    assert res["status"] == "unverified" and res["root_cause_candidates"] == []
    assert any("unresolved contradiction" in n for n in res["notes"])


def test_provider_timeout_does_not_escalate_and_hands_off():
    fake = Fake({"k8s_analyst": Err("timeout"), "log_summarizer": Err("timeout"), "coordinator": Err("timeout")})
    res, events, _ = run("NORMAL", fake)
    assert not any(e["kind"] == "escalated" for e in events)
    assert "agent_failed" in kinds(events)
    assert res["status"] in ("failed", "unverified")


# ------------------------------------------------------------------ COMPLEX
def test_complex_sequence_parallel_challenge_and_verify():
    fake = Fake({"challenger": verifier("supported"), "group_verifier": verifier()}, delay=0.15)
    res, events, _ = run("COMPLEX", fake)
    roles = [c[0] for c in fake.calls]
    assert roles[:2] == ["k8s_log_analyst", "cause_reasoner"]
    assert set(roles[2:4]) == {"challenger", "recovery_planner"} and fake.max_active == 2
    assert roles[-1] == "group_verifier" and len(roles) == 5
    assert any(e["kind"] == "early_stop" and e["stage"] == "challenge" for e in events)
    assert res["status"] == "verified" and res["recommended_steps"]
    models = {c[0]: c[1] for c in fake.calls}
    assert models["cause_reasoner"] == "gpt-6-astra" and models["challenger"] == "gpt-6-astra"
    assert models["recovery_planner"] == "gpt-6.1-sol"


def test_complex_challenger_contradiction_revises_reasoner_once_escalated():
    fake = Fake({"challenger": verifier("contradicted", "contradicted"), "group_verifier": verifier()})
    res, events, _ = run("COMPLEX", fake)
    roles = [c[0] for c in fake.calls]
    assert roles.count("cause_reasoner") == 2
    rev = [c for c in fake.calls if c[0] == "cause_reasoner"][1]
    assert rev[2] == "xhigh"  # Sol/high -> Sol/xhigh for the contradicted role only
    assert "contradiction" in kinds(events) and res["status"] == "verified"


# ------------------------------------------------------------------ limits
def test_loop_limit_hands_off_before_extra_call():
    cfg = AppConfig(workflows=WorkflowPolicy(max_agent_calls={"NORMAL": 2, "COMPLEX": 10}))
    fake = Fake({"k8s_analyst": good(confidence=0.7)})
    res, events, _ = run("NORMAL", fake, cfg=cfg)
    assert len(fake.calls) == 2
    assert res["status"] == "stopped" and res["stop"][0]["kind"] == "loop"
    assert any(e["kind"] == "budget_exceeded" and e["limit"] == "loop" for e in events)


def test_cost_limit_is_checked_before_the_call():
    cfg = AppConfig(workflows=WorkflowPolicy(max_cost_usd={"NORMAL": 0.05, "COMPLEX": 0.8}))
    fake = Fake({})
    res, events, ctl = run("NORMAL", fake, cfg=cfg)
    assert res["status"] == "stopped" and res["stop"][0]["kind"] == "cost"
    assert ctl.spent <= 0.05  # never crossed
    assert not any(c[0] == "group_verifier" for c in fake.calls)


def test_unknown_price_refuses_call():
    cfg = AppConfig()
    cfg.llm.prices.pop("gpt-6-astra")
    fake = Fake({})
    res, events, _ = run("COMPLEX", fake, cfg=cfg)
    assert res["status"] == "stopped" and res["stop"][0]["kind"] == "cost_unknown"
    assert all(c[1] != "gpt-6-astra" for c in fake.calls)


def test_missing_usage_is_charged_conservatively_not_zero():
    fake = Fake({"group_verifier": verifier()}, cost=None)
    res, events, ctl = run("NORMAL", fake)
    assert ctl.spent > 0 and "cost_projected" in kinds(events)


def test_deadline_limit_hands_off():
    t = [0.0]
    def clock():
        t[0] += 400
        return t[0]
    fake = Fake({})
    res, events, _ = run("COMPLEX", fake, clock=clock)
    assert res["status"] == "stopped" and res["stop"][0]["kind"] == "deadline"


def test_policy_requires_positive_limits_for_both_levels():
    with pytest.raises(Exception):
        WorkflowPolicy(max_agent_calls={"NORMAL": 0, "COMPLEX": 3})
    with pytest.raises(Exception):
        WorkflowPolicy(max_cost_usd={"NORMAL": 1.0})


def test_results_are_json_serialisable():
    fake = Fake({"group_verifier": verifier()})
    res, events, _ = run("COMPLEX", fake)
    json.dumps(res), json.dumps(events)


# ------------------------------------------------------------------ engine integration (DB)
from conftest import needs_db  # noqa: E402


class FakeProvider:
    """Provider fake at the LLM boundary: returns scripted GroupMessage JSON and usage."""
    name = "fake"

    def __init__(self, by_role):
        self.by_role = by_role
        self.calls = []

    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMResult, Usage
        self.calls.append((role, model, effort))
        if role == "router":
            data = {"level": "NORMAL", "rationale": "oom", "confidence": 0.9}
        else:
            data = self.by_role.get(role, good())
        return LLMResult(text=json.dumps(data), data=data, usage=Usage(input_tokens=16000, output_tokens=300),
                         duration_ms=5, provider="fake", model=model, effort=effort)


@needs_db
@pytest.mark.parametrize("level_sig,expected", [("OOMKilled", "NORMAL"), ("", "COMPLEX")])
def test_engine_runs_group_persists_events_runs_and_handoff(db_url, monkeypatch, level_sig, expected):
    from sqlalchemy import select
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.evidence import Bundle, EvidenceItem
    from app.models import Action, AgentRun, AuditLog, Incident
    b = Bundle("liveprobe-target", "inventory")
    b.pods = [{"name": "inventory-abc", "containers": [{"name": "inventory", "ready": False,
               "waiting_reason": "CrashLoopBackOff" if level_sig else None,
               "last_terminated_reason": "OOMKilled" if level_sig else None, "exit_code": 137}]}]
    b.deployment = {"name": "inventory", "current_revision": 3, "revisions": []}
    b.items = [EvidenceItem("deployment", "inventory", b.deployment), EvidenceItem("pod_status", "inventory", b.pods)]
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: b)
    msg = good(evidence=[{"ref": "E2_pods", "quote": '"exit_code": 137', "supports": "c0"}],
               root_cause_candidates=[{"cause": "memory", "confidence": 0.9, "evidence_refs": ["E2_pods"]}])
    prov = FakeProvider({r: msg for r in ("k8s_analyst", "log_summarizer", "coordinator", "k8s_log_analyst",
                                           "cause_reasoner", "recovery_planner")}
                        | {"group_verifier": {**verifier(), "evidence": msg["evidence"]},
                           "challenger": {**verifier(), "evidence": msg["evidence"]}})
    eng = Engine(AppConfig(), Settings(), None, prov, None)
    with db.session() as s:
        inc = Incident(fingerprint=f"kan53-{expected}", namespace="liveprobe-target", workload="inventory",
                       title="t", status="open", signal_reasons=[level_sig] if level_sig else ["Unknown"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.severity_level == expected and inc.status == "handed_off"
        assert inc.handoff["group"] == f"{expected.lower()}_group"
        assert inc.handoff["workflow_status"] == "verified", inc.handoff
        assert 1 <= len(inc.handoff["root_cause_candidates"]) <= 3
        events = [a.event for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == iid))]
        assert "wf_group_started" in events and "wf_group_finished" in events and "wf_early_stop" in events
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid, AgentRun.role != "router")))
        assert runs and all(r.estimated_cost_usd and r.reasoning_effort for r in runs)
        assert s.scalar(select(Action).where(Action.incident_id == iid)) is None


def _simple_bundle():
    from app.evidence import Bundle, EvidenceItem
    from test_router_workflows import family1
    b = family1()
    b.items = [EvidenceItem("deployment", "inventory", b.deployment), EvidenceItem("pod_status", "inventory", b.pods)]
    return b


@needs_db
def test_simple_forged_ref_withholds_action(db_url, monkeypatch):
    from sqlalchemy import select
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.models import Action, AuditLog, Incident
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: _simple_bundle())
    forged = {"summary": "bad image", "root_cause_candidates": [{"cause": "bad tag", "confidence": 0.95,
              "evidence_refs": ["E9_invented"]}], "counter_evidence": [], "confidence": 0.95,
              "next_action": "rollout_undo", "recommended_steps": [], "action": None}

    class P(FakeProvider):
        def complete(self, role, prompt, schema, model, effort, timeout):
            from app.llm.base import LLMResult, Usage
            data = {"level": "SIMPLE", "rationale": "r", "confidence": 0.9} if role == "router" else forged
            return LLMResult(text="", data=data, usage=Usage(input_tokens=10, output_tokens=5), duration_ms=1,
                             provider="fake", model=model, effort=effort)
    eng = Engine(AppConfig(), Settings(), None, P({}), None)
    with db.session() as s:
        inc = Incident(fingerprint="kan53-simple", namespace="liveprobe-target", workload="inventory", title="t",
                       status="open", signal_reasons=["ImagePullBackOff"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.severity_level == "SIMPLE" and inc.status == "handed_off"
        assert "cited evidence refs do not exist" in inc.status_reason
        assert s.scalar(select(Action).where(Action.incident_id == iid)) is None
        ev = s.scalar(select(AuditLog).where(AuditLog.incident_id == iid, AuditLog.event == "wf_citation_check"))
        assert ev is not None and ev.detail["unknown_refs"] == ["E9_invented"]


@needs_db
def test_engine_group_budget_stop_hands_off_with_event(db_url, monkeypatch):
    from sqlalchemy import select
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.models import AuditLog, Incident
    b = _simple_bundle()
    b.pods[0]["containers"][0]["waiting_reason"] = "CrashLoopBackOff"
    b.deployment["revisions"] = []
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: b)
    cfg = AppConfig(workflows=WorkflowPolicy(max_cost_usd={"NORMAL": 0.01, "COMPLEX": 0.01}))
    prov = FakeProvider({})
    eng = Engine(cfg, Settings(), None, prov, None)
    with db.session() as s:
        inc = Incident(fingerprint="kan53-budget", namespace="liveprobe-target", workload="inventory", title="t",
                       status="open", signal_reasons=["TargetWorkloadOOMKilled"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status == "handed_off" and inc.handoff["workflow_status"] == "stopped"
        assert "limit reached (cost)" in inc.status_reason
        ev = s.scalar(select(AuditLog).where(AuditLog.incident_id == iid, AuditLog.event == "wf_budget_exceeded"))
        assert ev is not None and ev.detail["limit"] == "cost"
    roles = [c[0] for c in prov.calls]
    assert "k8s_analyst" not in roles and "group_verifier" not in roles  # refused before the call
    assert inc.handoff["budget"]["estimated_cost_usd"] <= 0.01
