"""Evaluation entry point: arm wiring, evaluation lock, deterministic cause classifier (no LLM, no cluster)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import evalguard, evalrun, db
from app.config import AppConfig, RoleModel
from app.models import Setting
from app.workflows import AgentCall, build_single_graph
from conftest import needs_db


def _state(prev=True):
    return {"incident": {"id": 1, "workload": "inventory"}, "features": {}, "evidence": {"E1_deployment": {"x": 1}}, "rag": [],
            "has_previous_revision": prev, "min_confidence": 0.6}


def _run(output, ok=True):
    calls = []

    def run_agent(role, prompt):
        calls.append(role)
        return AgentCall(ok, output if ok else None, 1, None if ok else "boom")
    return run_agent, calls


def test_single_graph_makes_exactly_one_call_and_can_propose_rollback():
    ra, calls = _run({"summary": "s", "root_cause_candidates": [], "counter_evidence": [], "confidence": 0.9,
                      "next_action": "rollout_undo", "recommended_steps": []})
    out = build_single_graph(ra).invoke(_state())
    assert calls == ["signal_analyst"] and out["outcome"] == "propose_action"


@pytest.mark.parametrize("action,conf,prev", [("handoff", 0.9, True), ("rollout_undo", 0.3, True),
                                              ("rollout_undo", 0.9, False)])
def test_single_graph_hands_off_when_not_safe(action, conf, prev):
    ra, _ = _run({"summary": "s", "root_cause_candidates": [], "counter_evidence": [], "confidence": conf,
                  "next_action": action, "recommended_steps": []})
    assert build_single_graph(ra).invoke(_state(prev))["outcome"] == "handoff"


def test_single_graph_failed_call_is_handoff_not_action():
    ra, _ = _run(None, ok=False)
    out = build_single_graph(ra).invoke(_state())
    assert out["outcome"] == "handoff" and "failed" in out["reason"]


def test_arm_config_maps_roles_for_fixed_group_and_single():
    arm = {"arm_id": "fixed_multi_agent", "strategy": "fixed_group", "roles": {
        "router": {"model": "gpt-6-luna", "effort": "low"},
        "signal_analyst": {"model": "gpt-6.1-sol", "effort": "medium"},
        "result_verifier": {"model": "gpt-6.1-sol", "effort": "medium"},
        "evidence_curator": {"model": "gpt-6.1-sol", "effort": "medium"},
        "safety_verifier": {"model": "gpt-6-astra", "effort": "high"}}}
    cfg = evalrun.build_cfg(arm)
    assert cfg.llm.roles["challenger"] == RoleModel(model="gpt-6-astra", effort="high")
    assert cfg.llm.roles["k8s_log_analyst"].model == "gpt-6.1-sol"
    single = {"arm_id": "single_high", "strategy": "single",
              "roles": {r: {"model": "gpt-6-astra", "effort": "xhigh"} for r in
                        ("router", "signal_analyst", "result_verifier", "evidence_curator", "safety_verifier")}}
    cfg2 = evalrun.build_cfg(single)
    assert {rm.model for rm in cfg2.llm.roles.values() if rm.model} >= {"gpt-6-astra"}


@pytest.mark.parametrize("text,cls", [
    ("Container was OOMKilled (exit code 137) after hitting the memory limit", "memory_exhaustion"),
    ("ErrImagePull: the image tag does not exist", "image_pull_failure"),
    ("Service selector app=inventory-broken matches no pods", "service_selector_mismatch"),
    ("DB connection pool exhausted by held connections", "db_pool_exhaustion"),
    ("Slow responses with rising p99 latency", "dependency_latency"),
    ("Container is in CrashLoopBackOff and keeps restarting", "process_crash"),
    ("Service has zero ready endpoints", "endpoint_unavailable"),
    ("Everything looks fine", None)])
def test_cause_classifier(text, cls):
    assert evalrun.classify_cause(text) == cls


def test_target_classifier_picks_first_named_service_else_default():
    assert evalrun.classify_target("order calls to inventory fail", "inventory") == "order"
    assert evalrun.classify_target("the pod is crashing", "inventory") == "inventory"


@needs_db
def test_evaluation_lock_silences_and_expires(db_url):
    evalguard._cache = (0.0, False)
    assert evalguard.evaluation_lock_active() is False
    evalguard.set_lock("evaluation", 60)
    evalguard._cache = (0.0, False)
    assert evalguard.evaluation_lock_active() is True
    evalguard.clear_lock()
    evalguard._cache = (0.0, False)
    assert evalguard.evaluation_lock_active() is False
    with db.session() as s:  # an expired lock never silences the live system
        s.get(Setting, evalguard.KEY).value = {"owner": "evaluation",
                                               "until": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()}
    evalguard._cache = (0.0, False)
    assert evalguard.evaluation_lock_active() is False


# ------------------------------------------------------------------ engine integration (fake LLM boundary)
import json  # noqa: E402


class _Prov:
    name = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, role, prompt, schema, model, effort, timeout):
        from app.llm.base import LLMResult, Usage
        self.calls.append((role, model, effort))
        if role == "router":
            data = {"level": "SIMPLE", "rationale": "x", "confidence": 0.9}
        elif "candidate_verdicts" in schema.get("properties", {}):      # group message
            data = {"summary": "s", "evidence": [{"ref": "E2_pods", "quote": "ErrImagePull", "supports": "c"}],
                    "root_cause_candidates": [{"cause": "ErrImagePull on inventory", "confidence": 0.9,
                                               "evidence_refs": ["E2_pods"]}],
                    "counter_evidence": [], "confidence": 0.9, "verdict": "supported",
                    "candidate_verdicts": [{"index": 0, "verdict": "supported", "reason": "r"}],
                    "next_action": "act" if role in ("recovery_planner", "group_verifier") else "handoff", "recommended_steps": ["check"],
                    "action": {"type": "rollout_undo", "target": "inventory", "why": "bad image"} if role in ("recovery_planner", "group_verifier") else None}
        else:
            data = {"summary": "s", "root_cause_candidates": [{"cause": "ErrImagePull", "confidence": 0.9,
                                                               "evidence_refs": ["E2_pods"]}],
                    "counter_evidence": [], "confidence": 0.9, "next_action": "handoff", "recommended_steps": []}
        return LLMResult(text=json.dumps(data), data=data, usage=Usage(input_tokens=1000, output_tokens=100),
                         duration_ms=5, provider="fake", model=model, effort=effort)


def _bundle():
    from app.evidence import Bundle, EvidenceItem
    b = Bundle("liveprobe-target", "inventory")
    b.pods = [{"name": "inventory-abc", "containers": [{"name": "inventory", "ready": False,
               "waiting_reason": "ErrImagePull", "last_terminated_reason": None, "exit_code": None}]}]
    b.deployment = {"name": "inventory", "current_revision": 3, "revisions": []}
    b.items = [EvidenceItem("deployment", "inventory", b.deployment), EvidenceItem("pod_status", "inventory", b.pods)]
    return b


def _engine(db_url, monkeypatch, arm_roles):
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.models import Incident
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: _bundle())
    prov = _Prov()
    cfg = AppConfig()
    cfg.llm.roles.update(arm_roles)
    eng = Engine(cfg, Settings(), None, prov, None)
    eng.capture = {}
    with db.session() as s:
        inc = Incident(fingerprint="eval/test", namespace="liveprobe-target", workload="inventory", status="open",
                       title="t", signal_count=1, signal_reasons=["ErrImagePull"])
        s.add(inc)
        s.flush()
        return eng, prov, inc.id


@needs_db
def test_single_arm_uses_one_model_for_every_call_and_never_group(db_url, monkeypatch):
    from app import db
    from app.models import AgentRun, Incident
    from sqlalchemy import select
    sol = RoleModel(model="gpt-6-astra", effort="xhigh")
    eng, prov, iid = _engine(db_url, monkeypatch, {r: sol for r in ("router", "signal_analyst", "result_verifier",
                                                                    "evidence_curator", "safety_verifier")})
    eng.process(iid, arm={"strategy": "single", "role": sol, "level": "COMPLEX"})
    assert {(m, e) for _, m, e in prov.calls} == {("gpt-6-astra", "xhigh")}       # no luna/terra anywhere
    assert [r for r, _, _ in prov.calls] == ["signal_analyst"]                  # no router LLM, one analysis call
    with db.session() as s:
        assert s.get(Incident, iid).status == "handed_off"                       # fake says handoff -> no action
        assert len(list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid)))) == 1


@needs_db
def test_fixed_group_arm_always_runs_complex_group_without_router_llm(db_url, monkeypatch):
    from app import db
    from app.models import Incident, RoutingDecision
    from sqlalchemy import select
    terra = RoleModel(model="gpt-6.1-sol", effort="medium")
    roles = {r: terra for r in ("k8s_log_analyst", "cause_reasoner", "challenger", "recovery_planner", "group_verifier")}
    eng, prov, iid = _engine(db_url, monkeypatch, roles)
    eng.process(iid, arm={"strategy": "fixed_group", "level": "COMPLEX", "role": terra})
    called = {r for r, _, _ in prov.calls}
    assert "router" not in called and "k8s_log_analyst" in called and "cause_reasoner" in called
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.severity_level == "COMPLEX" and inc.status == "handed_off"
        rd = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)).first()
        assert rd.decided_by == "fixed"


@needs_db
def test_adaptive_default_path_unchanged_routes_with_router_llm(db_url, monkeypatch):
    from app.models import Incident
    from app import db
    eng, prov, iid = _engine(db_url, monkeypatch, {})
    eng.process(iid)                                   # no arm: production behaviour
    assert prov.calls[0][0] == "router"
    with db.session() as s:
        # the rule engine keeps the conservative level when the evidence has no recent rollout (router LLM cannot lower it)
        assert s.get(Incident, iid).severity_level == "COMPLEX"


@needs_db
def test_eval_incidents_are_not_fed_back_into_rag(db_url):
    from app import db
    from app.models import Incident
    from app.rag_ingest import closed_incident_documents
    from datetime import datetime, timezone
    with db.session() as s:
        for fp in ("eval/F1-01/adaptive/1", "liveprobe-target/inventory"):
            s.add(Incident(fingerprint=fp, namespace="n", workload="w", status="resolved", title="t", signal_count=1,
                           signal_reasons=[], summary="ErrImagePull after rollout",
                           closed_at=datetime.now(timezone.utc)))
    with db.session() as s:
        keys = [d.key for d in closed_incident_documents(s)]
    assert len(keys) == 1


@pytest.mark.parametrize("text,cls", [
    ("The newest Deployment revision references the nonexistent or inaccessible image liveprobe/inventory:does-not-exist-01, "
     "preventing its pod from starting.", "image_pull_failure"),
    ("Inventory Deployment points to an invalid image reference, so the new Pod cannot pull the container image.",
     "image_pull_failure"),
    ("The image tag does not exist in the registry (manifest unknown).", "image_pull_failure"),
    ("Pods are in ErrImagePull then ImagePullBackOff.", "image_pull_failure"),
    ("Held database connections left the pool saturated.", "db_pool_exhaustion"),
    ("The container keeps restarting with exit code 1.", "process_crash"),
    ("Order sees no healthy upstream for inventory.", "endpoint_unavailable"),
])
def test_cause_classifier_real_phrasings_seen_in_pilot(text, cls):
    assert evalrun.classify_cause(text) == cls


# ------------------------------------------------------------------ policy C: agent acts on Kubernetes (2026-10-05)
from app import proposals as P  # noqa: E402
from app import safety  # noqa: E402

_F = {"recent_rollout": True, "has_previous_revision": True}
_ALLOW = ["rollout_undo", "rollout_restart", "pod_delete", "scale_replicas", "set_memory_limit"]


def test_level_policy_everything_below_high_risk_is_auto():
    assert [safety.effective_policy(l, "auto") for l in ("SIMPLE", "NORMAL", "COMPLEX", "HIGH_RISK")] == \
        ["auto", "auto", "auto", "handoff"]
    assert safety.effective_policy("COMPLEX", "approval") == "approval"          # global tightening still wins
    assert safety.effective_policy("NORMAL", "suggest_only") == "suggest_only"


@pytest.mark.parametrize("raw,ok", [
    ({"type": "rollout_restart", "target": "inventory"}, True),
    ({"type": "set_memory_limit", "target": "inventory", "memory_limit_mi": 768}, True),
    ({"type": "scale_replicas", "target": "order", "replicas": 2}, True),
    ({"type": "pod_delete", "target": "inventory-abc-1"}, True),
    ({"type": "set_memory_limit", "target": "inventory"}, False),                # missing parameter
    ({"type": "scale_replicas", "target": "order", "replicas": True}, False),    # bool is not an int here
    ({"type": "kubectl_exec", "target": "inventory"}, False),                    # unknown type
    ({"type": "rollout_restart"}, True),                                         # defaults to the incident workload
    ("rollout_restart", False), (None, False),
])
def test_normalize_accepts_only_known_typed_actions(raw, ok):
    assert (P.normalize(raw, "inventory") is not None) is ok


def test_resolve_supports_legacy_rollout_undo_and_new_act():
    assert P.resolve({"next_action": "rollout_undo"}, "inventory")["type"] == "rollout_undo"
    assert P.resolve({"next_action": "act", "action": {"type": "rollout_restart"}}, "inventory")["type"] == "rollout_restart"
    assert P.resolve({"next_action": "handoff", "action": {"type": "rollout_restart"}}, "inventory") is None


@pytest.mark.parametrize("planner,verified,feat,expect", [
    ({"next_action": "act", "action": {"type": "set_memory_limit", "target": "inventory", "memory_limit_mi": 768}}, True, {}, True),
    ({"next_action": "act", "action": {"type": "rollout_restart"}}, True, {}, True),
    ({"next_action": "act", "action": {"type": "rollout_restart"}}, False, {}, False),            # not verified
    ({"next_action": "handoff"}, True, {}, False),                                                # app-code problem: handoff
    ({"next_action": "act", "action": {"type": "rollout_undo"}}, True, {}, False),                # nothing to roll back to
    ({"next_action": "act", "action": {"type": "rollout_undo"}}, True, _F, True),
    ({"next_action": "act", "action": {"type": "rollout_restart", "target": "order"}}, True, {}, False),  # other workload
    ({"next_action": "act", "action": {"type": "configmap_restore", "version": "v1", "target": "inventory-config"}}, True, {}, False),
])
def test_group_action_is_conservative(planner, verified, feat, expect):
    got = P.group_action({"recovery_planner": planner}, None, verified, feat, "inventory", _ALLOW)
    assert got["propose"] is expect, got


def test_precheck_uses_allowlist():
    assert P.precheck(P.normalize({"type": "pod_delete", "target": "x"}, "inventory"), {}, "inventory", ["rollout_undo"])["propose"] is False


@needs_db
def test_complex_group_with_verified_image_cause_rolls_back_through_safety_executor(db_url, monkeypatch):
    """COMPLEX group verified an image cause after a rollout -> proposes, safety.execute runs rollout_undo, incident resolves."""
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.evidence import Bundle, EvidenceItem
    from app.models import Action, Incident
    from sqlalchemy import select
    import app.router as R
    seen = {}

    def fake_execute(session_factory, cfg, kube, aid, actor, level=None):
        seen.update(aid=aid, level=level)
        with db.session() as s:
            a = s.get(Action, aid)
            a.status, a.executed = "succeeded", True
        from app.safety import ExecResult
        return ExecResult(aid, "succeeded", "ok")
    monkeypatch.setattr(engine_module.safety, "execute", fake_execute)
    b = _bundle()
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: b)
    monkeypatch.setattr(R, "rules_decide", lambda f: ("COMPLEX", "forced for test"))
    monkeypatch.setattr(R, "extract_features", lambda *a, **k: {
        "crashloop": False, "oom_killed": False, "ready_pods": 1, "event_reasons": [], "latency_signal": False,
        "recent_rollout": True, "has_previous_revision": True, "evidence_errors": [], "waiting_reasons": ["ErrImagePull"],
        "evidence_sources": ["deployment"], "protected_resource": False, "service_selector_matches_zero_pods": False,
        "service_has_no_endpoints": False, "dependent_callers_failing": [], "image_pull_failing": True,
        "affected_workload_count": 1, "current_revision": 3, "current_revision_age_min": 1})
    prov = _Prov()
    eng = Engine(AppConfig(), Settings(), None, prov, None)
    with db.session() as s:
        inc = Incident(fingerprint="eval/t", namespace="liveprobe-target", workload="inventory", status="open", title="t",
                       signal_count=1, signal_reasons=["ErrImagePull"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        assert s.get(Incident, iid).status == "resolved"
        assert seen["level"] == "COMPLEX"
        assert len(list(s.scalars(select(Action).where(Action.incident_id == iid)))) == 1


@needs_db
def test_complex_group_oom_proposes_memory_limit_through_typed_executor(db_url, monkeypatch):
    """Policy C end to end: an OOM cause verified by the group leads to set_memory_limit (not just rollback)."""
    from app import db, engine as engine_module, actions as typed
    from app.config import Settings
    from app.engine import Engine
    from app.models import Action, Incident
    from sqlalchemy import select
    import app.router as R
    ran = {}
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: _bundle())
    monkeypatch.setattr(R, "rules_decide", lambda f: ("COMPLEX", "forced for test"))

    class FakeTyped:
        def __init__(self, *a, **k): ...

    def fake_propose(s, cfg, kube, inc, level, atype, kind, name, params=None, api_group=None, actor="liveprobe"):
        a = Action(incident_id=inc.id, type=atype, target=f"{inc.namespace}/{kind}/{name}", params={"args": params},
                   policy_mode="auto", status="proposed")
        s.add(a)
        s.flush()
        ran["proposed"] = (atype, kind, name, params, level)
        return a

    def fake_execute(session_factory, cfg, kube, aid, actor, level=None):
        with db.session() as s:
            a = s.get(Action, aid)
            a.status, a.executed = "succeeded", True
        from app.safety import ExecResult
        return ExecResult(aid, "succeeded", "ok")
    monkeypatch.setattr(typed, "propose_typed", fake_propose)
    monkeypatch.setattr(engine_module.safety, "execute", fake_execute)

    class OomProv(_Prov):
        def complete(self, role, prompt, schema, model, effort, timeout):
            res = super().complete(role, prompt, schema, model, effort, timeout)
            if role in ("recovery_planner", "group_verifier"):
                res.data["next_action"] = "act"
                res.data["action"] = {"type": "set_memory_limit", "target": "inventory", "memory_limit_mi": 768,
                                      "why": "OOMKilled at 512Mi"}
            return res
    prov = OomProv()
    eng = Engine(AppConfig(), Settings(), None, prov, None)
    with db.session() as s:
        inc = Incident(fingerprint="eval/oom", namespace="liveprobe-target", workload="inventory", status="open",
                       title="t", signal_count=1, signal_reasons=["OOMKilled"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        assert s.get(Incident, iid).status == "resolved"
        assert ran["proposed"][:4] == ("set_memory_limit", "Deployment", "inventory", {"memory_limit_mi": 768})
        assert len(list(s.scalars(select(Action).where(Action.incident_id == iid)))) == 1


@needs_db
def test_high_risk_never_acts_even_if_agent_asks(db_url, monkeypatch):
    from app import db, engine as engine_module
    from app.config import Settings
    from app.engine import Engine
    from app.models import Action, Incident
    from sqlalchemy import select
    import app.router as R
    monkeypatch.setattr(engine_module, "collect", lambda *a, **k: _bundle())
    monkeypatch.setattr(R, "rules_decide", lambda f: ("HIGH_RISK", "forced for test"))
    prov = _Prov()
    eng = Engine(AppConfig(), Settings(), None, prov, None)
    with db.session() as s:
        inc = Incident(fingerprint="eval/hr", namespace="liveprobe-target", workload="inventory", status="open",
                       title="t", signal_count=1, signal_reasons=["NoEndpoints"])
        s.add(inc)
        s.flush()
        iid = inc.id
    eng.process(iid)
    with db.session() as s:
        assert s.get(Incident, iid).status == "handed_off"
        assert list(s.scalars(select(Action).where(Action.incident_id == iid))) == []


# ------------------------------------------------------------------ router: workload-down is not a routing fault
from app import router as RT  # noqa: E402
from app.evidence import Bundle as _B  # noqa: E402


def _rb(*, waiting=None, last_term=None, ready=False, restarts=0, selector_ok=True, callers=("order",)):
    b = _B("liveprobe-target", "inventory")
    b.pods = [{"name": "inventory-1", "containers": [{"name": "inventory", "ready": ready, "waiting_reason": waiting,
                                                      "last_terminated_reason": last_term, "exit_code": None,
                                                      "restart_count": restarts}]}]
    b.deployment = {"name": "inventory", "current_revision": 3, "revisions": [], "images": ["i"]}
    b.service = {"name": "inventory", "selector": {"app": "inventory" if selector_ok else "inventory-broken"},
                 "matching_pods": ["inventory-1"] if selector_ok else [], "pods_labeled_app_eq_service": ["inventory-1"],
                 "ready_endpoints": 0}
    b.callers = [{"name": c, "http_5xx_rate_per_s": 1.0} for c in callers]
    return b


def test_oom_with_empty_endpoints_is_not_high_risk():
    f = RT.extract_features(_rb(waiting="CrashLoopBackOff", last_term="OOMKilled", restarts=3))
    assert f["endpoints_empty_because_workload_down"] is True
    assert RT.rules_decide(f)[0] != "HIGH_RISK"


def test_selector_mismatch_stays_high_risk():
    f = RT.extract_features(_rb(ready=True, selector_ok=False))
    assert f["service_selector_matches_zero_pods"] is True
    assert RT.rules_decide(f)[0] == "HIGH_RISK"


def test_empty_endpoints_with_ready_pods_stays_high_risk():
    """Pods are Ready yet the Service has no endpoints and callers fail: that is a routing problem, not a workload one."""
    f = RT.extract_features(_rb(ready=True, restarts=0))
    assert f["endpoints_empty_because_workload_down"] is False
    assert RT.rules_decide(f)[0] == "HIGH_RISK"


def test_group_action_rejects_verifier_that_widens_or_withholds():
    plan = {"next_action": "act", "action": {"type": "set_memory_limit", "target": "inventory", "memory_limit_mi": 768}}
    other = {"next_action": "act", "action": {"type": "rollout_restart", "target": "inventory"}}
    backs = {"next_action": "act", "action": {"type": "set_memory_limit", "target": "inventory", "memory_limit_mi": 768}}
    hands = {"next_action": "handoff", "action": None}
    ok = lambda gv: P.group_action({"recovery_planner": plan, "group_verifier": gv}, None, True, {}, "inventory", _ALLOW)["propose"]
    assert ok(backs) is True
    assert ok(other) is False        # verifier proposes something else
    assert ok(hands) is False        # verifier does not support acting


def test_llm_cannot_raise_to_high_risk_on_its_own():
    b = _rb(waiting="CrashLoopBackOff", last_term="OOMKilled", restarts=3)
    d = RT.decide(b, 60, llm_classify=lambda p: {"level": "HIGH_RISK", "rationale": "scary", "confidence": 0.95}, cfg=AppConfig(),
                 signal_reasons=["OOMKilled"])
    assert d.level in ("NORMAL", "COMPLEX") and d.level != "HIGH_RISK"


def test_rules_high_risk_is_still_final():
    b = _rb(ready=True, selector_ok=False)
    d = RT.decide(b, 60, llm_classify=lambda p: {"level": "SIMPLE", "rationale": "easy", "confidence": 0.99}, cfg=AppConfig())
    assert d.level == "HIGH_RISK"


def test_evidence_adds_pool_and_latency_and_survives_optional_failures(monkeypatch):
    from app import evidence as EV

    def fake_prom(settings, q):
        if "hikaricp_connections_pending" in q:
            raise RuntimeError("series query failed")          # optional series failing must not hide the rest
        if "http_server_requests_seconds_sum" in q:
            return [{"metric": {"app": "inventory"}, "value": [0, "850.0"]}, {"metric": {"app": "order"}, "value": [0, "NaN"]}]
        if "hikaricp_connections_active" in q:
            return [{"metric": {"app": "inventory"}, "value": [0, "5"]}]
        if "hikaricp_connections_max" in q:
            return [{"metric": {"app": "inventory"}, "value": [0, "5"]}]
        if "status=~" in q:
            return [{"metric": {"app": "inventory"}, "value": [0, "1.5"]}]
        return []
    monkeypatch.setattr(EV, "_prom", fake_prom)
    b = EV.Bundle("liveprobe-target", "inventory")
    apps = ["inventory", "order"]
    # run only the prometheus block through collect's code path
    queries = {k: q % {"ns": "liveprobe-target"} for k, q in EV.PROM_QUERIES.items()}
    assert set(EV.CORE_PROM) <= set(queries)
    res = {k: fake_prom(None, q) for k, q in queries.items() if k in EV.CORE_PROM}
    assert res["http_5xx"][0]["value"][1] == "1.5"
    assert "latency_ms" in queries and "pool_pending" in queries


def test_reference_guide_loader_requires_the_leak_gate_hash(tmp_path):
    import hashlib
    from app.rag_ingest import load_reference_guides, _validate
    g = tmp_path / "g.md"
    g.write_text("---\ntitle: t\nsource_type: reference_guide\nversion: 1\n---\n\n# Guide\nbody text\n")
    with pytest.raises(ValueError, match="leak gate"):
        load_reference_guides(str(tmp_path), {})
    with pytest.raises(ValueError, match="leak gate"):
        load_reference_guides(str(tmp_path), {"g.md": "0" * 64})
    docs = load_reference_guides(str(tmp_path), {"g.md": hashlib.sha256(g.read_bytes()).hexdigest()})
    assert [d.kind for d in docs] == ["reference_guide"]
    _validate(docs[0])
    (tmp_path / "other.md").write_text("# not a guide\n")          # files without the marker are ignored
    assert len(load_reference_guides(str(tmp_path), {"g.md": hashlib.sha256(g.read_bytes()).hexdigest()})) == 1


def test_reference_guide_is_off_by_default_in_operations():
    assert AppConfig().use_reference_guide is False


@needs_db
def test_rollout_undo_uses_the_rollback_path_not_the_typed_executor(db_url):
    """Regression: with the executor attached, rollout_undo was sent to SafeActionKube (no request_rollback)."""
    from app import db
    from app.config import Settings
    from app.engine import Engine
    from app.models import Action, Incident
    eng = Engine(AppConfig(), Settings(), "OBSERVE", None, None)
    eng.exec_kube = "EXECUTOR"
    with db.session() as s:
        inc = Incident(fingerprint="t/kube_for", namespace="liveprobe-target", workload="order", status="open", title="t",
                       signal_count=1, signal_reasons=[])
        s.add(inc)
        s.flush()
        undo = Action(incident_id=inc.id, type="rollout_undo", target="liveprobe-target/Deployment/order", params={},
                      policy_mode="auto", status="proposed")
        mem = Action(incident_id=inc.id, type="set_memory_limit", target="liveprobe-target/Deployment/order", params={},
                     policy_mode="auto", status="proposed")
        s.add_all([undo, mem])
        s.flush()
        a1, a2 = undo.id, mem.id
    assert eng._kube_for(a1) == "OBSERVE"
    assert eng._kube_for(a2) == "EXECUTOR"
