#!/usr/bin/env python3
"""Outward-polling P18 runner. Only private clones are modified; never auto-merge PRs.

Requires git, docker and authenticated host gh. No credential is copied into a clone,
API result, command argument or log. --dry-run does not claim work or execute commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_FILE, MAX_TOTAL = 64 * 1024, 400 * 1024
EXCLUDED = {'.git', 'node_modules', 'target', 'dist', '.venv', '__pycache__', '.liveprobe-runner'}
EXTENSIONS = {'.py', '.js', '.mjs', '.ts', '.tsx', '.java', '.xml', '.json', '.gradle', '.properties', '.md'}


def safe_path(path):
    return (isinstance(path, str) and path and not path.startswith('/') and '\\' not in path
            and all(p not in ('', '.', '..', '.git') for p in path.split('/'))
            and not any(ord(c) < 32 for c in path))


def tail(text):
    value = re.sub(r'(?i)(bearer\s+|(?:token|password|secret|api_key)\s*[:=]\s*)\S+', r'\1[REDACTED]', str(text))
    return value.encode()[-8192:].decode(errors='replace')


class RunnerFailure(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__('runner_' + kind + '_failed')


class Runner:
    def __init__(self, config, *, root=None, execute=subprocess.run):
        self.config = config; self.root = Path(root or Path.cwd()).resolve(); self.execute = execute
        self.merge_cache = {}

    def run(self, args, cwd, *, input=None, timeout=600):
        result = self.execute(args, cwd=cwd, input=input, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
                                   'GH_PROMPT_DISABLED': '1', 'GIT_CONFIG_COUNT': '1',
                                   'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/dev/null'})
        if result.returncode:
            # No gh output or authentication diagnostics in exceptions or API responses.
            raise RuntimeError('runner_command_failed:' + args[0])
        return (result.stdout or '') + (result.stderr or '')

    def service(self, payload):
        cfg = self.config['systems'][payload['system']]['services'][payload['service']]
        repo = cfg.get('github_repo', '')
        if repo != 'live-probe/' + payload['system'] or not re.fullmatch(r'live-probe/[a-z0-9-]+', repo):
            raise ValueError('github_repository_not_registered')
        subdir = cfg.get('source_subdir', '.')
        if subdir != '.' and not safe_path(subdir): raise ValueError('unsafe_source_subdir')
        if cfg.get('base_branch', 'main') != 'main': raise ValueError('main_branch_required')
        work = self.root / cfg.get('work_repo', f".liveprobe-runner/repos/{payload['system']}-{payload['service']}")
        allowed = self.root / '.liveprobe-runner/repos'
        if not work.resolve().is_relative_to(allowed.resolve()) or work.resolve() == allowed.resolve(): raise ValueError('unsafe_work_repo')
        if any(p.is_symlink() for p in [work, *work.parents] if p != self.root and p.is_relative_to(self.root)):
            raise ValueError('symlink_work_repo')
        return cfg, work, subdir

    def prepare(self, payload):
        cfg, work, subdir = self.service(payload)
        if not (work / '.git').exists():
            work.parent.mkdir(parents=True, exist_ok=True)
            self.run(['gh', 'repo', 'clone', cfg['github_repo'], str(work), '--', '--no-recurse-submodules'], self.root)
        self.run(['git', 'fetch', '--no-tags', 'origin', 'main'], work)
        sha = self.run(['git', 'rev-parse', 'origin/main'], work).strip()
        if not re.fullmatch('[a-f0-9]{40}', sha): raise ValueError('invalid_main_sha')
        return cfg, work, subdir, sha

    @staticmethod
    def contained(work, subdir, path):
        if not safe_path(path): raise ValueError('unsafe_source_path')
        base = (work / subdir).resolve(); file = base / path
        if not file.resolve().is_relative_to(base) or any(p.is_symlink() for p in [file, *file.parents] if p != base and p.is_relative_to(base)):
            raise ValueError('symlink_source_path')
        return file

    def fetch_source(self, payload):
        cfg, work, subdir, sha = self.prepare(payload)
        self.run(['git', 'checkout', '--detach', sha], work)
        base = work / subdir
        paths = payload.get('paths') or []
        candidates = sorted(p for p in base.rglob('*') if p.is_file() and not p.is_symlink()
                            and not EXCLUDED.intersection(p.relative_to(base).parts)
                            and (p.suffix in EXTENSIONS or p.name == 'Dockerfile')
                            and not p.name.startswith('.') and not any(s in p.name.lower() for s in ('secret', 'credential', 'auth.json')))
        if paths:
            candidates = [p for p in candidates if any(str(p.relative_to(base)) == path or str(p.relative_to(base)).endswith('/' + path) for path in paths if safe_path(path))]
        files = {}; total = 0
        for p in candidates:
            path = str(p.relative_to(base)); self.contained(work, subdir, path)
            size = p.stat().st_size
            if size > MAX_FILE:
                if paths: raise ValueError('source_file_too_large')
                continue
            if total + size > MAX_TOTAL: break
            try: body = p.read_text(encoding='utf-8')
            except UnicodeError: continue
            if '\0' in body: continue
            files[path] = body; total += len(body.encode())
            if len(files) >= 200: break
        if not files: raise ValueError('source_paths_not_found')
        return {'base_commit': sha, 'repository': cfg['github_repo'], 'files': files}

    @staticmethod
    def check_diff(diff, base, subdir):
        if not isinstance(diff, str) or len(diff.encode()) > 200 * 1024: raise ValueError('invalid_diff')
        # Defense in depth: server owns full hunk validation; runner rejects dangerous git metadata.
        if re.search(r'(?m)^(?:new file mode (?!100644$)|old mode|new mode|rename |copy |GIT binary patch|Binary files)', diff):
            raise ValueError('unsafe_diff_metadata')
        for line in diff.splitlines():
            if line.startswith(('--- ', '+++ ')):
                value = line[4:]
                if value == '/dev/null': continue
                if not value.startswith(('a/', 'b/')): raise ValueError('unsafe_diff_path')
                Runner.contained(base, subdir, value[2:])

    def tests_and_image(self, cfg, work, subdir, image):
        context = (work / subdir).resolve()
        variables = {'image': image, 'dir': str(context)}
        test_cmd = cfg.get('test_cmd', '')
        try: test_log = self.run(shlex.split(test_cmd.format(**variables)), context) if test_cmd else ''
        except Exception: raise RunnerFailure('tests') from None
        build_cmd = cfg.get('build_cmd', '')
        if not build_cmd: raise ValueError('build_command_required')
        # Run tests before building and definitely before publishing a branch or PR.
        try: build_log = self.run(shlex.split(build_cmd.format(**variables)), context)
        except Exception: raise RunnerFailure('build') from None
        log = test_log + '\n' + build_log
        report = cfg.get('test_report')
        if report:
            file = self.contained(work, subdir, report)
            if file.stat().st_size > 64 * 1024: raise ValueError('test_report_too_large')
            tests = json.loads(file.read_text())
        else:
            unittest = re.search(r'Ran (\d+) tests?', log)
            passed = re.search(r'(\d+) passed', log)
            failed = re.search(r'(\d+) failed', log)
            node_total = re.search(r'(?:#|ℹ) tests (\d+)', log)
            node_failed = re.search(r'(?:#|ℹ) fail (\d+)', log)
            java = re.findall(r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)', log)
            if node_total:
                tests = {'total': int(node_total[1]), 'failed': int(node_failed[1]) if node_failed else None}
            elif java:
                tests = {'total': sum(int(a) for a, _, _ in java), 'failed': sum(int(b) + int(c) for _, b, c in java)}
            else:
                tests = {'total': int(unittest[1]) if unittest else (int(passed[1]) if passed else 0) + (int(failed[1]) if failed else 0),
                         'failed': int(failed[1]) if failed else 0}
        tests['report_tail'] = tail(test_log or build_log)
        if type(tests.get('total')) is not int or tests['total'] < 1 or tests.get('failed') != 0:
            raise RunnerFailure('tests')
        inspected = json.loads(self.run(['docker', 'image', 'inspect', image], work))
        item = inspected[0]; repo = cfg['image_repo']
        digest = next((d for d in item.get('RepoDigests', []) if re.fullmatch(re.escape(repo) + r'@sha256:[a-f0-9]{64}', d)), None)
        if not re.fullmatch('sha256:[a-f0-9]{64}', item['Id']): raise ValueError('invalid_image_id')
        return {'image': digest or image, 'image_id': item['Id'], 'tests': tests, 'log_tail': tail(log),
                'source_tree': self.run(['git', 'rev-parse', 'HEAD^{tree}'], work).strip()}

    def build(self, payload):
        cfg, work, subdir, latest = self.prepare(payload)
        if latest != payload['base_commit']: raise ValueError('base_main_changed')
        diff = payload['diff']
        if hashlib.sha256(diff.encode()).hexdigest() != payload.get('diff_sha256'): raise ValueError('diff_hash_mismatch')
        self.check_diff(diff, work, subdir)
        branch = f"fix/cr-{int(payload['cr_id'])}"
        self.run(['git', 'checkout', '-b', branch, payload['base_commit']], work)
        options = ['--directory=' + subdir] if subdir != '.' else []
        self.run(['git', 'apply', '--check', *options, '-'], work, input=diff)
        self.run(['git', 'apply', *options, '-'], work, input=diff)
        self.run(['git', 'add', '--all'], work)
        self.run(['git', '-c', 'user.name=LiveProbe Runner', '-c', 'user.email=runner@liveprobe.local',
                  'commit', '-m', f"Change request {payload['cr_id']}: regression fix"], work)
        commit = self.run(['git', 'rev-parse', 'HEAD'], work).strip()
        artifact = self.tests_and_image(cfg, work, subdir, f"{cfg['image_repo']}:cr-{int(payload['cr_id'])}")
        if self.run(['git', 'status', '--porcelain'], work).strip(): raise RunnerFailure('build')
        self.run(['git', '-c', 'http.version=HTTP/1.1', '-c', 'http.postBuffer=524288000', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential', 'push', '--set-upstream', 'origin', branch], work)
        url = self.run(['gh', 'pr', 'create', '--repo', cfg['github_repo'], '--base', 'main', '--head', branch,
                        '--title', f"변경 요청 #{int(payload['cr_id'])}: 회귀 시험을 포함한 코드 수정",
                        '--body', 'LiveProbe 개발 환경에서 시험과 이미지 빌드를 통과했습니다. GitHub에서 검토·병합하세요. 자동 병합하지 않습니다.'], work).strip()
        pr = json.loads(self.run(['gh', 'pr', 'view', url, '--repo', cfg['github_repo'], '--json', 'url,number,headRefOid,state'], work))
        if pr['headRefOid'] != commit or pr['state'] != 'OPEN': raise ValueError('pr_head_not_confirmed')
        return {'ok': True, 'commit': commit, 'head_sha': commit, 'branch': branch, 'repository': cfg['github_repo'],
                'pr_url': pr['url'], 'pr_number': pr['number'], **artifact}

    def request(self, path, payload=None, *, method=None):
        api = self.config['api'].rstrip('/'); parsed = urlsplit(api)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and (parsed.hostname in ('localhost', '127.0.0.1') or (parsed.hostname or '').endswith('.localhost'))):
            raise ValueError('runner_api_https_required')
        token = Path(self.config['token_file']).read_text().strip()
        if not token: raise ValueError('runner_token_unavailable')
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        req = Request(api + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
        with urlopen(req, timeout=240) as response:
            if response.status == 204: return None
            raw = response.read(3 * 1024 * 1024 + 1)
            if len(raw) > 3 * 1024 * 1024: raise ValueError('api_response_too_large')
            return json.loads(raw)

    def review(self, change):
        payload = {'system': change['system'], 'service': change['service']}
        cfg, work, subdir, main_sha = self.prepare(payload)
        built = change['build']
        pr = json.loads(self.run(['gh', 'pr', 'view', str(built['pr_number']), '--repo', cfg['github_repo'], '--json',
                                 'url,number,state,headRefOid,mergeCommit,mergedBy,reviewDecision,baseRefName'], work))
        out = {'repository': cfg['github_repo'], 'pr_url': pr['url'], 'pr_number': pr['number'], 'head_sha': pr['headRefOid'],
               'state': pr['state'], 'main_sha': main_sha, 'review_completed': False}
        if pr['state'] == 'MERGED' and pr.get('mergedBy') and pr.get('baseRefName') == 'main':
            login = pr['mergedBy'].get('login', '')
            if not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login): return out
            if self.run(['gh', 'api', 'users/' + login, '--jq', '.type'], work).strip() != 'User': return out
            sha = (pr.get('mergeCommit') or {}).get('oid')
            if sha == main_sha:
                key = (change['id'], sha)
                if key not in self.merge_cache:
                    self.run(['git', 'checkout', '--detach', sha], work)
                    tree = self.run(['git', 'rev-parse', 'HEAD^{tree}'], work).strip()
                    if tree != built.get('source_tree'): return out
                    artifact = self.tests_and_image(cfg, work, subdir, f"{cfg['image_repo']}:cr-{change['id']}-merged-{sha[:12]}")
                    self.merge_cache[key] = {'commit': sha, **artifact}
                out.update(merge_sha=sha, review_completed=True, merge_build=self.merge_cache[key])
        return out

    def sync_reviews(self):
        changes = self.request('/api/runner/changes', method='GET')
        for c in changes.get('items', []):
            if c['status'].endswith('_deploying') or c['status'] in ('prd_verified', 'cancelled') or not c.get('build', {}).get('pr_number'): continue
            try: self.request(f"/api/runner/changes/{c['id']}/review", self.review(c))
            except Exception:
                # Absence of a fresh merge receipt blocks prd; never manufacture GitHub success.
                continue

    def once(self, *, dry_run=False):
        registry = {system: list(value['services']) for system, value in self.config.get('systems', {}).items()}
        if dry_run: return {'dry_run': True, 'systems': sorted(registry)}
        self.sync_reviews()
        envelope = self.request('/api/runner/claim', {'systems': registry})
        if not envelope: return None
        job = envelope['job']
        try:
            result = self.fetch_source(job['payload']) if job['kind'] == 'fetch_source' else self.build(job['payload'])
        except Exception as exc:
            result = {'ok': False, 'error': type(exc).__name__, 'failure_kind': getattr(exc, 'kind', 'build'), 'tests': {'total': None, 'failed': None}, 'log_tail': '러너 작업 실패. 호스트 설정과 시험을 확인하세요.'}
        return self.request(f"/api/runner/jobs/{job['id']}/result", result)


def main():
    parser = argparse.ArgumentParser(description='LiveProbe 개발 환경 러너')
    parser.add_argument('--config', default='.liveprobe-runner/config.json')
    parser.add_argument('--once', action='store_true'); parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config_path = Path(args.config).resolve(); runner = Runner(json.loads(config_path.read_text()), root=config_path.parent.parent)
    if args.dry_run:
        print(json.dumps(runner.once(dry_run=True), ensure_ascii=False)); return
    import fcntl
    lock_path = config_path.parent / 'runner.lock'
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try: runner.once()
            except Exception as exc: print('러너 통신 실패: ' + type(exc).__name__)
            if args.once: break
            time.sleep(5)


if __name__ == '__main__': main()
