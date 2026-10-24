"""P15: interpretation is requested after each agent finishes and on page polls; never blocks the pipeline."""
from conftest import needs_db


class Recorder:
    def __init__(self):
        self.calls: list[tuple[int, bool]] = []
        self.views: list[int] = []

    def request(self, iid: int, idle: bool = False) -> None:
        self.calls.append((iid, idle))

    def seen(self, iid: int) -> None:
        self.views.append(iid)


@needs_db
def test_agent_finish_requests_interpretation(db_url):
    from app import db
    from app.config import AppConfig, Settings
    from app.engine import Engine
    from app.models import Incident
    from app.llm import AGENT_SCHEMA, AgentOutput
    from app.llm.base import LLMResult, Usage

    class P:
        name = "codex_cli"

        def complete(self, role, prompt, schema, model, effort, timeout):
            return LLMResult(text="", data={"summary": "ok", "confidence": 0.9, "root_cause_candidates": [], "next_action": "handoff"},
                             usage=Usage(input_tokens=10, output_tokens=5))

    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="n", workload="w", title="t", status="analyzing")
        s.add(inc)
        s.flush()
        iid = inc.id
    rec = Recorder()
    eng = Engine(AppConfig(), Settings(background=False), None, P(), None)
    eng.interpret_worker = rec
    eng._call(iid, "signal_analyst", "prompt", AGENT_SCHEMA, AgentOutput, [])
    assert rec.calls == [(iid, False)]


@needs_db
def test_narrative_poll_requests_idle_interpretation(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, main
    from app.models import Incident
    from app import db
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="n", workload="w", title="t", status="handed_off")
        s.add(inc)
        s.flush()
        iid = inc.id
        quiet = Incident(fingerprint="g", namespace="n", workload="w2", title="t2", status="handed_off")
        s.add(quiet)
        s.flush()
        quiet_id = quiet.id
        from app.models import AgentRun, Evidence
        s.add(Evidence(incident_id=iid, source="loki", target="n/w", payload={"lines": ["[w] strange widget hiccup code=77"]}))
        s.add(AgentRun(incident_id=iid, role="signal_analyst", provider="codex_cli", model="gpt-6-luna", status="succeeded",
                       attempts=1, output={"summary": "unclear"}))
    rec = Recorder()
    with TestClient(main.app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        main.STATE["interpret"] = rec
        try:
            assert c.get(f"/api/incidents/{iid}/narrative").status_code == 200
            assert c.get(f"/api/incidents/{quiet_id}/narrative").status_code == 200
        finally:
            main.STATE.pop("interpret", None)
    assert rec.calls == [(iid, True)]
    # Every war-room poll marks the incident as watched (live readings follow only watched incidents).
    assert rec.views == [iid, quiet_id]
    config.get_settings.cache_clear()


# ---- P15 live run: display-only readings consumed the analysis quota (51 interpreter calls vs 12 router calls in 3h).
import threading
import time


class FakeInterpreter:
    def __init__(self, wait=0.0):
        self.runs: list[int] = []
        self.wait = wait
        self.ran = threading.Event()

    def wait_s(self) -> float:
        return self.wait

    def run(self, iid: int) -> int:
        self.runs.append(iid)
        self.ran.set()
        return 1


def worker(fake, **kw):
    from app.interpret import InterpretWorker
    w = InterpretWorker(fake, **{"retry_after_s": 300, "min_interval_s": 0.4, "view_ttl_s": 5, **kw})
    w.start()
    return w


def settle(seconds=0.15):
    time.sleep(seconds)


def test_pipeline_progress_does_not_spend_quota_on_unwatched_incidents():
    fake = FakeInterpreter()
    w = worker(fake)
    try:
        w.request(7)
        settle()
        assert fake.runs == []
        w.seen(7)
        w.request(7)
        assert fake.ran.wait(2)
        assert fake.runs == [7]
    finally:
        w.stop()


def test_watched_pipeline_requests_are_coalesced_not_dropped():
    fake = FakeInterpreter()
    w = worker(fake)
    try:
        w.seen(7)
        w.request(7)
        assert fake.ran.wait(2)
        w.request(7)
        w.request(7)
        w.request(7)
        settle()
        assert fake.runs == [7]
        settle(0.6)
        assert fake.runs == [7, 7]
    finally:
        w.stop()


def test_page_poll_counts_as_viewing_and_runs_once_per_retry_window():
    fake = FakeInterpreter()
    w = worker(fake)
    try:
        w.request(9, idle=True)
        assert fake.ran.wait(2)
        w.request(9, idle=True)
        settle()
        assert fake.runs == [9]
    finally:
        w.stop()


def test_all_accounts_limited_defers_without_calling_the_model():
    fake = FakeInterpreter(wait=0.5)
    w = worker(fake)
    try:
        w.request(3, idle=True)
        settle(0.2)
        assert fake.runs == []
        fake.wait = 0.0
        settle(0.8)
        assert fake.runs == [3]
    finally:
        w.stop()


@needs_db
def test_interpreter_records_no_failed_run_while_every_account_is_limited(db_url):
    from app import db
    from app.config import AppConfig
    from app.interpret import Interpreter
    from app.models import AgentRun, Evidence, Incident
    from sqlalchemy import select

    class Limited:
        name = "codex_cli"

        def next_available_in(self):
            return 900.0

        def complete(self, *a, **k):
            raise AssertionError("model must not be called while every account is limited")

    with db.session() as s:
        inc = Incident(fingerprint="q", namespace="n", workload="w", title="t", status="handed_off")
        s.add(inc)
        s.flush()
        iid = inc.id
        s.add(Evidence(incident_id=iid, source="loki", target="n/w", payload={"lines": ["[w] strange widget hiccup code=77"]}))
    interp = Interpreter(AppConfig(), Limited())
    assert interp.wait_s() == 900.0
    assert interp.run(iid) == 0
    with db.session() as s:
        assert not list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid)))


def test_deferred_interpretation_must_not_run_after_view_expired():
    """독립 검토 재현: 한도로 미뤄진 사이 아무도 보지 않게 되면 한도가 풀려도 호출하지 않는다."""
    fake = FakeInterpreter(wait=0.15)
    w = worker(fake, view_ttl_s=0.03, min_interval_s=0.01)
    try:
        w.request(7, idle=True)
        settle(0.07)
        fake.wait = 0.0
        settle(0.4)
        assert fake.runs == []
        assert 7 not in w._due
    finally:
        w.stop()


def test_view_and_attempt_bookkeeping_is_pruned():
    fake = FakeInterpreter(wait=10)
    w = worker(fake, retry_after_s=0.05, min_interval_s=0.05, view_ttl_s=0.05)
    try:
        for iid in range(300):
            w.seen(iid)
        settle(0.12)
        w.seen(1000)
        assert len(w._seen) <= 2
    finally:
        w.stop()
