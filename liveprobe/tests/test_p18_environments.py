"""P18 environment/system contracts."""
from app.config import AppConfig


def test_default_environments_and_legacy_config():
    assert AppConfig().target_namespaces == ["dev", "stg", "prd"]
    assert AppConfig(target_namespace="legacy").target_namespaces == ["legacy"]
    assert AppConfig(target_namespace="old", target_namespaces=["prd", "dev"]).target_namespaces == ["prd", "dev"]
    assert AppConfig().environment("prd") == "prd"
    assert AppConfig().environment("legacy") is None


def test_system_is_observed_and_environments_never_merge(db_url):
    from app import db
    from app.incidents import ingest, Signal
    from conftest import FakeKube, mk_dep
    k = FakeKube()
    k.deployments["payment"] = mk_dep("payment", 1, "img")
    k.deployments["payment"]["metadata"]["labels"] = {"app.kubernetes.io/part-of": "payment"}
    cfg = AppConfig()
    with db.session() as s:
        dev, _ = ingest(s, cfg, Signal("dev", "Deployment", "payment", "ApplicationException", "app_log"), kube=k)
        prd, _ = ingest(s, cfg, Signal("prd", "Deployment", "payment", "ApplicationException", "app_log"), kube=k)
        assert dev.id != prd.id
        assert dev.system == prd.system == "payment"
    k.deployments.clear()
    with db.session() as s:
        unknown, _ = ingest(s, cfg, Signal("stg", "Deployment", "unknown", "CrashLoopBackOff", "pod_watch"), kube=k)
        assert unknown.system is None


def test_watch_threads_cover_each_environment(monkeypatch):
    from app.watcher import Watchers
    names = []
    class Thread:
        def __init__(self, **kw): names.append(kw["name"])
        def start(self): pass
    monkeypatch.setattr("app.watcher.threading.Thread", Thread)
    Watchers(None, AppConfig(), lambda s: None).start()
    for env in ("dev", "stg", "prd"):
        assert f"pod_watch/{env}" in names
        assert f"event_watch/{env}" in names
        assert f"endpoint_poller/{env}" in names


def test_alert_accepts_all_configured_environments():
    from app.main import alert_signal
    for env in ("dev", "stg", "prd"):
        sig = alert_signal({"labels": {"namespace": env, "app": "payment", "alertname": "InventoryHigh5xxRate"}}, AppConfig(), None)
        assert sig is not None and sig.namespace == env
    assert alert_signal({"labels": {"namespace": "other", "app": "payment", "alertname": "InventoryHigh5xxRate"}}, AppConfig(), None) is None


def test_overview_environment_order_missing_and_system_groups():
    from app.overview import assemble
    labels = {"app.kubernetes.io/part-of": "payment"}
    inv = {"namespaces": [{"metadata": {"name": "dev"}}, {"metadata": {"name": "prd"}}],
           "deployments": [{"metadata": {"namespace": "dev", "name": "payment", "labels": labels}, "spec": {"replicas": 0}}],
           "pods": [], "services": []}
    d = assemble(inv, {}, cfg=AppConfig())
    envs = d["cluster"]["environments"]
    assert [e["name"] for e in envs] == ["dev", "stg", "prd"]
    assert envs[0]["workloads"] == 1 and envs[0]["order"] == 1
    assert envs[1]["missing"] is True and envs[1]["workloads"] is None
    assert envs[2]["workloads"] == 0
    assert d["workloads"][0]["system"] == "payment"
    assert d["workloads"][0]["environment"] == "dev"
    node = next(n for n in d["topology"]["nodes"] if n["kind"] == "Deployment")
    assert node["system"] == "payment" and node["environment"] == "dev"
    assert {"id": "dev/system/payment", "kind": "system", "environment": "dev", "system": "payment", "label": "payment"} in d["topology"]["groups"]


def test_managed_labels_replace_fixed_deployment_allowlist():
    import pytest
    from app.actions import validate, Denied
    from conftest import FakeKube, mk_dep
    k = FakeKube()
    dep = mk_dep("payment", 1, "img")
    dep["metadata"].update(uid="uid", resourceVersion="1", namespace="prd", labels={"liveprobe.io/managed": "true", "app.kubernetes.io/part-of": "payment"})
    dep["spec"]["template"]["metadata"]["labels"].update(dep["metadata"]["labels"])
    k.deployments["payment"] = dep
    cfg = AppConfig()
    assert validate(cfg, k, "rollout_restart", "prd", "Deployment", "payment", {}, system="payment").workload == "payment"
    with pytest.raises(Denied, match="system"):
        validate(cfg, k, "rollout_restart", "prd", "Deployment", "payment", {}, system="other")
    dep["metadata"]["labels"].pop("liveprobe.io/managed")
    with pytest.raises(Denied, match="managed"):
        validate(cfg, k, "rollout_restart", "prd", "Deployment", "payment", {})
    cfg.safe_actions.managed_label = {"custom/managed": "yes"}
    dep["metadata"]["labels"]["custom/managed"] = "yes"
    assert validate(cfg, k, "rollout_restart", "prd", "Deployment", "payment", {}).name == "payment"


def test_exception_and_5xx_are_one_workload_incident_in_both_orders(db_url):
    from app import db
    from app.incidents import ingest, Signal
    from app.models import Incident
    from sqlalchemy import select
    cfg = AppConfig()
    with db.session() as s:
        for env, reasons in [('dev', ['ApplicationException', 'OrderHigh5xxRate']), ('prd', ['OrderHigh5xxRate', 'ApplicationException'])]:
            for reason in reasons:
                inc, _ = ingest(s, cfg, Signal(env, 'Deployment', 'order', reason, 'test'))
            assert inc.workload == 'order'
            assert set(inc.signal_reasons) == set(reasons)
        assert len(list(s.scalars(select(Incident)))) == 2


def test_0009_migration_roundtrip_preserves_legacy_incidents(db_url):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text
    from app import db
    from app.models import Incident
    from conftest import ROOT
    cfg = Config(str(ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(ROOT / 'alembic'))
    cfg.attributes['sqlalchemy_url'] = db_url
    with db.session() as s:
        s.add(Incident(fingerprint='f', namespace='prd', workload='payment', system='payment', title='legacy'))
    command.downgrade(cfg, '0008')
    assert 'system' not in {c['name'] for c in inspect(db.engine()).get_columns('incidents')}
    command.upgrade(cfg, '0009')
    with db.engine().connect() as c:
        assert c.execute(text('select system from incidents')).scalar_one() is None
        assert c.execute(text('select title from incidents')).scalar_one() == 'legacy'
    command.upgrade(cfg, 'head')


def test_system_missing_on_incident_must_deny_execution(db_url):
    from app import db
    from app.actions import propose_typed
    from app.models import Incident
    from test_typed_actions import ExecKube
    k = ExecKube()
    cfg = AppConfig(target_namespace='liveprobe-target')
    with db.session() as s:
        inc = Incident(fingerprint='f', namespace='liveprobe-target', workload='inventory', system=None, title='unknown', severity_level='SIMPLE')
        s.add(inc)
        s.flush()
        act = propose_typed(s, cfg, k, inc, 'SIMPLE', 'rollout_restart', 'Deployment', 'inventory')
        assert act.status == 'denied' and 'system' in act.reason


def test_api_environment_system_and_remediation_contract(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, db, main, overview
    from app.models import Incident
    monkeypatch.setenv('LIVEPROBE_DATABASE_URL', db_url)
    monkeypatch.setenv('LIVEPROBE_BACKGROUND', 'false')
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    remediation = {'kind': 'code_fix', 'why': '예외 관측', 'code_refs': [{'file': '/app/PaymentService.py', 'line': 42, 'symbol': 'pay'}], 'repro': None, 'source': 'rules'}
    with db.session() as s:
        inc = Incident(fingerprint='f', namespace='prd', workload='payment', system='payment', title='error', handoff={'remediation': remediation})
        s.add(inc)
        s.flush()
        iid = inc.id
    class K:
        def cluster_inventory(self):
            return {'namespaces': [{'metadata': {'name': 'prd'}}], 'pods': [], 'deployments': []}
        def cluster_warning_events(self): return []
    monkeypatch.setattr(overview, 'fetch_metrics', lambda *a: ({}, {}))
    with TestClient(main.app, base_url='http://liveprobe.localhost') as client:
        monkeypatch.setitem(main.STATE, 'kube', K())
        main._TOPO_CACHE.clear()
        for path, key in [('/api/incidents', 'items'), (f'/api/incidents/{iid}', 'incident'), (f'/api/incidents/{iid}/narrative', 'incident')]:
            response = client.get(path)
            assert response.status_code == 200
            row = response.json()[key]
            row = row[0] if isinstance(row, list) else row
            assert row['system'] == 'payment' and row['environment'] == 'prd'
        narrative = client.get(f'/api/incidents/{iid}/narrative').json()
        assert '코드 수정 필요 — PaymentService.py:42' in narrative['headline']['what']
        assert narrative['headline']['remediation'] == remediation
        assert client.get('/api/overview').json()['incidents']['open'][0]['system'] == 'payment'
        assert client.get('/api/topology').json()['incidents'][0]['environment'] == 'prd'
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()

