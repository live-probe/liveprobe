"""P15 command HTTP boundary: real service/DB, fake Kubernetes/LLM at transport boundaries only."""
import json
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from test_p15_commands import MemoryKube, Planner, cm, operation


@pytest.fixture
def api(db_url, monkeypatch):
    from app import config, db, main
    from app.commands import CommandService
    from app.models import Incident
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with db.session() as s:
        i = Incident(fingerprint="command-api", namespace="sandbox", workload="settings", title="시험", status="handed_off")
        s.add(i)
        s.flush()
        iid = i.id
    class Writer:
        def __init__(self):
            self.calls = 0
        def dry_run(self, namespace, name, keys):
            return {"status": "succeeded", "keys": keys}
        def write(self, namespace, name, values, *, expected_keys):
            self.calls += 1
            assert set(values) == set(expected_keys)
            return {"status": "succeeded", "keys": expected_keys}
    p, k, w = Planner([operation()]), MemoryKube(cm()), Writer()
    service = CommandService(config.AppConfig(), p, k, secret_writer=w)
    with TestClient(main.app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        monkeypatch.setitem(main.STATE, "commands", service)
        monkeypatch.setitem(main.STATE, "secret_writer", w)
        yield N(client=c, iid=iid, service=service, planner=p, kube=k, writer=w)
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()


def plan(x, instruction="설정 mode를 new로 바꿔줘"):
    r = x.client.post(f"/api/incidents/{x.iid}/commands", json={"instruction": instruction})
    assert r.status_code == 201, r.text
    cmd = r.json()["command"]
    assert cmd["status"] == "planning"
    out = x.client.get(f"/api/incidents/{x.iid}/commands").json()["items"][-1]
    assert out["status"] == "awaiting_confirmation", out
    return out


def test_http_plan_confirm_execute_verify_and_rollback(api):
    x = api
    c = plan(x)
    assert c["confirm_question"] == "정말 진행하시겠습니까?"
    assert c["side_effects"]["targets"] == ["sandbox/ConfigMap/settings"]
    assert x.kube.get(operation())["data"]["mode"] == "old"
    assert x.client.post(f"/api/commands/{c['id']}/confirm", json={"plan_sha256": "0" * 64}).status_code == 409
    assert x.kube.get(operation())["data"]["mode"] == "old"
    r = x.client.post(f"/api/commands/{c['id']}/confirm", json={"plan_sha256": c["plan_sha256"]})
    assert r.status_code == 200, r.text
    got = x.service.get(c["id"])
    assert got["status"] == "succeeded" and got["verification"]["ok"] is True
    assert x.kube.get(operation())["data"]["mode"] == "new"
    r = x.client.post(f"/api/commands/{c['id']}/rollback", json={"plan_sha256": c["plan_sha256"]})
    assert r.status_code == 200 and r.json()["status"] == "rolled_back", r.text
    assert x.kube.get(operation())["data"]["mode"] == "old"


def test_http_cancel_and_stale_plan_never_execute(api):
    c = plan(api)
    r = api.client.post(f"/api/commands/{c['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert api.client.post(f"/api/commands/{c['id']}/confirm", json={"plan_sha256": c["plan_sha256"]}).status_code == 409
    assert api.kube.get(operation())["data"]["mode"] == "old"
    c = plan(api)
    obj = next(iter(api.kube.objects.values()))
    obj["metadata"]["resourceVersion"] = "99"
    obj.setdefault("data", {})["concurrent"] = "edit"
    r = api.client.post(f"/api/commands/{c['id']}/confirm", json={"plan_sha256": c["plan_sha256"]})
    assert r.status_code == 409 and r.json()["detail"] == "stale_plan"
    assert api.kube.get(operation())["data"]["mode"] == "old"


@pytest.mark.parametrize("status", ["analyzing", "resolved", "handed_off", "failed"])
def test_mutation_chat_creates_a_separate_confirmed_command_not_an_agent_instruction(api, status):
    from app import db
    from app.models import Incident, OperatorMessage
    with db.session() as s:
        s.get(Incident, api.iid).status = status
    r = api.client.post(f"/api/incidents/{api.iid}/messages", json={"body": "설정 ConfigMap을 new로 바꿔줘"})
    assert r.status_code == 202, r.text
    assert r.json()["command_id"] and r.json()["applies"] == "command_confirmation_required"
    assert api.kube.get(operation())["data"]["mode"] == "old"   # dry-run only
    with db.session() as s:
        m = s.query(OperatorMessage).filter_by(incident_id=api.iid).one()
        assert m.applied_at is not None and m.intent == "cluster_change"


def test_secret_http_never_echoes_or_persists_values(api, caplog):
    from app import db
    api.planner.operations = [{"op": "secret_update", "apiVersion": "v1", "kind": "Secret", "namespace": "sandbox", "name": "app-key", "keys": ["ACCESS"]}]
    c = plan(api, "sandbox의 Secret app-key의 ACCESS를 변경해")
    r = api.client.post(f"/api/commands/{c['id']}/confirm", json={"plan_sha256": c["plan_sha256"]})
    assert r.json()["status"] == "awaiting_secret" and api.writer.calls == 0
    sentinel = "P15_SENTINEL_MUST_NOT_PERSIST_8b7f6130"
    r = api.client.post(f"/api/commands/{c['id']}/secret-values", json={"plan_sha256": c["plan_sha256"], "values": {"ACCESS": sentinel}})
    assert r.status_code == 200 and r.json() == {"status": "succeeded", "keys": ["ACCESS"]}
    assert api.writer.calls == 1
    assert sentinel not in r.text and sentinel not in caplog.text and sentinel not in str(api.planner.prompts)
    with db.session() as s:
        tables = s.execute(text("select tablename from pg_tables where schemaname='public'")).scalars().all()
        for table in tables:
            rows = s.execute(text(f'SELECT row_to_json(t)::text FROM "{table}" t')).scalars().all()
            assert not any(sentinel in row for row in rows), table
    assert api.client.post(f"/api/commands/{c['id']}/secret-values", json={"plan_sha256": c["plan_sha256"], "values": {"ACCESS": sentinel}}).status_code == 409
    assert api.writer.calls == 1


def test_secret_http_rejects_invalid_or_oversize_body_without_input_echo(api):
    sentinel = "SECRET_NOT_IN_ERROR_17a38a9e"
    for body, status in ((json.dumps({"values": {"X": sentinel}}).encode(), 422),
                         (b'{"values":"' + sentinel.encode() + b'"', 422),
                         (b'x' * 40961 + sentinel.encode(), 413)):
        r = api.client.post("/api/commands/1/secret-values", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == status, r.text
        assert sentinel not in r.text


def test_pending_commands_fail_closed_on_restart_without_replaying_writes(api):
    from app import db
    from app.command_api import recover_pending_commands
    from app.models import OperatorCommand
    rows = []
    for st in ("planning", "executing", "rolling_back", "awaiting_confirmation", "awaiting_secret"):
        c = api.service.create(api.iid, "시험")
        with db.session() as s:
            row = s.get(OperatorCommand, c["id"])
            row.status = st
            row.result = {"claimed": True, "inflight": 0} if st == "executing" else None
        rows.append((c["id"], st))
    recover_pending_commands()
    for cid, st in rows:
        got = api.service.get(cid)
        assert got["status"] == ("failed" if st in ("planning", "executing", "rolling_back") else st)
        if st == "executing":
            assert got["result"]["inflight"] == 0
    assert not api.kube.calls


def test_command_routes_are_host_and_origin_guarded(api):
    for path in (f"/api/incidents/{api.iid}/commands", "/api/commands/1/confirm", "/api/commands/1/cancel", "/api/commands/1/rollback", "/api/commands/1/secret-values"):
        for headers in ({"X-LiveProbe-Client": ""}, {"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            r = api.client.post(path, json={"instruction": "x", "plan_sha256": "0" * 64, "values": {}}, headers=headers)
            assert r.status_code == 403, (path, r.status_code)
