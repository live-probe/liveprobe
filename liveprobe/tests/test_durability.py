"""KAN-49 durability, replay and denial regressions against disposable pgvector DB."""
import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from conftest import ROOT, needs_db


def test_transition_guards():
    from app.lifecycle import transition
    from app.models import Incident
    inc = Incident(status="open")
    transition(inc, "analyzing")
    transition(inc, "action_pending")
    transition(inc, "acting")
    transition(inc, "resolved")
    assert inc.closed_at is not None
    with pytest.raises(ValueError):
        transition(inc, "open")


@needs_db
def test_migration_reversible_and_keeps_incidents(db_url):
    from app import db
    from app.models import Incident
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["sqlalchemy_url"] = db_url
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="n", workload="w", title="t")
        s.add(inc)
        s.flush()
        iid = inc.id
    command.downgrade(cfg, "0002")
    with db.engine().connect() as c:
        assert c.execute(text("select count(*) from incidents where id=:id"), {"id": iid}).scalar_one() == 1
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0002"
    db.run_migrations(db_url)
    with db.engine().connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0008"
        assert c.execute(text("select count(*) from incidents where id=:id"), {"id": iid}).scalar_one() == 1


@needs_db
def test_restart_marks_inflight_without_repeating_actions(db_url):
    from app import db
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.models import Action, AuditLog, Incident, WorkflowRun
    with db.session() as s:
        inc = Incident(fingerprint="r", namespace="n", workload="w", title="t", status="acting")
        s.add(inc); s.flush()
        act = Action(incident_id=inc.id, type="rollout_undo", target="n/Deployment/w",
                     params={}, policy_mode="auto", status="executing")
        s.add_all([act, WorkflowRun(incident_id=inc.id, status="running", phase="acting", snapshot={})])
        iid = inc.id
    engine = Engine(AppConfig(), Settings(), None, None, None)
    engine.recover_on_startup()
    with db.session() as s:
        assert s.get(Incident, iid).status == "failed"
        assert s.scalar(select(Action).where(Action.incident_id == iid)).status == "failed"
        assert "indeterminate" in s.scalar(select(Action).where(Action.incident_id == iid)).reason
        assert s.scalar(select(WorkflowRun).where(WorkflowRun.incident_id == iid)).status == "failed"
        assert s.scalar(select(AuditLog).where(AuditLog.event == "action_interrupted"))
    assert engine.q.empty()


@needs_db
def test_restart_recovery_publishes_durable_failed_events(db_url):
    """KAN-101(a): recovery transitions are persisted as SSE events and replayed from a pre-restart cursor."""
    from typing import Any, cast
    from app import db
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.main import stream
    from app.models import Action, Incident, IncidentEvent, WorkflowRun
    from app.sse import Broker
    with db.session() as s:
        acting = Incident(fingerprint="ra", namespace="n", workload="w", title="t", status="acting")
        pending = Incident(fingerprint="rp", namespace="n", workload="w2", title="t", status="action_pending")
        done = Incident(fingerprint="rd", namespace="n", workload="w3", title="t", status="resolved")
        s.add_all([acting, pending, done]); s.flush()
        s.add_all([
            Action(incident_id=acting.id, type="e2e_noop", target="n/Deployment/w", params={},
                   policy_mode="auto", status="executing"),
            Action(incident_id=pending.id, type="e2e_noop", target="n/Deployment/w2", params={},
                   policy_mode="approval", status="approved"),
            WorkflowRun(incident_id=acting.id, status="running", phase="acting", snapshot={}),
        ])
        ids = {"acting": acting.id, "pending": pending.id, "done": done.id}
    Broker().publish(ids["acting"], "action", "acting")  # last event a dashboard saw before the restart
    with db.session() as s:
        cursor = s.scalar(select(func.max(IncidentEvent.id)))

    Engine(AppConfig(), Settings(), None, None, None).recover_on_startup()

    with db.session() as s:
        rows = list(s.scalars(select(IncidentEvent).where(IncidentEvent.id > cursor).order_by(IncidentEvent.id)))
        assert s.get(Incident, ids["acting"]).status == "failed"
        assert s.get(Incident, ids["pending"]).status == "action_pending"
        assert all(a.status == "failed" for a in s.scalars(select(Action)))
    got = [(r.incident_id, r.event, r.status) for r in rows]
    assert got == [(ids["acting"], "failed", "failed"), (ids["pending"], "action", "action_pending")]

    class Request:
        headers = {"last-event-id": str(cursor)}
        query_params: dict = {}

        async def is_disconnected(self):
            return False

    async def check():
        response = await stream(cast(Any, Request()), user="admin")
        gen = cast(Any, response.body_iterator)
        first = await anext(gen)
        second = await anext(gen)
        await gen.aclose()
        assert first.id == str(rows[0].id) and first.event == "incident"
        assert f'"incident_id": {ids["acting"]}' in first.data and '"event": "failed"' in first.data
        assert '"status": "failed"' in first.data
        assert second.id == str(rows[1].id) and '"event": "action"' in second.data

    asyncio.run(check())

    # Idempotent: a second restart with nothing in flight publishes nothing new.
    Engine(AppConfig(), Settings(), None, None, None).recover_on_startup()
    with db.session() as s:
        assert s.scalar(select(func.max(IncidentEvent.id))) == rows[-1].id


@needs_db
def test_event_replay_after_broker_restart(db_url):
    from app import db
    from app.models import Incident, IncidentEvent
    from app.sse import Broker
    with db.session() as s:
        inc = Incident(fingerprint="e", namespace="n", workload="w", title="t")
        s.add(inc); s.flush(); iid = inc.id
    Broker().publish(iid, "created", "open")
    Broker().publish(iid, "updated", "analyzing")
    with db.session() as s:
        rows = list(s.scalars(select(IncidentEvent).order_by(IncidentEvent.id)))
        assert [r.event for r in rows] == ["created", "updated"]
        assert rows[0].id < rows[1].id
    from app.main import stream
    class Request:
        headers = {"last-event-id": str(rows[0].id)}
        query_params = {}
        async def is_disconnected(self):
            return False
    async def check():
        response = await stream(Request(), user="admin")
        gen = response.body_iterator
        event = await anext(gen)
        await gen.aclose()
        assert event.id == str(rows[1].id)
        assert '"event": "updated"' in event.data
    asyncio.run(check())


@needs_db
def test_new_incident_event_replays_on_reconnect(db_url):
    """A fresh incident event is replayed from the last acknowledged cursor."""
    from typing import Any, cast
    from app import db
    from app.main import stream
    from app.models import Incident, IncidentEvent
    from app.sse import Broker

    with db.session() as s:
        inc = Incident(fingerprint="reconnect", namespace="n", workload="w", title="t")
        s.add(inc)
        s.flush()
        iid = inc.id
    broker = Broker()
    broker.publish(iid, "created", "open")
    with db.session() as s:
        first_id = s.scalar(select(IncidentEvent.id))
    assert first_id is not None
    broker.publish(iid, "updated", "analyzing")

    class Request:
        headers = {}
        query_params = {"since": str(first_id)}

        async def is_disconnected(self):
            return False

    async def check():
        response = await stream(cast(Any, Request()), user="admin")
        gen = cast(Any, response.body_iterator)
        event = await anext(gen)
        await gen.aclose()
        assert event.id == str(first_id + 1)
        assert event.event == "incident"
        assert f'"incident_id": {iid}' in event.data
        assert '"event": "updated"' in event.data

    asyncio.run(check())
