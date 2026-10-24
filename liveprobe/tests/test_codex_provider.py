"""Codex JSONL failure fixtures: never persist raw CLI diagnostics."""
import json
import stat

import pytest

from app.llm import LLMError
from app.llm.codex_cli import CodexCLIProvider


@pytest.fixture
def fake_codex(tmp_path):
    def build(events, rc=0, stderr=""):
        output = tmp_path / "events"
        output.write_text("\n".join(json.dumps(event) for event in events))
        script = tmp_path / "codex"
        script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{output}'\nprintf '%s' '{stderr}' >&2\nexit {rc}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return CodexCLIProvider(str(script), str(tmp_path))
    return build


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}
GOOD = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"summary":"ok"}'}}


@pytest.mark.parametrize("events,rc,stderr,kind", [
    ([GOOD, {"type": "turn.failed", "error": {"message": "401 token expired opaque-value"}}], 1, "", "auth"),
    ([GOOD], 0, "", "provider"),
    ([GOOD, {"type": "turn.completed"}], 1, "opaque-value", "provider"),
    ([{"type": "turn.failed", "error": "403 unauthorized opaque-value"}], 1, "", "auth"),
])
def test_failed_or_incomplete_turn_is_safe(fake_codex, events, rc, stderr, kind):
    with pytest.raises(LLMError) as caught:
        fake_codex(events, rc, stderr).complete("r", "prompt", SCHEMA, "m", None, 5)
    assert caught.value.kind == kind
    assert "opaque-value" not in str(caught.value)


def test_schema_violation_is_classified_without_echoing_output(fake_codex):
    bad = {"type": "item.completed", "item": {"type": "agent_message", "text":
           '{"summary":"ok","extra":"opaque-value"}'}}
    with pytest.raises(LLMError) as caught:
        fake_codex([bad, {"type": "turn.completed", "usage": {"input_tokens": 7}}]).complete(
            "r", "prompt", SCHEMA, "m", None, 5)
    assert caught.value.kind == "invalid_output" and caught.value.usage.input_tokens == 7
    assert "opaque-value" not in str(caught.value)


def test_child_env_excludes_other_secret_sources(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "opaque-value")
    monkeypatch.setenv("LIVEPROBE_DB_PASSWORD", "opaque-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "opaque-value")
    provider = fake_codex([GOOD, {"type": "turn.completed"}])
    script = tmp_path / "codex"
    env_path = tmp_path / "child-env"
    script.write_text(script.read_text().replace("cat > /dev/null", f"env > '{env_path}'; cat > /dev/null"))
    provider.complete("r", "prompt", SCHEMA, "m", None, 5)
    child_env = env_path.read_text()
    assert "opaque-value" not in child_env
    assert "CODEX_HOME=" + str(tmp_path) in child_env
    assert "LIVEPROBE_DB_PASSWORD" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert all(not line.startswith("HOME=" + str(tmp_path) + "\n") for line in child_env.splitlines(keepends=True))
