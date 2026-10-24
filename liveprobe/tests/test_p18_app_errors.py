"""P18 deterministic exception parsing and repeated error intake."""
import pytest
from app.config import AppConfig, Settings

PY = 'Traceback (most recent call last):\n  File "/usr/lib/python/lib.py", line 1, in run\n  File "/app/PaymentService.py", line 42, in pay\nValueError: broken payment'
JAVA = 'java.lang.IllegalStateException: broken payment\n\tat java.base.Thread.run(Thread.java:9)\n\tat io.liveprobe.payment.PaymentService.pay(PaymentService.java:42)'
NODE = 'TypeError: broken payment\n    at run (/app/node_modules/pkg/lib.js:1:2)\n    at pay (/app/PaymentService.js:42:3)'


@pytest.mark.parametrize('text,language,file,symbol', [(PY, 'python', '/app/PaymentService.py', 'pay'), (JAVA, 'java', 'PaymentService.java', 'io.liveprobe.payment.PaymentService.pay'), (NODE, 'node', '/app/PaymentService.js', 'pay')])
def test_user_stack_frames(text, language, file, symbol):
    from app.app_errors import parse_exceptions
    out = parse_exceptions(text)
    assert len(out) == 1
    frames = out[0]['frames']
    assert frames == [{'file': file, 'line': 42, 'symbol': symbol, 'language': language}]
    assert out[0]['exception'] and out[0]['message'] == 'broken payment'


def test_access_json_is_not_an_exception():
    from app.app_errors import parse_exceptions
    assert parse_exceptions('{"method":"GET","path":"/Error:","status":500}') == []


def test_repeated_errors_threshold_is_event_time_and_replay_safe():
    from app.app_errors import AppErrorWatcher
    sent = []
    watcher = AppErrorWatcher(AppConfig(), Settings(), sent.append)
    rows = [{'namespace': 'prd', 'app': 'payment', 'timestamp': 100 + i, 'sample': PY} for i in range(3)]
    assert watcher.process(rows[:2], now=103) == 0
    assert watcher.process(rows, now=103) == 1
    sig = sent[0]
    assert (sig.namespace, sig.name, sig.reason, sig.source, sig.kind) == ('prd', 'payment', 'ApplicationException', 'app_log', 'Deployment')
    assert sig.detail['frames'][0]['line'] == 42
    assert watcher.process(rows, now=104) == 0
    assert watcher.process([{**rows[0], 'timestamp': 105, 'namespace': 'other'}], now=105) == 0
    assert watcher.process([{**rows[0], 'timestamp': 300}], now=300) == 0


def test_e8_code_refs_is_bounded_and_marks_missing():
    from app.app_errors import code_evidence
    out = code_evidence([{'sample': PY + '\n' + 'x' * 8000}, {'sample': NODE}, {'sample': JAVA}])
    assert len(out['exceptions']) == 2
    assert all(len(e['sample'].encode()) <= 4096 for e in out['exceptions'])
    assert out['frames'][0]['file'] == '/app/PaymentService.py'
    assert code_evidence([])['reason']


def test_collection_publishes_e8_and_evidence_summary(monkeypatch):
    from app import evidence
    from app.evidence import collect, compact_summary
    from conftest import family1_kube
    monkeypatch.setattr(evidence, '_prom', lambda *a: [])
    monkeypatch.setattr(evidence, '_loki', lambda *a: [])
    monkeypatch.setattr('app.app_errors.fetch_exceptions', lambda *a: [{'sample': PY}])
    b = collect(family1_kube(), AppConfig(), Settings(), 'prd', 'payment')
    assert compact_summary(b)['E8_code_refs']['frames'][0]['line'] == 42
    assert any(i.source == 'code_refs' and i.payload['exceptions'] for i in b.items)


def test_split_loki_stack_and_multiline_blocks(monkeypatch):
    import json
    from app.app_errors import fetch_exceptions, AppErrorWatcher
    class Response:
        def raise_for_status(self): pass
        def json(self):
            values = [[str(int((100 + i) * 1e9)), line] for i, line in enumerate(PY.splitlines())]
            return {'data': {'result': [{'stream': {'namespace': 'prd', 'app': 'payment', 'pod': 'one'}, 'values': values}]}}
    seen = {}
    def get(*args, **kwargs):
        seen.update(kwargs['params'])
        return Response()
    monkeypatch.setattr('app.app_errors.httpx.get', get)
    out = fetch_exceptions(Settings(), ['dev', 'prd'], 99, 110)
    assert out[0]['frames'][0]['file'] == '/app/PaymentService.py'
    assert 'dev|prd' in seen['query'] and 'namespace=~' in seen['query']
    watcher = AppErrorWatcher(AppConfig(app_error_threshold=1), Settings(), lambda s: None)
    assert watcher.process(out, now=110) == 1


def test_long_stack_still_detected_with_bounded_sample(monkeypatch):
    from app.app_errors import fetch_exceptions, AppErrorWatcher, code_evidence
    raw = PY.replace('ValueError:', '  # ' + 'x' * 5000 + '\nValueError:')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'data': {'result': [{'stream': {'namespace': 'prd', 'app': 'payment'}, 'values': [[str(int(100e9)), raw]]}]}}
    monkeypatch.setattr('app.app_errors.httpx.get', lambda *a, **kw: Response())
    out = fetch_exceptions(Settings(), ['prd'], 99, 110)
    watcher = AppErrorWatcher(AppConfig(app_error_threshold=1), Settings(), lambda s: None)
    assert watcher.process(out, now=110) == 1
    assert code_evidence(out)['frames'][0]['line'] == 42
