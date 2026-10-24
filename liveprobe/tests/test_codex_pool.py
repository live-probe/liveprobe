"""P6: Codex account pool (primary -> spare -> wait), quota classification, engine waiting."""
from __future__ import annotations

import json
import stat

import pytest

from app.llm.base import LLMError, LLMResult, Usage
from app.llm.codex_pool import Account, CodexPoolProvider, classify_quota, parse_reset_seconds

OK = LLMResult(text="{}", data={"x": 1}, usage=Usage(input_tokens=1, output_tokens=1), duration_ms=1, provider="codex_cli", model="m")


class Fake:
    """Stand-in for CodexCLIProvider: plays back a list of outcomes (LLMResult or LLMError)."""
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def complete(self, role, prompt, schema, model, effort, timeout):
        self.calls += 1
        o = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(o, Exception):
            raise o
        return o


def quota(reset=None):
    e = LLMError("quota", "codex usage limit reached")
    e.reset_seconds = reset
    return e


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def pool(primary, spare, clock=None, **kw):
    c = clock or Clock()
    return CodexPoolProvider([Account("primary", primary), Account("spare", spare)], clock=c, **kw), c


def call(p):
    return p.complete("r", "prompt", {}, "m", "low", 5)


def test_primary_serves_everything_while_it_works():
    a, b = Fake(OK), Fake(OK)
    p, _ = pool(a, b)
    for _ in range(3):
        call(p)
    assert (a.calls, b.calls) == (3, 0) and p.status()["active"] == "primary"


def test_quota_on_primary_switches_to_spare_and_stays_there():
    a, b = Fake(quota(600)), Fake(OK)
    p, clock = pool(a, b)
    assert call(p) is OK and p.status()["active"] == "spare"
    call(p); call(p)
    assert (a.calls, b.calls) == (1, 3)                       # primary is not hammered while limited
    kinds = [k for k, _ in p.drain_events()]
    assert kinds == ["account_limited", "account_switched"]


def test_primary_is_used_again_after_its_reset_time():
    a, b = Fake(quota(600), OK), Fake(OK)
    p, clock = pool(a, b)
    call(p)
    clock.t += 601
    call(p)
    assert p.status()["active"] == "primary" and a.calls == 2


def test_both_limited_raises_quota_with_wait_and_never_returns_a_result():
    a, b = Fake(quota(120)), Fake(quota(300))
    p, clock = pool(a, b)
    with pytest.raises(LLMError) as ei:
        call(p)
    assert ei.value.kind == "quota"
    assert p.next_available_in() == 120                        # earliest known reset
    ev = dict(p.drain_events())
    assert "waiting_quota" in ev and ev["waiting_quota"]["retry_in_s"] == 120
    # while both are blocked nobody is called again
    with pytest.raises(LLMError):
        call(p)
    assert (a.calls, b.calls) == (1, 1)


def test_unknown_reset_time_uses_the_default_cooldown():
    a, b = Fake(quota(None)), Fake(quota(None))
    p, _ = pool(a, b, cooldown_s=900)
    with pytest.raises(LLMError):
        call(p)
    assert p.next_available_in() == 900


def test_unclassified_provider_error_is_not_retried_on_the_spare():
    boom = LLMError("provider", "codex execution failed or did not complete")
    a, b = Fake(boom), Fake(OK)
    p, _ = pool(a, b)
    with pytest.raises(LLMError) as ei:
        call(p)
    assert ei.value.kind == "provider" and b.calls == 0       # a mystery failure must not silently burn the spare


@pytest.mark.parametrize("kind", ["timeout", "invalid_output", "config"])
def test_other_error_kinds_propagate_without_switching(kind):
    a, b = Fake(LLMError(kind, "x")), Fake(OK)
    p, _ = pool(a, b)
    with pytest.raises(LLMError):
        call(p)
    assert b.calls == 0


def test_auth_failure_on_primary_tries_spare_but_both_bad_raises_auth():
    p, _ = pool(Fake(LLMError("auth", "x")), Fake(OK))
    assert call(p) is OK                                       # spare still works
    p, _ = pool(Fake(LLMError("auth", "x")), Fake(LLMError("auth", "y")))
    with pytest.raises(LLMError) as ei:
        call(p)
    assert ei.value.kind == "auth"                            # never reported as a quota wait


def test_status_reports_limited_accounts_without_secrets():
    p, _ = pool(Fake(quota(60)), Fake(OK))
    call(p)
    st = p.status()
    assert st["accounts"][0]["state"] == "limited" and st["accounts"][0]["blocked_for_s"] == 60
    assert "home" not in json.dumps(st) and "auth" not in json.dumps(st).lower().replace("auth_failed", "")


def test_single_account_pool_waits_instead_of_switching():
    p = CodexPoolProvider([Account("primary", Fake(quota(30)))], clock=Clock())
    with pytest.raises(LLMError) as ei:
        call(p)
    assert ei.value.kind == "quota"


def test_empty_pool_is_a_config_error():
    with pytest.raises(LLMError) as ei:
        CodexPoolProvider([])
    assert ei.value.kind == "config"


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("msg", ["You've hit your usage limit. Try again in 3 hours.", "429 Too Many Requests",
                                 "rate limit exceeded", "Quota exceeded for this plan"])
def test_quota_messages_detected(msg):
    assert classify_quota(msg)


@pytest.mark.parametrize("msg", ["model not found", "connection reset by peer", "invalid schema", ""])
def test_non_quota_messages_not_detected(msg):
    assert not classify_quota(msg)


@pytest.mark.parametrize("msg,secs", [("Try again in 3 hours", 10800), ("retry after 45 seconds", 45),
                                      ("resets in 20 min", 1200), ("no time here", None)])
def test_reset_parse(msg, secs):
    assert parse_reset_seconds(msg) == secs


# ---------------------------------------------------------------- real CLI parser with a fake codex binary
def _fake_codex(tmp_path, events, rc=1, stderr=""):
    lines = "\n".join(json.dumps(e) for e in events)
    s = tmp_path / "codex"
    s.write_text("#!/bin/sh\ncat >/dev/null\ncat <<'EOF'\n" + lines + "\nEOF\n" + (f"echo {stderr!r} >&2\n" if stderr else "") + f"exit {rc}\n")
    s.chmod(s.stat().st_mode | stat.S_IEXEC)
    from app.llm.codex_cli import CodexCLIProvider
    return CodexCLIProvider(str(s))


def test_cli_usage_limit_becomes_quota_without_leaking_raw_text(tmp_path):
    raw = "You've hit your usage limit. Try again in 2 hours. token=sk-live-SECRETVALUE"
    prov = _fake_codex(tmp_path, [{"type": "turn.failed", "error": {"message": raw}}])
    with pytest.raises(LLMError) as ei:
        prov.complete("r", "p", {"type": "object", "properties": {}}, "m", None, 10)
    e = ei.value
    assert e.kind == "quota" and e.reset_seconds == 7200
    assert "SECRETVALUE" not in str(e) and "SECRETVALUE" not in repr(e.__dict__)


def test_cli_auth_failure_is_still_auth_not_quota(tmp_path):
    prov = _fake_codex(tmp_path, [{"type": "turn.failed", "error": {"message": "401 unauthorized: token expired"}}])
    with pytest.raises(LLMError) as ei:
        prov.complete("r", "p", {"type": "object", "properties": {}}, "m", None, 10)
    assert ei.value.kind == "auth"


def test_pool_over_real_cli_parser_switches_on_a_fake_limit(tmp_path):
    from app.llm.codex_cli import CodexCLIProvider
    d1, d2 = tmp_path / "a", tmp_path / "b"; d1.mkdir(); d2.mkdir()
    limited = _fake_codex(d1, [{"type": "turn.failed", "error": {"message": "usage limit reached, try again in 1 hour"}}])
    good_bin = d2 / "codex"
    good_bin.write_text("#!/bin/sh\ncat >/dev/null\n" + "".join(
        f"echo '{json.dumps(e)}'\n" for e in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"a": "b"})}},
            {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}}]))
    good_bin.chmod(good_bin.stat().st_mode | stat.S_IEXEC)
    p = CodexPoolProvider([Account("primary", limited), Account("spare", CodexCLIProvider(str(good_bin)))])
    r = p.complete("r", "p", {"type": "object", "properties": {"a": {"type": "string"}}}, "m", None, 10)
    assert r.data == {"a": "b"} and p.status()["active"] == "spare"


# ---------------------------------------------------------------- factory
def test_factory_builds_pool_only_when_spare_is_configured_and_logged_in(tmp_path):
    from app.config import AppConfig, Settings
    from app.llm import make_provider
    assert make_provider(AppConfig(), Settings()).__class__.__name__ == "CodexCLIProvider"
    spare = tmp_path / "spare"; spare.mkdir()
    # configured but not logged in -> single-account mode, never an advertised-but-unusable spare
    assert make_provider(AppConfig(), Settings(codex_home_spare=str(spare))).__class__.__name__ == "CodexCLIProvider"
    (spare / "auth.json").write_text("{}")
    pool_ = make_provider(AppConfig(), Settings(codex_home_spare=str(spare)))
    assert isinstance(pool_, CodexPoolProvider) and [a.name for a in pool_.accounts] == ["primary", "spare"]
    assert pool_.accounts[0].home != pool_.accounts[1].home      # separate CODEX_HOME per account


def test_api_and_claude_are_never_in_a_codex_pool(tmp_path):
    from app.config import AppConfig, Settings
    from app.llm import make_provider
    (tmp_path / "auth.json").write_text("{}")
    pool_ = make_provider(AppConfig(), Settings(codex_home_spare=str(tmp_path)))
    assert {a.provider.name for a in pool_.accounts} == {"codex_cli"}


@pytest.mark.parametrize("pool_mode", [True, False])
def test_accounts_endpoint_reports_state_without_secrets(pool_mode, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    prov = CodexPoolProvider([Account("primary", Fake(OK), "/secret/home"), Account("spare", Fake(OK), "/secret/spare")]) if pool_mode else object()
    class Eng: provider = prov
    main.STATE["engine"] = Eng()
    try:
        c = TestClient(main.app, base_url="http://liveprobe.localhost", raise_server_exceptions=False)
        body = c.get("/api/llm/accounts").json()
    finally:
        main.STATE["engine"] = None
    assert body["mode"] == ("pool" if pool_mode else "single")
    assert "/secret" not in json.dumps(body)
