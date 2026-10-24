"""Secret write-only path; synthetic sentinels must never leave transient request memory."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import db
from app.config import AppConfig
from app.models import Incident
from test_p15_commands import MemoryKube, Planner, operation
from test_p15_operator_kube import http_api  # loopback API fixture, teardown checks thread cleanup

SENTINEL = "P15_SYNTHETIC_SECRET_NEVER_PERSIST_7e913f"


def secret_op():
    return {"op": "secret_update", "apiVersion": "v1", "kind": "Secret", "namespace": "sandbox", "name": "db", "keys": ["PASSWORD"]}


def test_secret_http_patch_does_not_read_echo_and_releases_connection(http_api, caplog):
    from app.secret_writer import SecretWriter
    caplog.set_level(logging.DEBUG)
    h = http_api
    h.response = {"kind": "Secret", "data": {"PASSWORD": SENTINEL}, "stringData": {"PASSWORD": SENTINEL}}
    writer = SecretWriter(h.api)
    out = writer.write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])
    assert out == {"status": "succeeded", "keys": ["PASSWORD"]}
    assert h.calls[-1]["method"] == "PATCH"
    assert h.calls[-1]["path"] == "/api/v1/namespaces/sandbox/secrets/db"
    assert h.calls[-1]["content_type"] == "application/merge-patch+json"
    assert h.calls[-1]["body"] == {"stringData": {"PASSWORD": SENTINEL}}
    assert h.calls[-1]["query"].get("dryRun") is None
    assert all(c["method"] == "PATCH" for c in h.calls)
    assert len(h.calls) == 2 and h.calls[0]["query"]["dryRun"] == ["All"]
    assert h.calls[0]["body"] == {"stringData": {"PASSWORD": SENTINEL}}
    assert SENTINEL not in caplog.text and SENTINEL not in json.dumps(out)


def test_status_only_response_is_never_deserialized_or_read():
    from app.secret_writer import SecretWriter
    class Response:
        status = 200
        closed = released = False
        @property
        def data(self):
            raise AssertionError("response body was accessed")
        def close(self):
            self.closed = True
        def release_conn(self):
            self.released = True
    r = Response()
    from kubernetes import client
    api = client.ApiClient(client.Configuration())
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return r
    api.rest_client.pool_manager.request = request
    try:
        out = SecretWriter(api).write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])
        assert out["status"] == "succeeded"
        assert calls[0]["preload_content"] is False
        assert r.closed and r.released
    finally:
        api.close()


@pytest.mark.parametrize("status", [200, 201, 204, 299, 300, 403, 422, 500])
def test_secret_status_is_2xx_only_and_error_bodies_do_not_leak(http_api, caplog, status):
    from app.commands import CommandError
    from app.secret_writer import SecretWriter
    caplog.set_level(logging.DEBUG)
    http_api.status, http_api.response = status, {"message": SENTINEL}
    writer = SecretWriter(http_api.api)
    if 200 <= status < 300:
        assert writer.write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])["status"] == "succeeded"
    else:
        with pytest.raises(CommandError) as e:
            writer.write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])
        assert SENTINEL not in str(e.value)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("values", [[], {"OTHER": SENTINEL}, {"PASSWORD": [SENTINEL]}, {"PASSWORD": "한" * 683}, {str(i): SENTINEL for i in range(17)}, {"bad/key": SENTINEL}])
def test_validation_is_static_and_does_not_send_http(http_api, values):
    from app.commands import CommandError
    from app.secret_writer import SecretWriter
    with pytest.raises(CommandError) as e:
        SecretWriter(http_api.api).write("sandbox", "db", values, expected_keys=["PASSWORD"])
    assert SENTINEL not in str(e.value) and http_api.calls == []


def test_confirmed_secret_plan_never_reads_secret_or_persists_values(db_url, http_api, caplog):
    from app.commands import CommandService
    from app.secret_writer import SecretWriter
    caplog.set_level(logging.DEBUG)
    with db.session() as s:
        i = Incident(fingerprint="secret-path", namespace="sandbox", workload="db", status="handed_off", title="Secret 변경")
        s.add(i)
        s.flush()
        iid = i.id
    planner, kube, writer = Planner([secret_op()]), MemoryKube(), SecretWriter(http_api.api)
    service = CommandService(AppConfig(), planner, kube, secret_writer=writer)
    c = service.create(iid, "db Secret PASSWORD 키를 변경해줘")
    c = service.plan(c["id"])
    assert c["status"] == "awaiting_confirmation"
    assert c["dry_run"][0]["ok"] is True
    assert http_api.calls[-1]["query"]["dryRun"] == ["All"]
    assert c["side_effects"]["reversible"] is False
    assert service.confirm(c["id"], c["plan_sha256"])["status"] == "awaiting_secret"
    http_api.response = {"data": {"PASSWORD": SENTINEL}}
    out = service.secret_values(c["id"], c["plan_sha256"], {"PASSWORD": SENTINEL}, writer)
    assert out == {"status": "succeeded", "keys": ["PASSWORD"]}
    assert kube.calls == [] and planner.prompts and SENTINEL not in json.dumps(planner.prompts)
    assert SENTINEL not in json.dumps(service.get(c["id"]))
    assert SENTINEL not in caplog.text
    # Scan ALL public tables (JSON, text, AgentRun, events, audit), not a selected subset.
    with db.engine().connect() as conn:
        tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all()
        assert "operator_commands" in tables and "agent_runs" in tables
        for table in tables:
            rows = conn.execute(text('SELECT row_to_json(t)::text FROM public."' + table.replace('"', '""') + '" t')).scalars()
            assert all(SENTINEL not in row for row in rows), table


@pytest.mark.parametrize("body", [
    b'{"plan_sha256":"x", "values":{"PASSWORD":"one","PASSWORD":"two"}}',
    b'{"plan_sha256":"x", "values":{"PASSWORD":"P15_SYNTHETIC_SECRET_NEVER_PERSIST_7e913f"}, "extra":1}',
    b'{not-json-P15_SYNTHETIC_SECRET_NEVER_PERSIST_7e913f',
])
def test_raw_endpoint_body_validation_never_echoes_values(body):
    from app.commands import CommandError
    from app.secret_writer import parse_secret_payload
    with pytest.raises(CommandError) as e:
        parse_secret_payload(body)
    assert SENTINEL not in str(e.value)


def test_raw_endpoint_body_returns_only_transient_values_for_direct_writer():
    from app.secret_writer import parse_secret_payload
    digest, values = parse_secret_payload(json.dumps({"plan_sha256": "a" * 64, "values": {"PASSWORD": SENTINEL}}).encode())
    assert digest == "a" * 64 and values == {"PASSWORD": SENTINEL}


def test_exception_from_secret_transport_or_cleanup_is_sanitized(http_api):
    from app.commands import CommandError
    from app.secret_writer import SecretWriter
    def fail(*args, **kwargs):
        raise RuntimeError(SENTINEL)
    http_api.api.rest_client.pool_manager.request = fail
    with pytest.raises(CommandError) as e:
        SecretWriter(http_api.api).write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])
    assert SENTINEL not in str(e.value)
    class Response:
        status = 200
        def close(self):
            raise RuntimeError(SENTINEL)
        def release_conn(self):
            pass
    http_api.api.rest_client.pool_manager.request = lambda *a, **kw: Response()
    with pytest.raises(CommandError) as e:
        SecretWriter(http_api.api).write("sandbox", "db", {"PASSWORD": SENTINEL}, expected_keys=["PASSWORD"])
    assert SENTINEL not in str(e.value)


def test_secret_service_error_never_persists_exception_body_or_values(db_url, http_api, caplog):
    from app.commands import CommandError, CommandService
    from app.secret_writer import SecretWriter
    with db.session() as s:
        i = Incident(fingerprint="secret-failure", namespace="sandbox", workload="db", status="analyzing", title="시험")
        s.add(i)
        s.flush()
        iid = i.id
    writer, planner = SecretWriter(http_api.api), Planner([secret_op()])
    service = CommandService(AppConfig(), planner, MemoryKube(), secret_writer=writer)
    c = service.plan(service.create(iid, "Secret 키 변경")["id"])
    service.confirm(c["id"], c["plan_sha256"])
    with pytest.raises(CommandError):
        service.secret_values(c["id"], c["plan_sha256"], {"PASSWORD": [SENTINEL]}, writer)
    assert service.get(c["id"])["status"] == "awaiting_secret"
    http_api.status, http_api.response = 403, {"message": SENTINEL}
    assert service.secret_values(c["id"], c["plan_sha256"], {"PASSWORD": SENTINEL}, writer)["status"] == "failed"
    assert SENTINEL not in json.dumps(service.get(c["id"])) and SENTINEL not in caplog.text
    with db.engine().connect() as conn:
        for table in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars().all():
            assert all(SENTINEL not in row for row in conn.execute(text('SELECT row_to_json(t)::text FROM public."' + table.replace('"', '""') + '" t')).scalars()), table
