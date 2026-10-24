"""P18 runner trust boundary: fake credentials only; no external writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location('sandbox_runner', Path(__file__).resolve().parents[2] / 'scripts/liveprobe-runner.py')
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
PYTHON = 'python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e'
NODE = 'node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402'


def config(**kw):
    return dict(test_image=PYTHON, test_cmd='python test_probe.py', build_cmd='docker build --pull=false -t {image} {dir}', image_repo='local/p18', **kw)


def test_runner_java_target_package_included_and_outputs_excluded(tmp_path):
    files = ['pom.xml', 'target/classes/Generated.java', 'module/pom.xml', 'module/target/classes/Generated.java',
             'src/main/java/io/liveprobe/target/App.java', 'src/test/java/io/liveprobe/target/AppTest.java',
             'module/src/main/java/io/liveprobe/target/App.java', '.git/config', 'node_modules/unsafe.js']
    for name in files:
        p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('<project/>' if name.endswith('pom.xml') else 'class App {}')
    (tmp_path / 'escaped.java').symlink_to(tmp_path.parent / 'outside.java')
    r = m.Runner({}, root=tmp_path, execute=lambda args, **kw: subprocess.CompletedProcess(args, 0, '', ''))
    r.prepare = lambda _: ({'github_repo': 'live-probe/test'}, tmp_path, '.', 'a' * 40)
    wanted = [f for f in files if '/src/' in '/' + f]
    all_files = r.fetch_source({})['files']
    assert all(p in all_files for p in wanted)
    assert not any(p in all_files for p in files if p not in wanted and not p.endswith('pom.xml'))
    for p in wanted:
        assert p in r.fetch_source({'paths': [p]})['files']


@pytest.mark.parametrize('override', [
    {'test_image': None}, {'test_image': 'python:latest'}, {'test_image': PYTHON + ' bad'},
    {'build_cmd': 'python -c "print(1)"'}, {'build_cmd': 'bash -c true'},
    {'build_cmd': 'docker build --secret id=x -t {image} {dir}'},
    {'build_cmd': 'docker build --ssh default -t {image} {dir}'},
    {'build_cmd': 'docker build --privileged -t {image} {dir}'},
    {'build_cmd': 'docker build --allow security.insecure -t {image} {dir}'},
    {'build_cmd': 'docker build --build-arg TOKEN -t {image} {dir}'},
    {'build_cmd': 'docker build -f ../Dockerfile -t {image} {dir}'},
    {'build_cmd': 'docker build --network=host -t {image} {dir}'},
    {'test_report': 'report.json'}, {'test_cmd': ''},
])
def test_runner_unsafe_configuration_fails_closed_without_execution(tmp_path, override):
    calls = []
    r = m.Runner({}, root=tmp_path, execute=lambda args, **kw: calls.append(args))
    cfg = config(); cfg.update(override)
    with pytest.raises((ValueError, m.RunnerFailure)):
        r.tests_and_image(cfg, tmp_path, '.', 'local/p18:test')
    assert calls == []


def test_runner_java_sandbox_structure_and_no_host_fallback(tmp_path):
    (tmp_path / 'pom.xml').write_text('<project/>')
    (tmp_path / 'Dockerfile').write_text('FROM scratch\n')
    (tmp_path / '.env').write_text('FAKE')
    (tmp_path / 'auth.json').write_text('FAKE')
    calls = []
    def execute(args, **kw):
        calls.append(args)
        assert args[:2] == ['docker', 'run']
        assert args[args.index('--user') + 1] == '10001:10001'
        for flag in ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pull=never']:
            assert flag in args
        assert any(x.startswith('--pids-limit=') for x in args)
        assert any(x.startswith('--memory=') for x in args)
        assert any(x.startswith('--cpus=') for x in args)
        mounts = [args[i + 1] for i, x in enumerate(args) if x == '--mount']
        assert len(mounts) == 1 and mounts[0].endswith(',target=/source,readonly')
        stage = Path(mounts[0].split('src=')[1].split(',')[0])
        assert (stage / 'pom.xml').exists() and not (stage / '.env').exists() and not (stage / 'auth.json').exists()
        assert 'mvn' in args and 'test' in args
        assert not any('docker.sock' in a or str(Path.home()) == a for a in args)
        return subprocess.CompletedProcess(args, 1, '', 'offline Maven dependencies unavailable')
    cfg = config(); cfg.update(test_cmd='mvn -o test', test_image='maven:3@sha256:' + 'a' * 64)
    with pytest.raises(m.RunnerFailure, match='tests'):
        m.Runner({}, root=tmp_path, execute=execute).tests_and_image(cfg, tmp_path, '.', 'local/p18:test')
    assert len(calls) == 1


@pytest.mark.parametrize('runtime,image', [('python', PYTHON), ('node', NODE)])
def test_runner_actual_docker_sentinel_isolation(tmp_path, monkeypatch, runtime, image):
    # Resolve the active daemon with the real HOME first; a fake HOME would otherwise hide the CLI context.
    endpoint = subprocess.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
                              capture_output=True, text=True, timeout=30)
    if endpoint.returncode or not endpoint.stdout.strip().startswith('unix://'):
        pytest.skip('로컬 Docker 데몬 없음: 실제 격리 시험 미수행')
    monkeypatch.setenv('DOCKER_HOST', endpoint.stdout.strip())
    home = tmp_path / 'fake-home'; home.mkdir()
    forbidden = [home / '.config/gh/hosts.yml', home / '.codex/auth.json', tmp_path / '.liveprobe-runner/token', tmp_path / 'parent-sentinel']
    for p in forbidden:
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text('FAKE-P18-ONLY')
    monkeypatch.setenv('HOME', str(home)); monkeypatch.setenv('P18_FAKE_CREDENTIAL', 'FAKE-P18-ONLY')
    work = tmp_path / 'work'; work.mkdir()
    (work / '.env').write_text('FAKE-P18-ONLY'); (work / 'auth.json').write_text('FAKE-P18-ONLY')
    (work / 'Dockerfile').write_text('FROM scratch\n')
    paths = [str(p) for p in forbidden] + ['/var/run/docker.sock', '/source/../parent-sentinel', '/source/.env', '/source/auth.json', '/source/.git/config']
    if runtime == 'python':
        name = 'test_probe.py'
        script = 'import os,json,pathlib\npaths=' + repr(paths) + '\n'
        script += 'checks={p:pathlib.Path(p).exists() for p in paths}\nchecks["env"]="P18_FAKE_CREDENTIAL" in os.environ\nchecks["uid"]=os.getuid()!=10001\nchecks["workspace_write"]=not os.access("/workspace",os.W_OK)\nchecks["source_write"]=os.access("/source",os.W_OK)\nprint("ISOLATION="+json.dumps(checks))\nassert not any(checks.values())\nprint("Ran 1 test\\nOK")\n'
        cmd = 'python test_probe.py'
    else:
        name = 'test_probe.cjs'
        script = 'const fs=require("fs");const paths=' + json.dumps(paths) + ';\n'
        script += 'const checks=Object.fromEntries(paths.map(p=>[p,fs.existsSync(p)]));checks.env="P18_FAKE_CREDENTIAL" in process.env;checks.uid=process.getuid()!==10001;let writable=true;try{fs.accessSync("/source",fs.constants.W_OK)}catch{writable=false}checks.source_write=writable;fs.writeFileSync("/workspace/write-proof","ok");console.log("ISOLATION="+JSON.stringify(checks));if(Object.values(checks).some(Boolean))process.exit(1);console.log("# tests 1\\n# fail 0");'
        cmd = 'node test_probe.cjs'
    (work / name).write_text(script)
    observed = []; calls = []
    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ['docker', 'run']:
            # The fake HOME must not leak into the container: docker run forwards no host env and the entrypoint uses env -i.
            result = subprocess.run(args, **kwargs)
            observed.extend(json.loads(line[10:]) for line in result.stdout.splitlines() if line.startswith('ISOLATION='))
            if result.returncode: print('SANDBOX_STDERR=' + result.stderr[-2000:])
            return result
        if args[:2] == ['docker', 'build']:
            return subprocess.CompletedProcess(args, 0, '', '')
        if args[:3] == ['docker', 'image', 'inspect']:
            return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': 'sha256:' + 'a'*64}]), '')
        assert args[0] == 'git'
        return subprocess.CompletedProcess(args, 0, 'd'*40, '')
    cfg = config(); cfg.update(test_image=image, test_cmd=cmd)
    result = m.Runner({}, root=tmp_path, execute=execute).tests_and_image(cfg, work, '.', 'local/p18:test')
    assert result['tests']['total'] == 1 and result['tests']['failed'] == 0
    assert len(observed) == 1 and not any(observed[0].values())
    assert any(a[:2] == ['docker', 'run'] for a in calls)


def test_runner_review_main_moves_during_rebuild_no_receipt(tmp_path):
    r = m.Runner({}, root=tmp_path)
    cfg = {'github_repo': 'live-probe/test', 'image_repo': 'local/p18'}
    r.prepare = lambda _: (cfg, tmp_path, '.', 'c'*40)
    def run(args, cwd, **kw):
        if args[:3] == ['gh', 'pr', 'view']:
            return json.dumps(dict(url='url', number=1, headRefOid='b'*40, state='MERGED', mergeCommit={'oid':'c'*40}, mergedBy={'login':'human'}, baseRefName='main'))
        if args[:2] == ['gh', 'api']: return 'User'
        if 'HEAD^{tree}' in args: return 'd'*40
        if 'origin/main' in args: return 'e'*40
        return ''
    r.run = run
    r.tests_and_image = lambda *a: dict(source_tree='d'*40, tests={'total':1, 'failed':0})
    out = r.review(dict(id=1, system='test', service='test', build={'pr_number':1, 'source_tree':'d'*40}))
    assert out['review_completed'] is False and 'merge_build' not in out
