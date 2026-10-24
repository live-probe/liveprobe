"""Group model settings: persisted, versioned, isolated from safety and active runs."""
from __future__ import annotations

from test_api import client  # noqa: F401 - shared real PostgreSQL/TestClient fixture
import pytest


def test_model_settings_defaults_and_save_survive_fresh_read(client):
    response = client.get('/api/model-settings')
    assert response.status_code == 200
    initial = response.json()
    assert initial['provider'] == 'codex_cli'
    assert initial['revision'] == 0
    assert set(initial['groups']) == {'SIMPLE', 'NORMAL', 'COMPLEX', 'HIGH_RISK'}
    assert initial['groups']['NORMAL']['primary_mode'] == 'router'
    payload = {k: initial[k] for k in ('router', 'groups')}
    payload['expected_revision'] = initial['revision']
    payload['groups']['NORMAL']['roles']['log_summarizer']['effort'] = 'medium'
    saved = client.put('/api/model-settings', json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] == 1
    again = client.get('/api/model-settings').json()
    assert again['groups']['NORMAL']['roles']['log_summarizer']['effort'] == 'medium'
    assert again['source'] == 'database'


@pytest.mark.parametrize('level,role,model,effort,mode', [
    ('SIMPLE', 'result_verifier', 'gpt-6-luna', 'low', 'router'),
    ('NORMAL', 'group_verifier', 'gpt-6-luna', 'medium', 'router'),
    ('COMPLEX', 'challenger', 'gpt-6.1-sol', 'high', 'router'),
    ('HIGH_RISK', 'safety_verifier', 'gpt-6-astra', 'low', 'router'),
    ('NORMAL', 'k8s_analyst', 'gpt-6-luna', 'low', 'fixed'),
])
def test_rejects_reduced_verification_or_below_floor_primary(client, level, role, model, effort, mode):
    initial = client.get('/api/model-settings').json()
    payload = {k: initial[k] for k in ('router', 'groups')}
    payload['expected_revision'] = 0
    payload['groups'][level]['roles'][role] = {'model': model, 'effort': effort}
    payload['groups'][level]['primary_mode'] = mode
    response = client.put('/api/model-settings', json=payload)
    assert response.status_code == 422, response.text
    assert client.get('/api/model-settings').json()['revision'] == 0


def test_version_conflict_and_audit(client):
    from app import db
    from app.models import AuditLog
    from sqlalchemy import select
    state = client.get('/api/model-settings').json()
    body = {'expected_revision': 0, 'router': state['router'], 'groups': state['groups']}
    assert client.put('/api/model-settings', json=body).status_code == 200
    assert client.put('/api/model-settings', json=body).status_code == 409
    with db.session() as s:
        events = list(s.scalars(select(AuditLog).where(AuditLog.event == 'group_model_settings_changed')))
        assert len(events) == 1
        assert events[0].detail['revision'] == 1


def test_snapshot_separates_group_roles_and_freezes_revision(client):
    from app import db, model_settings as M
    from app.config import AppConfig
    cfg = AppConfig()
    state = client.get('/api/model-settings').json()
    body = {'expected_revision': 0, 'router': state['router'], 'groups': state['groups']}
    body['groups']['NORMAL']['roles']['group_verifier']['effort'] = 'high'
    body['groups']['COMPLEX']['roles']['group_verifier']['effort'] = 'xhigh'
    body['groups']['NORMAL']['primary_mode'] = 'fixed'
    body['groups']['NORMAL']['roles']['k8s_analyst']['model'] = 'gpt-6.1-sol'
    assert client.put('/api/model-settings', json=body).status_code == 200
    with db.session() as s:
        frozen, snapshot = M.snapshot_config(s, cfg)
    policy = {'selected_model': 'gpt-6-astra', 'candidates': []}
    normal, np = M.apply_group(frozen, snapshot, 'NORMAL', policy)
    complex_, cp = M.apply_group(frozen, snapshot, 'COMPLEX', policy)
    assert normal.llm.roles['group_verifier'].effort == 'high'
    assert complex_.llm.roles['group_verifier'].effort == 'xhigh'
    assert np['selected_model'] == 'gpt-6.1-sol'
    assert np['model_settings']['primary_mode'] == 'fixed'
    assert cp['selected_model'] == 'gpt-6-astra'
    assert cp['model_settings']['roles']['cause_reasoner']['model'] == 'gpt-6-astra'
    body['expected_revision'] = 1
    body['groups']['NORMAL']['roles']['group_verifier']['effort'] = 'medium'
    assert client.put('/api/model-settings', json=body).status_code == 200
    assert snapshot['revision'] == 1
    assert normal.llm.roles['group_verifier'].effort == 'high'
    assert 'group_verifier' not in cfg.llm.roles
    assert frozen.llm.roles['router'] is not cfg.llm.roles['router']


def test_engine_uses_private_snapshot_and_evaluation_bypasses_settings(client):
    from app.engine import Engine
    from app.config import AppConfig, Settings
    seen = []
    class ObservedEngine(Engine):
        def _process(self, iid, *, arm=None):
            seen.append((self.cfg, getattr(self, 'model_snapshot', None), arm))
    engine = ObservedEngine(AppConfig(), Settings(), None, None, None)
    state = client.get('/api/model-settings').json()
    body = {'expected_revision': 0, 'router': state['router'], 'groups': state['groups']}
    body['router']['effort'] = 'high'
    assert client.put('/api/model-settings', json=body).status_code == 200
    engine.process(123)
    engine.process(124, arm={'strategy': 'adaptive'})
    assert len(seen) == 2
    assert seen[0][0].llm.roles['router'].effort == 'high'
    assert seen[0][1]['revision'] == 1
    assert seen[1][0].llm.roles['router'].effort == 'low'
    assert seen[1][1] is None
    assert engine.cfg.llm.roles['router'].effort == 'low'


def test_corrupt_saved_placement_fails_closed_before_calls(client):
    from app import db, model_settings as M
    from app.config import AppConfig
    from app.models import Setting
    initial = client.get('/api/model-settings').json()
    body = {'expected_revision': 0, 'router': initial['router'], 'groups': initial['groups']}
    assert client.put('/api/model-settings', json=body).status_code == 200
    with db.session() as s:
        row = s.get(Setting, M.KEY)
        assert row is not None
        import copy
        value = copy.deepcopy(row.value)
        value['groups']['HIGH_RISK']['roles']['safety_verifier']['effort'] = 'low'
        row.value = value
    with db.session() as s, pytest.raises(ValueError, match='안전 검증'):
        M.snapshot_config(s, AppConfig())


@pytest.mark.parametrize('field,value', [('provider', 'openai_api'), ('allowlist', ['shell']), ('expected_revision', True)])
def test_payload_cannot_change_provider_or_policy(client, field, value):
    initial = client.get('/api/model-settings').json()
    body = {'expected_revision': 0, 'router': initial['router'], 'groups': initial['groups'], field: value}
    assert client.put('/api/model-settings', json=body).status_code == 422


def test_default_revision_zero_is_captured_and_recorded_before_first_save(db_url):
    from app import db, model_settings as M
    from app.config import AppConfig
    cfg = AppConfig()
    with db.session() as s:
        frozen, snapshot = M.snapshot_config(s, cfg)
    assert snapshot is not None
    assert snapshot['revision'] == 0
    active, policy = M.apply_group(frozen, snapshot, 'SIMPLE', {'selected_model': 'gpt-6-luna'})
    assert policy['model_settings']['revision'] == 0
    assert policy['model_settings']['roles']['signal_analyst']['model'] == 'gpt-6-luna'
    assert active.llm.roles['result_verifier'].model == 'gpt-6.1-sol'


def test_router_only_model_requires_known_price():
    from app import model_settings as M
    from app.config import AppConfig
    cfg = AppConfig()
    value = M.defaults(cfg)
    # Only the common classifier uses luna; group roles all have a known mid/high price.
    for group in value['groups'].values():
        for placement in group['roles'].values():
            if placement['model'] == 'gpt-6-luna':
                placement['model'] = 'gpt-6.1-sol'
    cfg.llm.prices.pop('gpt-6-luna')
    body = M.ModelSettingsIn(expected_revision=0, **value)
    with pytest.raises(ValueError, match='가격'):
        M.validate_settings(cfg, body)


def test_registered_model_missing_from_escalation_ladder_is_rejected():
    from app import model_settings as M
    from app.config import AppConfig
    cfg = AppConfig()
    cfg.workflows.model_ladder['openai'] = ['gpt-6-luna', 'gpt-6-astra']
    body = M.ModelSettingsIn(expected_revision=0, **M.defaults(cfg))
    with pytest.raises(ValueError, match='승격'):
        M.validate_settings(cfg, body)


def test_concurrent_first_save_has_one_winner(client):
    from concurrent.futures import ThreadPoolExecutor
    from app import db, model_settings as M
    from app.config import AppConfig
    initial = client.get('/api/model-settings').json()
    body = M.ModelSettingsIn(expected_revision=0, router=initial['router'], groups=initial['groups'])
    def attempt(_):
        try:
            with db.session() as s:
                M.save_settings(s, AppConfig(), body, 'test')
            return 'saved'
        except M.RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['conflict', 'saved']
