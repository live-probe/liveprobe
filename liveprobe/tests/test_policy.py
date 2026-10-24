"""P7: policy profiles fail closed; HIGH_RISK stays held in assessment; ledger is redacted and CSV-safe."""
from __future__ import annotations

import pytest

from app import policy, safety
from conftest import needs_db


class K:
    def __init__(self, uid=None, err=None): self.uid, self.err = uid, err
    def get_namespace_uid(self, name):
        if self.err: raise self.err
        return self.uid


@pytest.fixture(autouse=True)
def _reset():
    safety.set_profile("assessment"); yield; safety.set_profile("assessment")


def test_assessment_is_default_and_never_widens():
    assert policy.resolve_profile("assessment", "x", K("x")).name == "assessment"


@pytest.mark.parametrize("req,pin,kube,why", [
    ("poc_autonomy", None, K("u"), "pinned"),
    ("poc_autonomy", "u", K("other"), "not the pinned"),
    ("poc_autonomy", "u", K(None), "not the pinned"),
    ("poc_autonomy", "u", K(err=RuntimeError("x")), "unreadable"),
    ("bogus", "u", K("u"), "unknown"),
])
def test_poc_autonomy_fails_closed(req, pin, kube, why):
    p = policy.resolve_profile(req, pin, kube)
    assert p.name == "assessment" and why in (p.reason or "")


def test_poc_autonomy_only_on_pinned_cluster():
    assert policy.resolve_profile("poc_autonomy", "u", K("u")).name == "poc_autonomy"


def test_assessment_keeps_high_risk_as_handoff_for_every_mode():
    for m in ("auto", "approval", "suggest_only"):
        assert safety.effective_policy("HIGH_RISK", m) == "handoff"
    assert not safety.high_risk_may_execute()


def test_poc_autonomy_high_risk_is_approval_never_auto():
    safety.set_profile("poc_autonomy")
    assert safety.effective_policy("HIGH_RISK", "auto") == "approval"
    assert safety.effective_policy("HIGH_RISK", "approval") == "approval"
    assert safety.effective_policy("HIGH_RISK", "suggest_only") == "suggest_only"
    assert safety.effective_policy("SIMPLE", "auto") == "auto"          # other levels unchanged


def test_unknown_profile_name_sets_assessment():
    safety.set_profile("whatever"); assert safety.active_profile() == "assessment"


def test_csv_neutralises_formulas():
    out = policy.to_csv([{"id": 1, "rationale": "=HYPERLINK(\"http://x\")", "impact": "+1", "workload": "ok"}])
    assert "'=HYPERLINK" in out and "'+1" in out and ",ok," in out


@needs_db
def test_ledger_records_redacts_and_lists(db_url):
    from app import db
    from app.models import Incident
    with db.session() as s:
        i = Incident(fingerprint="l", namespace="n", workload="w", status="analyzing", title="t", signal_reasons=[]); s.add(i); s.flush(); iid = i.id
    assert policy.record(iid, "high_risk_level", profile="assessment", level="HIGH_RISK", workload="w",
                         rationale="password=hunter2abc leaked", outcome="held_for_operator")
    assert policy.record(iid, "weird_code", profile="assessment", level=None, workload="w")   # unknown code is normalised
    r = policy.list_entries()
    assert r["total"] == 2 and r["by_reason"]["high_risk_level"] == 2
    assert "hunter2abc" not in str(r["items"])
    assert policy.list_entries(reason="x")["total"] == 0


@needs_db
def test_policy_api(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url); monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config
    config.get_settings.cache_clear(); config.get_app_config.cache_clear()
    from app.main import app
    with TestClient(app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        assert c.get("/api/policy/profile").json()["active"] == "assessment"
        assert c.get("/api/review-ledger").json()["total"] >= 0
        r = c.get("/api/review-ledger.csv")
        assert r.status_code == 200 and r.text.startswith("id,created_at")
    config.get_settings.cache_clear()
