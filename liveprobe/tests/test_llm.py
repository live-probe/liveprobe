"""KAN-40: provider tests with a fake `codex` executable; redaction."""
from __future__ import annotations

import json
import os
import stat
import textwrap

import pytest

from app.llm import AGENT_SCHEMA, AgentOutput, LLMError
from app.llm.base import parse_json_object, strict_schema
from app.llm.codex_cli import CodexCLIProvider
from app.llm.openai_api import OpenAIAPIProvider
from app.redact import redact, redact_obj

GOOD = {"summary": "s", "root_cause_candidates": [{"cause": "bad tag", "confidence": 0.9, "evidence_refs": ["E2_pods"]}],
        "counter_evidence": [], "confidence": 0.9, "next_action": "rollout_undo", "recommended_steps": [], "action": None}


def fake_codex(tmp_path, body: str):
    p = tmp_path / "codex"
    p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def jsonl(*events):
    return "\n".join(json.dumps(e) for e in events)


def test_codex_success_parses_message_and_usage(tmp_path):
    out = jsonl({"type": "thread.started"}, {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(GOOD)}},
                {"type": "turn.completed", "usage": {"input_tokens": 1234, "output_tokens": 56}})
    (tmp_path / "out.jsonl").write_text(out)
    args_file = tmp_path / "args.txt"
    bin_ = fake_codex(tmp_path, f"""
        echo "$@" > {args_file}
        cat > /dev/null
        cat {tmp_path / 'out.jsonl'}
        """)
    r = CodexCLIProvider(bin_, str(tmp_path)).complete("signal_analyst", "p", AGENT_SCHEMA, "gpt-6-luna", "low", 10)
    AgentOutput.model_validate(r.data)
    assert r.usage.input_tokens == 1234 and r.usage.output_tokens == 56
    assert r.provider == "codex_cli" and r.model == "gpt-6-luna" and r.effort == "low"
    args = args_file.read_text()
    assert "-s read-only" in args and "-m gpt-6-luna" in args and "model_reasoning_effort=low" in args
    assert "--output-schema" in args and "--json" in args and "--skip-git-repo-check" in args


def test_codex_timeout_kills_process(tmp_path):
    bin_ = fake_codex(tmp_path, "sleep 30\n")
    with pytest.raises(LLMError) as ei:
        CodexCLIProvider(bin_).complete("r", "p", AGENT_SCHEMA, "m", None, 1)
    assert ei.value.kind == "timeout"


def test_codex_invalid_json(tmp_path):
    (tmp_path / "o").write_text(jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": "not json"}},
                                      {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}))
    bin_ = fake_codex(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}\n")
    with pytest.raises(LLMError) as ei:
        CodexCLIProvider(bin_).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "invalid_output" and ei.value.usage.input_tokens == 5


def test_codex_auth_error_is_classified_and_redacted(tmp_path):
    (tmp_path / "o").write_text(jsonl({"type": "error", "message": "401 Unauthorized: token expired access_token=abcd1234efgh"}))
    bin_ = fake_codex(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}; exit 1\n")
    with pytest.raises(LLMError) as ei:
        CodexCLIProvider(bin_).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "auth"
    assert "abcd1234efgh" not in ei.value.message and "access_token" not in ei.value.message


def test_codex_missing_binary():
    with pytest.raises(LLMError) as ei:
        CodexCLIProvider("/nonexistent/codex").complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "config"


def test_codex_does_not_leak_api_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass-1234567890")
    envf = tmp_path / "env"
    (tmp_path / "o").write_text(jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(GOOD)}},
                                      {"type": "turn.completed"}))
    bin_ = fake_codex(tmp_path, f"env > {envf}; cat > /dev/null; cat {tmp_path / 'o'}\n")
    CodexCLIProvider(bin_, str(tmp_path)).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    env = envf.read_text()
    assert "OPENAI_API_KEY" not in env and f"CODEX_HOME={tmp_path}" in env


class _Resp:
    def __init__(self, text):
        self.output_text = text
        self.usage = type("U", (), {"input_tokens": 11, "output_tokens": 7})()


class _Client:
    def __init__(self, text=None, exc=None):
        self.kw = None
        self.text, self.exc = text, exc
        self.responses = self

    def create(self, **kw):
        self.kw = kw
        if self.exc:
            raise self.exc
        return _Resp(self.text)


def test_openai_api_structured_output():
    c = _Client(json.dumps(GOOD))
    r = OpenAIAPIProvider("unused", client=c).complete("result_verifier", "p", AGENT_SCHEMA, "gpt-5.6-terra", "low", 10)
    assert r.provider == "openai_api" and r.usage.input_tokens == 11
    fmt = c.kw["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert c.kw["reasoning"] == {"effort": "low"}


def test_openai_api_without_key_is_config_error():
    with pytest.raises(LLMError) as ei:
        OpenAIAPIProvider(None).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "config"


def test_openai_api_error_redacted():
    class AuthenticationError(Exception):
        status_code = 401
    c = _Client(exc=AuthenticationError("Incorrect API key provided: sk-abcdefghijklmnop"))
    with pytest.raises(LLMError) as ei:
        OpenAIAPIProvider("k", client=c).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "auth" and "sk-abcdefghijklmnop" not in ei.value.message


def test_strict_schema_requires_all():
    s = strict_schema(AGENT_SCHEMA)
    assert s["additionalProperties"] is False and set(s["required"]) == set(AGENT_SCHEMA["properties"])
    item = s["properties"]["root_cause_candidates"]["items"]
    assert item["additionalProperties"] is False


def test_parse_json_fenced():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_redaction():
    s = '{"access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijkl", "refresh_token":"rt_xyz"} sk-proj-ABCDEFGHIJKLMNOP Bearer abcdefghijklmn'
    out = redact(s)
    for bad in ("access_token", "refresh_token", "rt_xyz", "sk-proj", "abcdefghijklmn", "eyJhbGci"):
        assert bad not in out
    assert redact_obj({"k": ["sk-1234567890abcdef"]}) == {"k": ["[REDACTED_KEY]"]}
