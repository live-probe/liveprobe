"""P17: an operator closes an incident the AI already stopped on. A record decision, never a cluster change."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from conftest import needs_db
from test_api import client  # noqa: F401 - shared API fixture

CLOSE = {"reason": "대상 정상 확인, 추가 조치 불필요", "confirm": True}


def test_closed_is_terminal_and_reachable_only_from_operator_stops():
    from app.lifecycle import TRANSITIONS, transition
    from app.models import INCIDENT_CLOSED_STATES, Incident
    assert "closed" in INCIDENT_CLOSED_STATES and TRANSITIONS["closed"] == set()
    for start in ("handed_off", "action_pending"):
        inc = Incident(status=start)
        transition(inc, "closed")
        assert inc.status == "closed" and inc.closed_at is not None
    for start in ("open", "analyzing", "acting", "resolved", "failed"):
        with pytest.raises(ValueError):
            transition(Incident(status=start), "closed")
    # Every other terminal state still stamps closed_at even though handed_off now has a successor.
    inc = Incident(status="analyzing")
    transition(inc, "handed_off")
    assert inc.closed_at is not None


def _seed(status: str, **fields) -> int:
    from app import db
    from app.models import Incident
    with db.session() as s:
        inc = Incident(fingerprint=f"p17-{status}", namespace="liveprobe-target", workload="inventory",
                       status=status, title="OOMKilled on Pod/inventory", signal_reasons=["OOMKilled"],
                       severity_level="NORMAL", **fields)
        s.add(inc)
        s.flush()
        return inc.id


@needs_db
def test_close_handoff_records_reason_audit_and_leaves_cluster_alone(client):  # noqa: F811
    from app import db
    from app.models import AuditLog, Incident, RunEvent
    iid = _seed("handed_off", handoff={"summary": "operator review", "claimed_by": "local-operator"})
    path = f"/api/incidents/{iid}/close"
    assert client.post(path, json=CLOSE, headers={"X-LiveProbe-Client": ""}).status_code == 403
    assert client.post(path, json=CLOSE, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.post(path, json={**CLOSE, "confirm": False}).status_code == 422
    assert client.post(path, json={**CLOSE, "reason": "   "}).status_code == 422
    assert client.post(path, json={**CLOSE, "reason": "가" * 501}).status_code == 422
    assert client.post("/api/incidents/999999/close", json=CLOSE).status_code == 404
    r = client.post(path, json=CLOSE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed" and body["closed_by"] == "local-operator"
    assert body["close_reason"] == CLOSE["reason"] and body["closed_from"] == "handed_off"
    assert body["cluster_changed"] is False and body["closed_at"].endswith("Z")
    again = client.post(path, json={**CLOSE, "reason": "두 번째 사유"})
    assert again.status_code == 200 and again.json() == body  # idempotent: the first decision stands
    detail = client.get(f"/api/incidents/{iid}").json()["incident"]
    assert detail["status"] == "closed" and detail["handoff"]["claimed_by"] == "local-operator"
    assert detail["handoff"]["close_reason"] == CLOSE["reason"]
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.status_reason == f"운영자 종료: {CLOSE['reason']}" and inc.closed_at is not None
        audits = [(a.event, a.detail) for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == iid))]
        assert [e for e, _ in audits] == ["incident_closed"]
        assert audits[0][1] == {"previous_status": "handed_off", "reason": CLOSE["reason"], "cluster_changed": False}
        kinds = [(e.kind, e.detail.get("outcome")) for e in s.scalars(select(RunEvent).where(RunEvent.incident_id == iid))]
        assert kinds == [("completed", "closed")]
    story = client.get(f"/api/incidents/{iid}/narrative").json()
    assert story["headline"]["stage"] == {"key": "closed", "label": "운영자 종료"}
    assert "운영자가 종료" in story["headline"]["next"]
    outcome = next(n for n in story["flow"]["nodes"] if n["id"] == "outcome")
    assert outcome["label"] == "운영자 종료" and outcome["status"] == "done"
    # A closed incident accepts no further AI instructions or run control.
    assert client.post(f"/api/incidents/{iid}/messages", json={"body": "재시작해"}).status_code == 409
    assert client.post(f"/api/incidents/{iid}/control", json={"action": "stop"}).status_code == 409


@needs_db
def test_close_refuses_live_work_and_other_operators(client):  # noqa: F811
    from app import db
    from app.models import Action, Incident, OperatorCommand
    for status in ("open", "analyzing", "acting", "resolved", "failed"):
        assert client.post(f"/api/incidents/{_seed(status)}/close", json=CLOSE).status_code == 409
    other = _seed("handed_off", handoff={"claimed_by": "someone-else"})
    assert client.post(f"/api/incidents/{other}/close", json=CLOSE).status_code == 409
    waiting = _seed("action_pending")
    with db.session() as s:
        s.add(Action(incident_id=waiting, type="set_memory_limit", target="liveprobe-target/Deployment/inventory",
                     params={}, policy_mode="approval", status="pending_approval"))
    r = client.post(f"/api/incidents/{waiting}/close", json=CLOSE)
    assert r.status_code == 409 and "action" in r.json()["detail"]
    busy = _seed("handed_off")
    with db.session() as s:
        s.add(OperatorCommand(incident_id=busy, actor="local-operator", instruction="x", status="awaiting_confirmation"))
    r = client.post(f"/api/incidents/{busy}/close", json=CLOSE)
    assert r.status_code == 409 and "command" in r.json()["detail"]
    with db.session() as s:
        assert {s.get(Incident, i).status for i in (other, waiting, busy)} == {"handed_off", "action_pending"}


@needs_db
def test_stale_approval_record_closes_when_its_action_already_finished(client):  # noqa: F811
    """Incident #14 shape: the action succeeded long ago but the incident stayed action_pending."""
    from app import db
    from app.models import Action
    iid = _seed("action_pending")
    with db.session() as s:
        s.add(Action(incident_id=iid, type="rollout_undo", target="liveprobe-target/Deployment/inventory",
                     params={}, policy_mode="approval", status="succeeded", executed=True))
    r = client.post(f"/api/incidents/{iid}/close", json=CLOSE)
    assert r.status_code == 200 and r.json()["closed_from"] == "action_pending"


def test_closed_incident_never_warns_on_the_map():
    import time
    from app.flows import build_topology
    from test_flows import _inv
    inc = {"id": 9, "namespace": "shop", "workload": "web", "status": "closed", "severity_level": "NORMAL",
           "title": "t", "claimed": False, "updated_ts": time.time()}
    topo = build_topology(_inv(), [], {"state": "connected", "stale": False}, [inc])
    web = next(n for n in topo["nodes"] if n["id"] == "shop/Deployment/web")
    assert not web.get("warnings")
