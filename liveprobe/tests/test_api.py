"""KAN-38: API auth, contract shapes, migration on empty DB, settings, SSE auth."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import needs_db


def test_host_allow_list():
    from app.auth import host_allowed
    from app.config import Settings
    s = Settings()
    assert host_allowed(s, "liveprobe.localhost") and host_allowed(s, "127.0.0.1:8080")
    assert host_allowed(s, "localhost:5173")
    assert not host_allowed(s, "evil.example") and not host_allowed(s, "") and not host_allowed(s, None)


@pytest.fixture()
def client(db_url, monkeypatch):
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_WEBHOOK_TOKEN", "hook-token-abc")
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    from app.main import app
    with TestClient(app, base_url="http://liveprobe.localhost",
                    headers={"X-LiveProbe-Client": "dashboard"}) as c:
        yield c
    config.get_settings.cache_clear()


def login(c):
    """No login exists any more; kept so older tests read the same."""
    return None


@needs_db
def test_health_open(client):
    assert client.get("/api/healthz").json() == {"status": "ok"}
    assert client.get("/api/readyz").status_code == 200


@needs_db
@pytest.mark.parametrize("path", ["/api/me", "/api/login", "/api/logout"])
def test_user_auth_endpoints_removed(client, path):
    assert client.get(path).status_code in (404, 405)
    assert client.post(path).status_code in (404, 405)


@needs_db
@pytest.mark.parametrize("path", ["/api/incidents", "/api/settings", "/api/docs", "/api/openapi.json"])
def test_open_without_login_on_local_host(client, path):
    assert client.get(path).status_code == 200


@needs_db
@pytest.mark.parametrize("path", ["/api/incidents", "/api/settings", "/api/stream"])
def test_foreign_host_rejected(client, path):
    assert client.get(path, headers={"Host": "evil.example"}).status_code == 403


@needs_db
def test_readiness_outage_recovery(client):
    from app import db
    from sqlalchemy import create_engine
    assert client.get("/api/readyz").status_code == 200
    original = db._engine
    unavailable = create_engine("postgresql+psycopg://test:test@127.0.0.1:1/lptest", connect_args={"connect_timeout": 1})
    db._engine = unavailable
    try:
        assert client.get("/api/readyz").status_code == 503
    finally:
        db._engine = original
        unavailable.dispose()
    assert client.get("/api/readyz").status_code == 200


@needs_db
def test_mutation_guard(client):
    body = {"automation_mode": "approval"}
    assert client.put("/api/settings", json=body, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.put("/api/settings", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/api/settings", json=body, headers={"X-LiveProbe-Client": ""}).status_code == 403
    assert client.put("/api/settings", json=body, headers={"Origin": "http://liveprobe.localhost"}).status_code == 200


@needs_db
def test_webhook_token_still_required(client):
    assert client.post("/api/hooks/alertmanager", json={}).status_code == 401


@needs_db
def test_incident_list_and_detail_shapes(client):
    from app import db
    from app.models import AgentRun, Incident
    with db.session() as s:
        inc = Incident(fingerprint="liveprobe-target/inventory", namespace="liveprobe-target", workload="inventory",
                       status="open", title="t", signal_reasons=["ImagePullBackOff"])
        s.add(inc)
        s.flush()
        s.add(AgentRun(incident_id=inc.id, role="signal_analyst", provider="codex_cli", model="m",
                       reasoning_effort="low", input_tokens=10, output_tokens=5, estimated_cost_usd=0.01,
                       duration_ms=100, status="succeeded", attempts=1, output={"summary": "x"}))
        iid = inc.id
    login(client)
    lst = client.get("/api/incidents").json()
    assert set(lst) == {"items", "total"} and lst["total"] == 1
    item = lst["items"][0]
    assert set(item) == {"id", "title", "namespace", "workload", "status", "severity_level", "signal_count",
                         "created_at", "updated_at", "closed_at", "total_cost_usd", "agent_run_count"}
    assert item["agent_run_count"] == 1 and item["total_cost_usd"] == 0.01
    assert item["created_at"].endswith("Z")
    assert client.get("/api/incidents?status=resolved").json()["total"] == 0
    d = client.get(f"/api/incidents/{iid}").json()
    assert set(d) == {"incident", "evidence", "routing", "agent_runs", "actions", "audit"}
    assert {"summary", "status_reason", "signal_reasons", "handoff", "family_hint"} <= set(d["incident"])
    assert d["agent_runs"][0]["provider"] == "codex_cli" and d["agent_runs"][0]["citations"] == []
    assert client.get("/api/incidents/99999").status_code == 404

@needs_db
def test_incident_search_and_unknown_estimated_cost(client):
    from app import db
    from app.models import AgentRun, Incident
    with db.session() as s:
        for title, workload, cost in (("OOM 100%", "inventory", None), ("Latency", "order", 0.02)):
            inc = Incident(fingerprint=title, namespace="liveprobe-target", workload=workload,
                           status="open", title=title, signal_reasons=[])
            s.add(inc)
            s.flush()
            s.add(AgentRun(incident_id=inc.id, role="router", provider="codex_cli", model="m",
                           status="succeeded", attempts=1, estimated_cost_usd=cost))
    login(client)
    matched = client.get("/api/incidents?search=inventory&status=open").json()
    assert matched["total"] == 1 and matched["items"][0]["total_cost_usd"] is None
    assert client.get("/api/incidents?search=100%25").json()["total"] == 1
    assert client.get("/api/incidents?search=%25").json()["total"] == 1  # literal %, not wildcard
    assert client.get("/api/incidents?search=order&status=resolved").json()["total"] == 0
    assert client.get("/api/incidents?search=order").json()["items"][0]["total_cost_usd"] == 0.02
    assert client.get("/api/incidents?search=" + "a" * 121).status_code == 422

@needs_db
def test_handoff_claim_is_guarded_idempotent_and_audited(client):
    from app import db
    from app.models import AuditLog, Incident
    from sqlalchemy import select
    with db.session() as s:
        handed = Incident(fingerprint="handoff", namespace="liveprobe-target", workload="inventory",
                          status="handed_off", title="handoff", handoff={"summary": "operator review"})
        open_inc = Incident(fingerprint="open", namespace="liveprobe-target", workload="order",
                            status="open", title="open")
        s.add_all([handed, open_inc])
        s.flush()
        hid, oid = handed.id, open_inc.id
    path = f"/api/incidents/{hid}/handoff/claim"
    assert client.post(path, headers={"X-LiveProbe-Client": ""}).status_code == 403
    assert client.post(path, headers={"Host": "evil.example"}).status_code == 403
    login(client)
    assert client.post(f"/api/incidents/{oid}/handoff/claim").status_code == 409
    assert client.post("/api/incidents/999999/handoff/claim").status_code == 404
    assert client.post(path, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    claimed = client.post(path)
    assert claimed.status_code == 200
    assert claimed.json()["claimed_by"] == "local-operator" and claimed.json()["claimed_at"].endswith("Z")
    assert client.post(path).json() == claimed.json()
    assert client.get(f"/api/incidents/{hid}").json()["incident"]["handoff"] == claimed.json()
    with db.session() as s:
        inc = s.get(Incident, hid)
        assert inc is not None and inc.status == "handed_off"
        assert [a.event for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == hid))] == ["handoff_claimed"]

@needs_db
def test_settings_roundtrip(client):
    login(client)
    assert client.get("/api/settings").json() == {"automation_mode": "auto"}
    assert client.put("/api/settings", json={"automation_mode": "approval"}).json() == {"automation_mode": "approval"}
    assert client.get("/api/settings").json() == {"automation_mode": "approval"}
    assert client.put("/api/settings", json={"automation_mode": "yolo"}).status_code == 422


@needs_db
def test_webhook_requires_bearer(client):
    assert client.post("/api/hooks/alertmanager", json={"alerts": []}).status_code == 401
    assert client.post("/api/hooks/alertmanager", json={"alerts": []},
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/hooks/alertmanager", json={"alerts": []},
                    headers={"Authorization": "Bearer hook-token-abc"})
    assert r.status_code == 200 and r.json() == {"accepted": 0}


@needs_db
def test_reject_and_approve_conflicts(client):
    from app import db
    from app.models import Action, Incident
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="liveprobe-target", workload="inventory", status="action_pending",
                       severity_level="COMPLEX", title="t", signal_reasons=[])
        s.add(inc)
        s.flush()
        a = Action(incident_id=inc.id, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                   params={}, policy_mode="approval", status="pending_approval")
        s.add(a)
        s.flush()
        aid = a.id
    login(client)
    r = client.post(f"/api/actions/{aid}/reject", json={"reason": "not now"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["executed"] is False
    assert client.post(f"/api/actions/{aid}/approve").status_code == 409


@needs_db
def test_unavailable_executor_does_not_audit_false_approval(client, monkeypatch):
    """Synthetic DB incident: 503 is not an operator approval or an execution."""
    from app import db
    from app.main import STATE
    from app.models import Action, AuditLog, Incident
    from sqlalchemy import select
    with db.session() as s:
        inc = Incident(fingerprint="executor-unavailable", namespace="liveprobe-target", workload="inventory",
                       status="action_pending", severity_level="SIMPLE", title="executor unavailable")
        s.add(inc)
        s.flush()
        act = Action(incident_id=inc.id, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                     params={}, policy_mode="approval", status="pending_approval")
        s.add(act)
        s.flush()
        iid, aid = inc.id, act.id
    login(client)
    monkeypatch.setitem(STATE, "engine", None)
    assert client.post(f"/api/actions/{aid}/approve").status_code == 503
    with db.session() as s:
        stored = s.get(Action, aid)
        assert stored is not None and stored.status == "pending_approval"
        assert list(s.scalars(select(AuditLog.event).where(AuditLog.incident_id == iid))) == []


@needs_db
def test_high_risk_approve_is_forbidden(client):
    from app import db
    from app.models import Action, Incident
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="liveprobe-target", workload="inventory", status="action_pending",
                       severity_level="HIGH_RISK", title="t", signal_reasons=[])
        s.add(inc)
        s.flush()
        a = Action(incident_id=inc.id, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                   params={}, policy_mode="approval", status="pending_approval")
        s.add(a)
        s.flush()
        aid = a.id
    login(client)
    r = client.post(f"/api/actions/{aid}/approve")
    assert r.status_code == 403


@needs_db
def test_citation_api_retains_original_provenance_after_refresh(client):
    from app import db
    from app.models import AgentRun, Citation, Incident, RagChunk
    from app.rag_ingest import Document, index_documents
    from sqlalchemy import select

    class Fixed:
        def embed(self, texts):
            return [[1.0] + [0.0] * 383 for _ in texts]

    def doc(uri, content):
        return Document(key="one.md", title="one", kind="runbook", source="internal-runbook",
                        source_uri=uri, license="internal", text=f"# One\n\n## Symptoms\n{content}\n")

    v1 = doc("repo://liveprobe/runbooks/one.md", "ImagePullBackOff")
    v2 = doc("repo://liveprobe/runbooks/one-v2.md", "OOMKilled")
    with db.session() as s:
        index_documents(s, Fixed(), [v1])
        ch = s.scalar(select(RagChunk).where(RagChunk.doc == "one.md", RagChunk.chunk_ix == 1))
        inc = Incident(fingerprint="x", namespace="liveprobe-target", workload="inventory", status="resolved",
                       title="x")
        s.add(inc); s.flush()
        run = AgentRun(incident_id=inc.id, role="analyst", provider="test", model="test", status="succeeded")
        s.add(run); s.flush()
        s.add(Citation(agent_run_id=run.id, chunk_id=ch.id, score=0.8))
        iid = inc.id
    with db.session() as s:
        index_documents(s, Fixed(), [v2])
    login(client)
    citation = client.get(f"/api/incidents/{iid}").json()["agent_runs"][0]["citations"][0]
    assert citation["source_uri"] == v1.source_uri
    assert citation["version_hash"] == v1.version_hash
    assert citation["locator"].startswith(v1.source_uri + "#L")
    assert citation["license"] == "internal"


@needs_db
def test_migration_idempotent(db_url):
    from app import db
    db.run_migrations(db_url)  # second run on migrated DB is a no-op
    from sqlalchemy import inspect
    names = set(inspect(db.engine()).get_table_names())
    assert {"incidents", "evidence", "routing_decisions", "agent_runs", "rag_chunks", "citations", "actions",
            "audit_log", "settings"} <= names
