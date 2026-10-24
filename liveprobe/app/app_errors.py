"""Read-only Loki exception detection. Stack text is untrusted evidence, never instructions."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque

import httpx

from .config import AppConfig, Settings
from .incidents import Signal
from .redact import redact

log = logging.getLogger(__name__)
_PY = re.compile(r'File "([^"\n]+)", line (\d+)(?:, in ([^\n]+))?')
_JAVA = re.compile(r'\bat\s+([\w.$<>]+)\(([^():]+\.java):(\d+)\)')
_NODE = re.compile(r'\bat\s+(?:(.*?)\s+\()?(/[^():\n]+\.(?:[cm]?js|ts)):(\d+):(\d+)\)?')
_EX = re.compile(r'(?:^|\s)([\w.$]*(?:Exception|Error|Fault)(?:[\w.$]*))(?::\s*(.*))?$')


def parse_frames(text: str) -> list[dict]:
    """Keep observed user-code frames only; no path/source reconstruction."""
    frames = []
    for line in text.splitlines():
        py, java, node = _PY.search(line), _JAVA.search(line), _NODE.search(line)
        frame = None
        if py and py[1].startswith('/app/') and not any(x in py[1] for x in ('/site-packages/', '/dist-packages/', '/.venv/')):
            frame = {'file': py[1], 'line': int(py[2]), 'symbol': (py[3] or '').strip(), 'language': 'python'}
        elif java and java[1].startswith('io.liveprobe.'):
            frame = {'file': java[2], 'line': int(java[3]), 'symbol': java[1], 'language': 'java'}
        elif node and node[2].startswith('/app/') and '/node_modules/' not in node[2]:
            frame = {'file': node[2], 'line': int(node[3]), 'symbol': (node[1] or '').strip(), 'language': 'node'}
        if frame and frame not in frames:
            frames.append(frame)
    return frames[:40]


def _bounded(text: str) -> str:
    return (redact(text) or '').encode('utf-8')[:4096].decode('utf-8', errors='ignore')


def parse_exceptions(text: str) -> list[dict]:
    """Parse Java/Python/Node blocks, preserving each repetition and dropping JSON access lines."""
    blocks, lines = [], []
    def flush():
        if not lines:
            return
        body = '\n'.join(lines)
        matches = [m for line in lines if (m := _EX.search(line.strip()))]
        if matches:
            match = matches[-1]
            blocks.append({'exception': match[1], 'message': _bounded(match[2] or ''),
                           'frames': parse_frames(body), 'sample': _bounded(body)})
        lines.clear()
    for line in str(text).splitlines():
        if line.lstrip().startswith(('{', '[')):
            try:
                if isinstance(json.loads(line), (dict, list)):
                    continue
            except ValueError:
                pass
        stripped = line.strip()
        is_exception = _EX.search(stripped)
        if stripped.startswith('Traceback '):
            flush()
        elif is_exception and lines and not any('Traceback ' in x for x in lines) and not stripped.startswith('Caused by:'):
            flush()
        lines.append(line)
        if is_exception and any('Traceback ' in x for x in lines):
            flush()
    flush()
    return blocks


def exception_query(namespaces: list[str], apps: list[str] | None = None) -> str:
    labels = 'namespace=~' + json.dumps('|'.join(re.escape(n) for n in namespaces))
    if apps:
        labels += ', app=~' + json.dumps('|'.join(re.escape(a) for a in apps))
    pattern = r'Traceback|Exception|[A-Za-z]*Error:|\sat\s|File "'
    return '{' + labels + '} |~ ' + json.dumps(pattern)


def fetch_exceptions(settings: Settings, namespaces: list[str], start: float, end: float,
                     apps: list[str] | None = None) -> list[dict]:
    response = httpx.get(settings.loki_url + '/loki/api/v1/query_range', timeout=8, params={
        'query': exception_query(namespaces, apps), 'start': str(int(start * 1e9)), 'end': str(int(end * 1e9)),
        'limit': 1000, 'direction': 'forward'})
    response.raise_for_status()
    result = []
    for stream in response.json().get('data', {}).get('result', []):
        labels = stream.get('stream') or {}
        ns, app = labels.get('namespace'), labels.get('app')
        if ns not in namespaces or not app or (apps and app not in apps):
            continue
        values = sorted(stream.get('values') or [], key=lambda row: int(row[0]))
        # Loki may store a stack as one multiline entry or several lines in one stream.
        current, ts = [], None
        def flush():
            if current:
                for parsed in parse_exceptions('\n'.join(current)):
                    result.append({**parsed, 'namespace': ns, 'app': app, 'timestamp': ts,
                                   'stream': json.dumps(labels, sort_keys=True)})
                current.clear()
        for stamp, body in values:
            if 'Traceback ' in body or (_EX.search(body.splitlines()[0].strip()) and current and not any('Traceback ' in x for x in current)):
                flush()
            current.append(body)
            ts = int(stamp) / 1e9
            if _EX.search(body.splitlines()[-1].strip()) and any('Traceback ' in x for x in current):
                flush()
        flush()
    return sorted(result, key=lambda row: row['timestamp'])


def code_evidence(rows: list[dict], reason: str | None = None) -> dict:
    exceptions, frames = [], []
    for row in rows:
        for parsed in parse_exceptions(row.get('sample') or ''):
            if len(exceptions) >= 2:
                break
            exceptions.append(parsed)
            for frame in parsed['frames']:
                if frame not in frames:
                    frames.append(frame)
        if len(exceptions) >= 2:
            break
    return {'frames': frames, 'exceptions': exceptions,
            'reason': reason or ('사용자 코드 프레임 미관측' if exceptions and not frames else '최근 예외 로그 미관측' if not exceptions else None)}


class AppErrorWatcher:
    def __init__(self, cfg: AppConfig, settings: Settings, emit):
        self.cfg, self.settings, self.emit = cfg, settings, emit
        self._stop = threading.Event()
        self._seen = {}
        self._counts = defaultdict(deque)
        self.status = {'state': 'starting'}

    def process(self, rows: list[dict], *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - self.cfg.app_error_window_seconds
        self._seen = {key: ts for key, ts in self._seen.items() if ts >= cutoff}
        for key in list(self._counts):
            while self._counts[key] and self._counts[key][0] < cutoff:
                self._counts[key].popleft()
            if not self._counts[key]:
                del self._counts[key]
        emitted = 0
        for row in sorted(rows, key=lambda r: r.get('timestamp', 0)):
            ns, app, stamp = row.get('namespace'), row.get('app'), row.get('timestamp', 0)
            if ns not in self.cfg.target_namespaces or not app or not cutoff <= stamp <= now:
                continue
            for parsed in parse_exceptions(row.get('sample') or ''):
                identity = (ns, app, row.get('stream'), stamp, parsed['sample'])
                if identity in self._seen:
                    continue
                frames = parsed['frames']
                top = frames[-1] if frames and frames[0]['language'] == 'python' else frames[0] if frames else {}
                key = (ns, app, parsed['exception'], top.get('file'), top.get('line'), top.get('symbol'))
                count = self._counts[key]
                count.append(stamp)
                if len(count) >= self.cfg.app_error_threshold:
                    self.emit(Signal(ns, 'Deployment', app, 'ApplicationException', 'app_log', parsed))
                    count.clear()
                    emitted += 1
                self._seen[identity] = stamp
        return emitted

    def poll_once(self) -> int:
        now = time.time()
        try:
            rows = fetch_exceptions(self.settings, self.cfg.target_namespaces, now - self.cfg.app_error_window_seconds, now)
            emitted = self.process(rows, now=now)
            self.status = {'state': 'running', 'emitted': emitted}
            return emitted
        except Exception as exc:
            self.status = {'state': 'degraded', 'error': type(exc).__name__}
            log.warning('app exception observation unavailable: %s', type(exc).__name__)
            return 0

    def start(self):
        threading.Thread(target=self._run, name='app_error_poller', daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.cfg.app_error_poll_interval_seconds)

    def stop(self):
        self._stop.set()
