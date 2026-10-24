"""P14 integration: late network risk blocks stale-level proposals and queued execution."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import db, actions, safety
from app.config import AppConfig, Settings
from app.engine import Engine
from app.models import Incident, Action


def incident():
    with db.session() as s:
        inc = Incident(fingerprint='p14/network', namespace='liveprobe-target', workload='inventory',
                       title='late network drop', status='analyzing', severity_level='COMPLEX',
                       signal_count=2, signal_reasons=['ErrImagePull', 'NetworkDrop'])
        s.add(inc)
        s.flush()
        return inc.id


def test_late_network_drop_hands_off_before_proposing_any_action(db_url):
    iid = incident()
    eng = Engine(AppConfig(), Settings(), None, None, None)
    assert eng._propose_and_run(iid, 'COMPLEX', {'type': 'rollout_undo', 'kind': 'Deployment',
                                               'name': 'inventory', 'params': {}}, 'stale analysis')
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc is not None
        assert inc.status == 'handed_off'
        assert inc.severity_level == 'HIGH_RISK'
        assert 'NetworkDrop' in (inc.status_reason or '')
        assert list(s.scalars(select(Action).where(Action.incident_id == iid))) == []


@pytest.mark.parametrize('kind', ['rollout_undo', 'rollout_restart', 'set_memory_limit'])
def test_late_network_risk_cannot_be_approved_using_stale_level(db_url, kind):
    iid = incident()
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc is not None
        if kind == 'rollout_undo':
            act = safety.propose(s, AppConfig(), inc, 'COMPLEX', kind, 'Deployment', 'inventory')
            assert act.status == 'denied'
            assert act.policy_mode == 'handoff'
        else:
            assert actions.typed_policy(s, 'COMPLEX', kind, iid) == 'handoff'
        # Even an action queued before the new signal cannot execute from its old policy.
        act = Action(incident_id=iid, type=kind, target='liveprobe-target/Deployment/inventory',
                     params={}, policy_mode='auto', status='proposed')
        s.add(act)
        s.flush()
        if kind == 'rollout_undo':
            assert not safety._authorized(s, act, inc)
        else:
            assert actions._authorized(s, act, inc) is not None
