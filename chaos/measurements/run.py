"""Disposable kind signal measurements, NOT a v1 case runner.

Only accepts the private kubeconfig created for kind-kan110-measure. Does not
read Secrets, touch other contexts, or claim application-contract success.
"""
import datetime
import json
import os
from pathlib import Path
import statistics
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
KUBECONFIG = HERE / 'kubeconfig'
CONTEXT = 'kind-kan110-measure'
NS = 'kan110-measure'
OUT = ROOT / 'docs/evidence/KAN-110'


def cmd(*args, input=None, timeout=40, check=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args[:6])}: {result.stderr[:400]}")
    return result.stdout


def k(*args, input=None, timeout=40):
    if not KUBECONFIG.exists():
        raise RuntimeError('isolated kubeconfig missing')
    return cmd('kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT,
               '-n', NS, *args, input=input, timeout=timeout)


def obj(kind, name):
    return json.loads(k('get', kind, name, '-o', 'json'))


def apply(value):
    k('apply', '-f', '-', input=json.dumps(value))


def pod(name, image, command=None, *, memory=None, restart='Never', env=None, volume=False):
    container = {'name': name, 'image': image, 'imagePullPolicy': 'IfNotPresent'}
    if command:
        container['command'] = command
    if memory:
        container['resources'] = {'requests': {'memory': '16Mi'}, 'limits': {'memory': memory}}
    if env:
        container['env'] = [{'name': key, 'value': value} for key, value in env.items()]
    if volume:
        container['volumeMounts'] = [{'name': 'fixture', 'mountPath': '/fixture'}]
    spec = {'containers': [container], 'restartPolicy': restart,
            'automountServiceAccountToken': False}
    if volume:
        spec['volumes'] = [{'name': 'fixture', 'configMap': {'name': 'fixture'}}]
    apply({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NS,
           'labels': {'app': name}}, 'spec': spec})


def wait_ready(name, seconds=120):
    k('wait', f'pod/{name}', '--for=condition=Ready', f'--timeout={seconds}s', timeout=seconds + 10)


def poll(name, predicate, seconds=30):
    start = time.monotonic()
    rows = []
    while time.monotonic() - start < seconds:
        current = obj('pod', name)
        status = current.get('status', {}).get('containerStatuses', [{}])[0]
        elapsed = round(time.monotonic() - start, 3)
        reason = (status.get('state', {}).get('waiting', {}).get('reason') or
                  status.get('state', {}).get('terminated', {}).get('reason') or
                  status.get('lastState', {}).get('terminated', {}).get('reason'))
        row = {'second': elapsed, 'restart': status.get('restartCount', 0), 'reason': reason,
               'exit': status.get('lastState', {}).get('terminated', {}).get('exitCode')}
        rows.append(row)
        if predicate(row):
            return rows
        time.sleep(.15)
    return rows


def restart_trial(index):
    name = f'crash-{index}'
    pod(name, 'python:3.11-alpine', ['sh', '-c', 'exit 1'], restart='Always')
    rows = poll(name, lambda r: r['reason'] == 'CrashLoopBackOff' and r['restart'] >= 2, 22)
    k('delete', 'pod', name, '--wait=true', '--ignore-not-found=true', timeout=90)
    return {'trial': index, 'rows': rows, 'first_crashloop_s': next((r['second'] for r in rows if r['reason'] == 'CrashLoopBackOff'), None),
            'two_restarts_s': next((r['second'] for r in rows if r['restart'] >= 2), None),
            'joint_s': next((r['second'] for r in rows if r['reason'] == 'CrashLoopBackOff' and r['restart'] >= 2), None)}


def oom_trial(index):
    name = f'oom-{index}'
    pod(name, 'python:3.11-alpine', ['python', '-c',
        "x=[]\nwhile True:\n x.append(bytearray(4*1024*1024))\n for i in range(0,len(x[-1]),4096): x[-1][i]=1"], memory='48Mi')
    rows = poll(name, lambda r: r['reason'] == 'OOMKilled', 30)
    state = obj('pod', name).get('status', {})
    termination = state.get('containerStatuses', [{}])[0].get('state', {}).get('terminated', {})
    k('delete', 'pod', name, '--wait=true', '--ignore-not-found=true', timeout=90)
    return {'trial': index, 'rows': rows, 'reason': termination.get('reason'),
            'exit': termination.get('exitCode'),
            'oom_s': next((r['second'] for r in rows if r['reason'] == 'OOMKilled'), None)}


def ready_addresses():
    slices = json.loads(k('get', 'endpointslices', '-l', 'kubernetes.io/service-name=target', '-o', 'json'))
    return sum(1 for sl in slices['items'] for endpoint in (sl.get('endpoints') or [])
               if endpoint.get('conditions', {}).get('ready') is True)


def http(path, port=18080):
    start = time.monotonic()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=4) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    return {'status': status, 'ms': round((time.monotonic() - start) * 1000, 3)}


def endpoint_trial(index):
    start = time.monotonic()
    k('patch', 'service', 'target', '--type=merge', '-p', '{"spec":{"selector":{"app":"unmatched"}}}')
    rows = []
    try:
        for _ in range(35):
            t = round(time.monotonic() - start, 3)
            ready = ready_addresses()
            result = http('/caller', 18081)
            rows.append({'second': t, 'ready_addresses': ready, 'caller_status': result['status']})
            if len([r for r in rows if r['ready_addresses'] == 0 and r['caller_status'] == 502]) >= 2:
                break
            time.sleep(.2)
    finally:
        k('patch', 'service', 'target', '--type=merge', '-p', '{"spec":{"selector":{"app":"target"}}}')
    recovery = []
    for _ in range(50):
        ready = ready_addresses()
        response = http('/caller', 18081)
        recovery.append({'ready_addresses': ready, 'caller_status': response['status']})
        if ready > 0 and response['status'] == 200:
            break
        time.sleep(.2)
    return {'trial': index, 'rows': rows, 'recovery': recovery[-1],
            'first_joint_s': next((r['second'] for r in rows if r['ready_addresses'] == 0 and r['caller_status'] == 502), None)}


def latency_trial(index):
    http('/delay/0')
    trial_start = time.monotonic()
    baseline = [(time.monotonic() - trial_start, http('/health')) for _ in range(10)]
    http('/delay/100')
    try:
        active = [(time.monotonic() - trial_start, http('/health')) for _ in range(10)]
    finally:
        http('/delay/0')
    pairs = [{'baseline_second': round(t, 3),
              'active_second': round(at, 3), 'baseline_ms': b['ms'], 'active_ms': a['ms'],
              'baseline_status': b['status'], 'active_status': a['status']}
             for (t, b), (at, a) in zip(baseline, active)]
    return {'trial': index, 'pairs': pairs,
            'baseline_ms': [p['baseline_ms'] for p in pairs],
            'active_ms': [p['active_ms'] for p in pairs],
            'baseline_statuses': [p['baseline_status'] for p in pairs],
            'active_statuses': [p['active_status'] for p in pairs],
            'median_delta_ms': round(statistics.median(p['active_ms'] for p in pairs) -
                                     statistics.median(p['baseline_ms'] for p in pairs), 3)}


def psql(sql):
    return k('exec', 'postgres', '--', 'psql', '-U', 'postgres', '-Atc', sql)


def pool_trial(index, size):
    # Test-only bounded client checkout against real throwaway PostgreSQL, NOT Hikari.
    start = time.monotonic()
    gate = threading.BoundedSemaphore(size)
    lock = threading.Lock()
    occupied = 0
    pending = 0
    observations = []
    failures = []
    workers = []
    sql = "select count(*) from pg_stat_activity where state='active' and query='select pg_sleep(5)'"

    def snapshot(label):
        with lock:
            local_active, local_pending = occupied, pending
        row = {'second': round(time.monotonic() - start, 3), 'label': label,
               'occupied': local_active, 'pending': local_pending,
               'physical_active': int(psql(sql))}
        observations.append(row)
        return row

    def checkout(label, timeout):
        nonlocal occupied, pending
        with lock:
            pending += 1
        acquired = gate.acquire(timeout=timeout)
        with lock:
            pending -= 1
            if acquired:
                occupied += 1
        if not acquired:
            return {'label': label, 'outcome': 'checkout_timeout',
                    'second': round(time.monotonic() - start, 3)}
        try:
            process = subprocess.Popen(['kubectl', '--kubeconfig', str(KUBECONFIG),
                '--context', CONTEXT, '-n', NS, 'exec', 'postgres', '--', 'psql',
                '-U', 'postgres', '-Atc', 'select pg_sleep(5)'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                _, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                raise RuntimeError(f'pool checkout DB query failed: {stderr[:200]!r}')
            return {'label': label, 'outcome': 'success',
                    'second': round(time.monotonic() - start, 3)}
        finally:
            with lock:
                occupied -= 1
            gate.release()

    def launch(label, timeout):
        result = {}
        def work():
            try:
                result.update(checkout(label, timeout))
            except Exception as error:
                failures.append(f'{label}: {error}')
        thread = threading.Thread(target=work)
        thread.start()
        workers.append(thread)
        return result

    try:
        holders = [launch(f'holder-{i}', 20) for i in range(size)]
        for _ in range(50):
            row = snapshot('filling')
            if row['occupied'] == size and row['physical_active'] == size:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('bounded pool did not fill physical PostgreSQL sessions')
        contender = launch('excess-checkout', 1)
        for _ in range(30):
            with lock:
                waiting = pending
            if waiting:
                break
            time.sleep(.01)
        full = snapshot('excess-pending')
        workers[-1].join(timeout=3)
        if workers[-1].is_alive() or contender.get('outcome') != 'checkout_timeout':
            raise RuntimeError(f'excess checkout did not timeout: {contender}')
        timed_out = snapshot('excess-timeout')
        for thread in workers[:size]:
            thread.join(timeout=18)
        if any(thread.is_alive() for thread in workers[:size]):
            raise RuntimeError('holder failed to release within deadline')
        released = snapshot('released')
        retry = launch('after-release', 10)
        workers[-1].join(timeout=17)
        if workers[-1].is_alive() or retry.get('outcome') != 'success':
            raise RuntimeError(f'post-release checkout failed: {retry}')
        after_retry = snapshot('after-retry')
        if failures or any(h.get('outcome') != 'success' for h in holders):
            raise RuntimeError(f'holder failed: {failures}, {holders}')
        return {'trial': index, 'size': size, 'ttl_s': 5, 'counts': observations,
                'full': full, 'timed_out': timed_out, 'contender': contender,
                'released': released, 'retry': retry, 'after_retry': after_retry,
                'holders': holders, 'elapsed_s': round(time.monotonic() - start, 3)}
    finally:
        for thread in workers:
            thread.join(timeout=20)


def write(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main():
    actual = cmd('kubectl', '--kubeconfig', str(KUBECONFIG), 'config', 'current-context').strip()
    if actual != CONTEXT or 'kan110-measure' not in cmd('kind', 'get', 'clusters').splitlines():
        raise RuntimeError('refusing non-disposable context')
    k('create', 'namespace', NS) if not cmd('kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT,
       'get', 'namespace', NS, check=False).strip() else None
    if not cmd('kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT,
               '-n', NS, 'get', 'configmap', 'fixture', check=False).strip():
        k('create', 'configmap', 'fixture', '--from-file=fixture.py=' + str(HERE / 'fixture.py'))
    pod('target', 'python:3.11-alpine', ['python', '/fixture/fixture.py'], restart='Always', volume=True)
    pod('caller', 'python:3.11-alpine', ['python', '/fixture/fixture.py'], restart='Always', volume=True)
    pod('postgres', 'postgres:16-alpine', restart='Always', env={'POSTGRES_HOST_AUTH_METHOD': 'trust'})
    apply({'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'target', 'namespace': NS},
           'spec': {'selector': {'app': 'target'}, 'ports': [{'port': 8080, 'targetPort': 8080}]}})
    for name in ('target', 'caller', 'postgres'):
        wait_ready(name, 180)
    forwards = [subprocess.Popen(['kubectl', '--kubeconfig', str(KUBECONFIG), '--context', CONTEXT,
                '-n', NS, 'port-forward', f'pod/{name}', f'{port}:8080'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL) for name, port in (('target', 18080), ('caller', 18081))]
    try:
        for _ in range(40):
            try:
                if http('/health')['status'] == 200 and http('/caller', 18081)['status'] == 200:
                    break
            except Exception:
                time.sleep(.25)
        else:
            raise RuntimeError('fixture port-forwards failed')
        saved = OUT / 'measurements.json'
        result = json.loads(saved.read_text()) if saved.exists() else {
            'context': CONTEXT, 'cluster': 'kan110-measure',
            'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'fixture': 'python:3.11-alpine + postgres:16-alpine', 'baseline_ready': ready_addresses()}
        for family, runner in (('crash', restart_trial), ('oom', oom_trial),
                               ('latency', latency_trial), ('endpoint', endpoint_trial)):
            result.setdefault(family, [])
            for i in range(len(result[family]) + 1, 6):
                result[family].append(runner(i))
                write('measurements.json', result)
        result.setdefault('pool', {})
        for size in (2, 6):
            result['pool'].setdefault(str(size), [])
            # Old physical-session-only samples cannot stand in for checkout trials.
            if result['pool'][str(size)] and 'contender' not in result['pool'][str(size)][0]:
                result['pool'][str(size)] = []
            for i in range(len(result['pool'][str(size)]) + 1, 6):
                result['pool'][str(size)].append(pool_trial(i, size))
                write('measurements.json', result)
        result['final_ready'] = ready_addresses()
        result['final_caller'] = http('/caller', 18081)['status']
        write('measurements.json', result)
    finally:
        for process in forwards:
            process.terminate()
            process.wait(timeout=5)


if __name__ == '__main__':
    main()
