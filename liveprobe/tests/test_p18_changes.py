"""P18: real PostgreSQL lifecycle, fake external transports, no cluster writes."""
import copy
import hashlib
import json
from types import SimpleNamespace as N

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.config import AppConfig, Settings
from app.llm.base import LLMResult
from app.models import Incident, Evidence, AuditLog

DIFF = '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-bad\n+good\n--- /dev/null\n+++ b/tests/test_fix.py\n@@ -0,0 +1 @@\n+assert True\n'


def test_state_machine_rejects_skipped_stages():
    from app.changes import transition, ChangeError
    c = N(status='built')
    with pytest.raises(ChangeError):
        transition(c, 'prd_deploying')
    transition(c, 'dev_deploying')
    transition(c, 'dev_verified')
    transition(c, 'stg_deploying')
    transition(c, 'stg_verified')
    transition(c, 'prd_deploying')
    transition(c, 'prd_verified')
    with pytest.raises(ChangeError):
        transition(c, 'cancelled')


class Planner:
    name = 'fake'
    def __init__(self):
        self.diff, self.calls = DIFF, []
    def complete(self, role, prompt, schema, model, effort, timeout):
        self.calls.append((role, prompt, model, effort))
        return LLMResult('', {'diff': self.diff, 'summary_ko': '예외 처리와 회귀 시험 추가', 'tests_added': ['tests/test_fix.py'], 'risk_ko': '낮음'})


class Release:
    def __init__(self, kube): self.kube, self.calls = kube, []
    def patch_image(self, ns, service, before, after, resource_version):
        d = self.kube.deployments[ns]
        assert d['metadata']['resourceVersion'] == resource_version
        assert d['spec']['template']['spec']['containers'][0]['image'] == before
        self.calls.append((ns, service, before, after, resource_version))
        d['spec']['template']['spec']['containers'][0]['image'] = after
        d['metadata']['resourceVersion'] = str(int(resource_version) + 1)


@pytest.fixture
def setup(db_url):
    from app.changes import ChangeService
    from conftest import FakeKube, mk_dep, mk_pod
    k = FakeKube()
    k.deployments = {ns: mk_dep('payment', 1, 'local/payment:base') for ns in ('dev', 'stg', 'prd')}
    for ns, d in k.deployments.items():
        d['metadata'].update(uid='uid-' + ns, resourceVersion='1', namespace=ns, labels={'app.kubernetes.io/part-of': 'payment', 'liveprobe.io/managed': 'true'})
    k.get_deployment = lambda ns, name: copy.deepcopy(k.deployments[ns])
    k.pods = [mk_pod('payment-a', 'payment', None)]
    k.services['payment'] = {'spec': {'ports': [{'port': 80}]}}
    with db.session() as s:
        i = Incident(fingerprint='p18', namespace='prd', workload='payment', title='예외', status='handed_off', handoff={'remediation': {'kind': 'code_fix', 'repro': {'method': 'GET', 'path': '/pay', 'query': {'id': '1'}}}})
        if hasattr(Incident, 'system'): i.system = 'payment'
        s.add(i); s.flush(); iid = i.id
        s.add(Evidence(incident_id=iid, source='loki', payload={'logs': ['File "app.py", line 1: ValueError']}))
    p, r = Planner(), Release(k)
    cfg = AppConfig(runner_systems={'payment': ['payment']})
    svc = ChangeService(cfg, Settings(), p, k, release=r, sleep=lambda _: None, logs=lambda *a: 0, reproduce=lambda *a: True)
    return N(service=svc, kube=k, planner=p, release=r, iid=iid)


def built(x):
    c = x.service.create_for_incident(x.iid, 'operator')
    job = x.service.claim()
    assert job['kind'] == 'fetch_source'
    x.service.report(job['id'], {'base_commit': 'a' * 40, 'files': {'app.py': 'bad\n'}})
    job = x.service.claim()
    assert job['kind'] == 'build'
    x.service.report(job['id'], {'ok': True, 'commit': 'b' * 40, 'branch': f"fix/cr-{c['id']}", 'image': f"local/payment:cr-{c['id']}", 'image_id': 'sha256:' + 'a' * 64, 'repository': 'live-probe/payment', 'pr_number': 4, 'pr_url': 'https://github.com/live-probe/payment/pull/4', 'head_sha': 'b' * 40, 'source_tree': 'd' * 40, 'tests': {'total': 1, 'failed': 0, 'report_tail': '통과'}})
    return x.service.get(c['id'])


def test_fetch_patch_build_and_ordered_promotions(setup):
    x = setup; c = built(x)
    assert c['status'] == 'built'
    assert c['source_files'][0] == {'path': 'app.py', 'size': 4, 'sha256': hashlib.sha256(b'bad\n').hexdigest()}
    assert x.planner.calls[0][0::2] == ('code_fixer', 'gpt-6.1-sol')
    assert 'ValueError' in x.planner.calls[0][1]
    for stage in ('dev', 'stg', 'prd'):
        if stage == 'prd':
            x.service.review(c['id'], {'repository': 'live-probe/payment', 'pr_number': 4,
                'pr_url': 'https://github.com/live-probe/payment/pull/4', 'head_sha': 'b' * 40,
                'state': 'MERGED', 'merge_sha': 'c' * 40, 'main_sha': 'c' * 40, 'review_completed': True,
                'merge_build': {'commit': 'c' * 40, 'image': 'local/payment:merged', 'image_id': 'sha256:' + 'c' * 64,
                                'source_tree': 'd' * 40, 'tests': {'total': 1, 'failed': 0}}})
        plan = x.service.plan(c['id'], stage)
        assert plan['previous_image'] == 'local/payment:base' and plan['side_effects_ko']
        x.service.promote(c['id'], stage, plan['plan_sha256'], True, 'operator')
        x.service.execute_stage(c['id'], stage)
        c = x.service.get(c['id'])
        assert c['status'] == stage + '_verified'
        assert c['stages'][stage]['verification']['exceptions']['count'] == 0
    assert len(x.release.calls) == 3
    with db.session() as s:
        events = {a.event for a in s.scalars(select(AuditLog).where(AuditLog.incident_id == x.iid))}
        assert {'change_created', 'patch_generated', 'build_reported', 'promotion_confirmed', 'stage_verified'} <= events


def test_unknown_registration_is_explicit_error(setup):
    setup.service.cfg.runner_systems = {}
    c = setup.service.create_for_incident(setup.iid, 'operator')
    assert c['status'] == 'error' and c['error'] == '소스 저장소 미등록'
    assert setup.service.claim() is None


@pytest.mark.parametrize('diff,added', [
    (DIFF.replace('b/app.py', 'b/../evil.py'), ['tests/test_fix.py']),
    (DIFF.replace('b/app.py', 'b/outside.py'), ['tests/test_fix.py']),
    (DIFF.split('--- /dev/null')[0], []),
    (DIFF.replace('tests/test_fix.py', 'evil.py'), ['evil.py']),
    (DIFF.replace('--- a/app.py', 'new file mode 120000\n--- a/app.py'), ['tests/test_fix.py']),
])
def test_patch_validation_rejects_scope_escape_and_missing_tests(diff, added):
    from app.changes import validate_diff, ChangeError
    with pytest.raises(ChangeError): validate_diff(diff, {'app.py': 'bad\n'}, added)


def test_claim_and_report_are_exactly_once_and_source_bodies_not_stored(setup):
    from app.change_models import RunnerJob
    from app.changes import ChangeError
    x = setup; c = x.service.create_for_incident(x.iid, 'operator')
    job = x.service.claim(); assert x.service.claim() is None
    x.service.report(job['id'], {'base_commit': 'a' * 40, 'files': {'app.py': 'bad\n'}})
    with pytest.raises(ChangeError): x.service.report(job['id'], {'files': {}})
    with db.session() as s:
        stored = s.get(RunnerJob, job['id'])
        assert 'bad' not in json.dumps(stored.result)
    assert x.service.get(c['id'])['status'] == 'awaiting_build'


@pytest.mark.parametrize('size,expected', [(65537, 'patch_failed'), (65536, 'awaiting_build')])
def test_source_file_byte_limit(setup, size, expected):
    x = setup; c = x.service.create_for_incident(x.iid, 'operator'); job = x.service.claim()
    x.service.report(job['id'], {'base_commit': 'a' * 40, 'files': {'app.py': 'x' * size}})
    assert x.service.get(c['id'])['status'] == expected


def test_loki_unavailable_fails_and_rolls_back_with_receipt(setup):
    x = setup; c = built(x)
    def unavailable(*_): raise RuntimeError('secret response must not persist')
    x.service.logs = unavailable
    p = x.service.plan(c['id'], 'dev'); x.service.promote(c['id'], 'dev', p['plan_sha256'], True)
    c = x.service.execute_stage(c['id'], 'dev')
    assert c['status'] == 'dev_failed' and c['stages']['dev']['reverted'] is True
    assert c['stages']['dev']['verification']['exceptions']['status'] == '미관측'
    assert len(x.release.calls) == 2


def test_no_repro_is_recorded_not_fabricated_as_pass(setup):
    x = setup
    out = x.service.verify('dev', 'payment', 'local/payment:base', None)
    assert out['repro']['status'] == '미수행' and out['ok']


def test_stale_hash_order_confirm_cancel_and_restart_are_fail_closed(setup):
    from app.changes import ChangeError
    x = setup; c = built(x)
    with pytest.raises(ChangeError): x.service.plan(c['id'], 'stg')
    p = x.service.plan(c['id'], 'dev')
    with pytest.raises(ChangeError): x.service.promote(c['id'], 'dev', p['plan_sha256'], False)
    with pytest.raises(ChangeError): x.service.promote(c['id'], 'dev', '0' * 64, True)
    x.kube.deployments['dev']['spec']['template']['spec']['containers'][0]['image'] = 'concurrent:edit'
    with pytest.raises(ChangeError): x.service.promote(c['id'], 'dev', p['plan_sha256'], True)
    x.kube.deployments['dev']['spec']['template']['spec']['containers'][0]['image'] = p['previous_image']
    x.service.promote(c['id'], 'dev', p['plan_sha256'], True)
    with pytest.raises(ChangeError): x.service.cancel(c['id'])
    x.service.recover()
    c = x.service.get(c['id']); assert c['status'] == 'dev_failed' and '재시작' in c['error']
    with pytest.raises(ChangeError): x.service.execute_stage(c['id'], 'dev')
    assert not x.release.calls


def test_prd_requires_reviewed_exact_merge_artifact(setup):
    from app.changes import ChangeError
    from app.change_models import ChangeRequest
    x = setup; c = built(x)
    with db.session() as s: s.get(ChangeRequest, c['id']).status = 'stg_verified'
    with pytest.raises(ChangeError, match='github_merge_required'): x.service.plan(c['id'], 'prd')
    x.service.review(c['id'], {'repository': 'live-probe/payment', 'pr_number': 4, 'pr_url': 'https://github.com/live-probe/payment/pull/4',
        'head_sha': 'b' * 40, 'state': 'MERGED', 'merge_sha': 'c' * 40, 'main_sha': 'c' * 40, 'review_completed': True,
        'merge_build': {'commit': 'c' * 40, 'image': 'local/payment:merged', 'image_id': 'sha256:' + 'c' * 64,
                        'source_tree': 'd' * 40, 'tests': {'total': 1, 'failed': 0}}})
    p = x.service.plan(c['id'], 'prd')
    assert p['new_image'] == 'local/payment:merged'
    assert x.service.get(c['id'])['build']['merge_sha'] == 'c' * 40


def test_code_fixer_model_settings_are_saved_and_resolved(db_url):
    from app.model_settings import ModelSettingsIn, read_settings, save_settings
    cfg = AppConfig()
    with db.session() as s:
        data = read_settings(s, cfg)
        assert data['assist']['code_fixer'] == {'model': 'gpt-6.1-sol', 'effort': 'high'}
        body = ModelSettingsIn(expected_revision=0, router=data['router'], groups=data['groups'], assist={'code_fixer': {'model': 'gpt-6-astra', 'effort': 'high'}})
        result = save_settings(s, cfg, body, 'operator')
        assert result['assist']['code_fixer']['model'] == 'gpt-6-astra'


def test_runner_real_git_roundtrip_and_no_original_mutation(tmp_path):
    import importlib.util
    import subprocess
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('p18_runner', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-runner.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    source = tmp_path / 'original'; source.mkdir()
    (source / 'app.py').write_text('bad\n')
    def git(*args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost', *args], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main'); git('add', '.'); git('commit', '-m', 'base')
    original_sha = git('rev-parse', 'HEAD')
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        if args[:3] == ['gh', 'repo', 'clone']:
            return subprocess.run(['git', 'clone', str(source), args[4]], **kwargs)
        if args[:3] == ['gh', 'pr', 'create']:
            return subprocess.CompletedProcess(args, 0, 'https://github.com/live-probe/payment/pull/4\n', '')
        if args[:3] == ['gh', 'pr', 'view']:
            head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=kwargs['cwd'], capture_output=True, text=True).stdout.strip()
            return subprocess.CompletedProcess(args, 0, json.dumps({'url': 'https://github.com/live-probe/payment/pull/4', 'number': 4, 'headRefOid': head, 'state': 'OPEN'}), '')
        if args[0] == 'git' and 'push' in args: return subprocess.CompletedProcess(args, 0, '', '')
        if args[:3] == ['docker', 'image', 'inspect']:
            return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'sha256:' + 'a' * 64, 'RepoDigests': ['local/payment@sha256:' + 'b' * 64]}]), '')
        return subprocess.run(args, **kwargs)
    cfg = {'api': 'http://localhost', 'token_file': 'unused', 'systems': {'payment': {'services': {'payment': {
        'github_repo': 'live-probe/payment', 'source_subdir': '.', 'base_branch': 'main',
        'work_repo': '.liveprobe-runner/repos/payment-payment', 'image_repo': 'local/payment',
        'test_cmd': "python3 -c \"print('Ran 1 test'); print('OK')\"", 'build_cmd': 'python3 -c "print(123)"'}}}}}
    r = runner.Runner(cfg, root=tmp_path, execute=execute)
    payload = {'cr_id': 8, 'system': 'payment', 'service': 'payment', 'paths': ['app.py']}
    fetched = r.fetch_source(payload)
    assert fetched['base_commit'] == original_sha and fetched['files'] == {'app.py': 'bad\n'}
    result = r.build({**payload, 'base_commit': original_sha, 'diff': DIFF, 'diff_sha256': hashlib.sha256(DIFF.encode()).hexdigest()})
    assert result['ok'] and result['tests']['total'] == 1 and result['tests']['failed'] == 0
    assert result['repository'] == 'live-probe/payment' and result['pr_number'] == 4
    assert result['image'] == 'local/payment@sha256:' + 'b' * 64
    assert result['commit'] != original_sha and result['head_sha'] == result['commit']
    assert git('rev-parse', 'HEAD') == original_sha and git('status', '--porcelain') == ''
    assert not any(args[:3] == ['gh', 'pr', 'merge'] for args in calls)


def test_runner_dry_run_never_claims_or_writes(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('p18_runner', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-runner.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    r = module.Runner({'systems': {}}, root=tmp_path, execute=lambda *_args, **_kwargs: pytest.fail('dry run executed a command'))
    assert r.once(dry_run=True) == {'dry_run': True, 'systems': []}
    assert not (tmp_path / '.liveprobe-runner').exists()


def test_untrusted_pr_receipts_cannot_unlock_prd(setup):
    from app.change_models import ChangeRequest
    from app.changes import ChangeError
    x = setup; c = built(x)
    with db.session() as s: s.get(ChangeRequest, c['id']).status = 'stg_verified'
    for state, merged, main_sha in [('OPEN', True, 'c' * 40), ('MERGED', False, 'c' * 40), ('MERGED', True, 'e' * 40)]:
        x.service.review(c['id'], {'repository':'live-probe/payment','pr_number':4,'pr_url':'https://github.com/live-probe/payment/pull/4','head_sha':'b'*40,
            'state':state,'review_completed':merged,'merge_sha':'c'*40,'main_sha':main_sha,
            'merge_build':{'commit':'c'*40,'image':'local/payment:merged','image_id':'sha256:'+'c'*64,'source_tree':'d'*40,'tests':{'total':1,'failed':0}}})
        with pytest.raises(ChangeError, match='github_merge_required'): x.service.plan(c['id'], 'prd')


def test_runner_http_plan_promote_confirm_and_no_token_fallback(setup, tmp_path):
    from app.change_api import change_router
    x = setup; c = built(x); app = FastAPI(); app.include_router(change_router({'changes':x.service}))
    with TestClient(app, base_url='http://liveprobe.localhost', headers={'X-LiveProbe-Client':'dashboard'}) as client:
        assert client.get(f"/api/changes?incident_id={x.iid}").json()['items'][0]['diff'] == DIFF
        p = client.get(f"/api/changes/{c['id']}/plan?stage=dev").json()
        assert client.post(f"/api/changes/{c['id']}/promote",json={'stage':'dev','plan_sha256':p['plan_sha256']}).status_code == 422
        assert not x.release.calls
        x.service.release = None
        r = client.post(f"/api/changes/{c['id']}/promote",json={'stage':'dev','plan_sha256':p['plan_sha256'],'confirm':True})
        assert r.status_code == 503 and r.json()['detail'] == 'release_executor_unavailable'
        x.service.release = x.release
        r = client.post(f"/api/changes/{c['id']}/promote",json={'stage':'dev','plan_sha256':p['plan_sha256'],'confirm':True})
        assert r.status_code == 200
        assert x.service.get(c['id'])['status'] == 'dev_verified'
        assert client.post(f"/api/changes/{c['id']}/promote",json={'stage':'dev','plan_sha256':p['plan_sha256'],'confirm':True}).status_code == 409


def test_git_style_regular_file_diff_is_supported_without_symlink_modes():
    from app.changes import validate_diff
    patch = DIFF.replace('--- a/app.py', 'diff --git a/app.py b/app.py\nindex 123abcd..456abcd 100644\n--- a/app.py').replace('--- /dev/null', 'diff --git a/tests/test_fix.py b/tests/test_fix.py\nnew file mode 100644\nindex 0000000..123abcd\n--- /dev/null')
    assert validate_diff(patch, {'app.py':'bad\n'}, ['tests/test_fix.py']) == ['app.py','tests/test_fix.py']


def test_missing_ready_condition_is_not_healthy(setup):
    setup.kube.pods[0]['status']['conditions'] = [{'type':'ContainersReady','status':'True'}]
    result = setup.service.verify('dev','payment','local/payment:base',None)
    assert not result['ok'] and result['health']['status'] == '실패'


def test_source_paths_prefer_e8_and_normalize_only_app_prefix(setup):
    x = setup
    with db.session() as s:
        s.add(Evidence(incident_id=x.iid,source='code_refs',payload={'frames':[{'file':'/app/handler.py','line':7,'symbol':'pay'}],'exceptions':[{'sample':'ValueError: bad'}]}))
    x.service.create_for_incident(x.iid,'operator')
    job=x.service.claim()
    assert job['payload']['paths'] == ['handler.py']


def test_runner_review_checks_human_merge_and_rebuilds_exact_tree(tmp_path):
    import importlib.util
    import subprocess
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('p18_runner',Path(__file__).resolve().parents[2]/'scripts/liveprobe-runner.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    cfg={'github_repo':'live-probe/payment','image_repo':'local/payment'}
    r=m.Runner({'systems':{}},root=tmp_path)
    r.prepare=lambda _: (cfg,tmp_path,'.','c'*40)
    called=[]
    def run(args,cwd,**kwargs):
        called.append(args)
        if args[:3]==['gh','pr','view']:
            return json.dumps({'url':'https://github.com/live-probe/payment/pull/4','number':4,'headRefOid':'b'*40,'state':'MERGED','mergeCommit':{'oid':'c'*40},'mergedBy':{'login':'reviewer'},'baseRefName':'main'})
        if args[:2]==['gh','api']: return 'User\n'
        if 'HEAD^{tree}' in args: return 'd'*40
        return ''
    r.run=run
    r.tests_and_image=lambda *a: {'source_tree':'d'*40,'image':'local/payment:merged','image_id':'sha256:'+'a'*64,'tests':{'total':1,'failed':0}}
    out=r.review({'id':8,'system':'payment','service':'payment','build':{'pr_number':4,'source_tree':'d'*40}})
    assert out['review_completed'] is True and out['merge_sha']==out['merge_build']['commit']=='c'*40
    assert ['gh','api','users/reviewer','--jq','.type'] in called


def test_explicit_build_failure_is_not_mislabeled_as_tests_failed(setup):
    x=setup;c=x.service.create_for_incident(x.iid,'operator');job=x.service.claim()
    x.service.report(job['id'],{'base_commit':'a'*40,'files':{'app.py':'bad\n'}})
    job=x.service.claim()
    x.service.report(job['id'],{'ok':False,'failure_kind':'build','tests':{'total':None,'failed':None}})
    assert x.service.get(c['id'])['status']=='build_failed'


def test_release_transport_exactly_three_image_only_ops(tmp_path, monkeypatch):
    from app.change_api import ReleaseKube
    from app import operator_kube
    calls = []
    deployment = {'metadata': {'resourceVersion': '7'}, 'spec': {'template': {'spec': {'containers': [
        {'name': 'sidecar', 'image': 'side'}, {'name': 'payment', 'image': 'old'}]}}}}
    class Client:
        def call_api(self, path, method, **kwargs):
            calls.append((path, method, kwargs))
            return deployment if method == 'GET' else {}
    monkeypatch.setattr(operator_kube, 'bound_api_client', lambda token_file: Client())
    t = tmp_path / 'token'; t.write_text('only-test')
    r = ReleaseKube(str(t)); r.patch_image('dev', 'payment', 'old', 'new', '7')
    assert calls[-1][2]['body'] == [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'},
        {'op': 'test', 'path': '/spec/template/spec/containers/1/image', 'value': 'old'},
        {'op': 'replace', 'path': '/spec/template/spec/containers/1/image', 'value': 'new'}]
    assert calls[-1][2]['header_params']['Content-Type'] == 'application/json-patch+json'


def test_runner_auth_is_distinct_from_dashboard_guard_and_redacts_token(setup, tmp_path):
    from app.change_api import change_router
    token = tmp_path / 'token'; token.write_text('runner-test-only')
    state = {'changes': setup.service}
    app = FastAPI(); app.include_router(change_router(state, settings=Settings(runner_token_file=str(token))))
    with TestClient(app, base_url='http://liveprobe.localhost') as c:
        assert c.post('/api/runner/claim').status_code == 401
        assert c.post('/api/runner/claim', headers={'X-LiveProbe-Client': 'dashboard'}).status_code == 401
        r = c.post('/api/runner/claim', headers={'Authorization': 'Bearer runner-test-only'})
        assert r.status_code == 204 and 'runner-test-only' not in r.text
        assert c.post('/api/changes/1/cancel').status_code == 403
        token.unlink()
        assert c.post('/api/runner/claim').status_code == 503
