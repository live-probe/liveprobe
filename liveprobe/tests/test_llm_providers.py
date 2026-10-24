"""KAN-51: Claude Code CLI / Anthropic API providers, role map, price table, redaction.

No real credentials are used: the CLI is a fake executable and the SDK client is a stub.
Dummy "secrets" below are synthetic test strings.
"""
from __future__ import annotations

import json
import stat
import textwrap
from types import SimpleNamespace

import httpx2 as httpx  # the anthropic SDK is built on httpx2
import pytest

from app.config import LLMConfig, Settings
from app.llm import AGENT_SCHEMA, AgentOutput, LLMError, Usage, estimate_cost, make_provider
from app.llm.anthropic_api import AnthropicAPIProvider
from app.llm.claude_cli import ClaudeCLIProvider
from app.llm.openai_api import OpenAIAPIProvider
from app.llm.secrets import resolve_secret
from app.redact import redact

GOOD = {"summary": "s", "root_cause_candidates": [{"cause": "bad tag", "confidence": 0.9, "evidence_refs": ["E2_pods"]}],
        "counter_evidence": [], "confidence": 0.9, "next_action": "rollout_undo", "recommended_steps": [], "action": None}
DUMMY_TOKEN = "sk-ant-oat01-DUMMYdummyDUMMYdummy0123456789"
DUMMY_KEY = "sk-ant-api03-DUMMYdummyDUMMYdummy0123456789"


def fake_claude(tmp_path, body: str) -> str:
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def result_event(**kw) -> str:
    ev = {"type": "result", "subtype": "success", "is_error": False, "api_error_status": None,
          "result": json.dumps(GOOD), "structured_output": GOOD, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 200,
                    "output_tokens": 120}}
    ev.update(kw)
    return json.dumps(ev)


# ----------------------------------------------------------------- Claude Code CLI (KAN-76)
def test_claude_cli_success_structured_output_and_isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-dummy-0123456789")
    (tmp_path / "out.json").write_text(result_event())
    args_f, env_f, stdin_f = tmp_path / "args", tmp_path / "env", tmp_path / "stdin"
    bin_ = fake_claude(tmp_path, f"""
        printf '%s\\n' "$@" > {args_f}
        env > {env_f}
        cat > {stdin_f}
        cat {tmp_path / 'out.json'}
        """)
    r = ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("result_verifier", "PROMPT", AGENT_SCHEMA,
                                                     "claude-sonnet-5", "low", 10)
    AgentOutput.model_validate(r.data)
    assert r.provider == "claude_cli" and r.model == "claude-sonnet-5" and r.effort == "low"
    # input = base + cache creation + cache read; cached = cache read
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_input_tokens) == (1210, 120, 200)
    args = args_f.read_text().splitlines()
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config"):
        assert flag in args
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--tools") + 1] == "" and args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-sonnet-5" and args[args.index("--effort") + 1] == "low"
    schema = json.loads(args[args.index("--json-schema") + 1])
    assert schema["additionalProperties"] is False
    assert DUMMY_TOKEN not in " ".join(args)  # token only via env, never argv
    assert stdin_f.read_text() == "PROMPT"
    env = dict(line.split("=", 1) for line in env_f.read_text().splitlines() if "=" in line)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == DUMMY_TOKEN
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env  # no silent API-billing fallback
    assert "lp-claude-" in env["HOME"] and env["CLAUDE_CONFIG_DIR"].startswith(env["HOME"])


def test_claude_cli_no_effort_flag_when_none(tmp_path):
    (tmp_path / "o").write_text(result_event())
    args_f = tmp_path / "args"
    bin_ = fake_claude(tmp_path, f"printf '%s\\n' \"$@\" > {args_f}; cat > /dev/null; cat {tmp_path / 'o'}\n")
    ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("router", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 10)
    assert "--effort" not in args_f.read_text().splitlines()


def test_claude_cli_falls_back_to_result_text_when_no_structured_output(tmp_path):
    (tmp_path / "o").write_text(result_event(structured_output=None, result="```json\n" + json.dumps(GOOD) + "\n```"))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}\n")
    r = ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert r.data == GOOD


def test_claude_cli_invalid_output_keeps_usage(tmp_path):
    (tmp_path / "o").write_text(result_event(structured_output=None, result="not json"))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert ei.value.kind == "invalid_output" and ei.value.usage.output_tokens == 120


@pytest.mark.parametrize("ev", [
    # shapes observed from claude 2.1.x with an invalid token and with no credentials at all
    {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
     "result": "Failed to authenticate. API Error: 401 Invalid bearer token"},
    {"type": "result", "subtype": "success", "is_error": True, "api_error_status": None,
     "result": "Not logged in · Please run /login"},
    {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
     "result": f"OAuth token has expired. token={DUMMY_TOKEN}"},
])
def test_claude_cli_expired_or_missing_auth_is_classified_and_redacted(tmp_path, ev):
    (tmp_path / "o").write_text(json.dumps(ev))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}; exit 1\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert ei.value.kind == "auth"
    assert DUMMY_TOKEN not in ei.value.message and "sk-ant" not in ei.value.message


def test_claude_cli_unsupported_model_is_provider_error_with_leading_diagnostic(tmp_path):
    # Shape observed from real claude 2.1.267 with --model claude-opus-5-5 (needs >= 2.1.280).
    ev = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 400,
          "result": "API Error: 400 Claude Code 2.1.267 does not support this model; version 2.1.280 or newer "
                    "is required.", "usage": {"input_tokens": 0, "output_tokens": 0}}
    (tmp_path / "o").write_text('[claude-code:unrecognized_model] {"model":"claude-opus-5-5"}\n' + json.dumps(ev))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}; exit 1\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "claude-opus-5-5", "high", 10)
    assert ei.value.kind == "provider" and "api_error_status=400" in ei.value.message


def test_claude_cli_provider_error_and_stderr_redaction(tmp_path):
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; echo 'boom {DUMMY_TOKEN}' >&2; exit 3\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert ei.value.kind == "provider" and "boom" in ei.value.message and DUMMY_TOKEN not in ei.value.message


def test_claude_cli_timeout_kills_process_group(tmp_path):
    pidf = tmp_path / "child.pid"
    bin_ = fake_claude(tmp_path, f"sleep 60 & echo $! > {pidf}; wait\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 1)
    assert ei.value.kind == "timeout"
    import os
    import time
    child = int(pidf.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_claude_cli_missing_token_is_config_error(tmp_path):
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider("claude", None, str(tmp_path / "absent")).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "config"


def test_claude_cli_missing_binary():
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider("/nonexistent/claude", DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "config"


def test_claude_cli_rereads_rotated_secret_file(tmp_path):
    tok_file = tmp_path / "CLAUDE_CODE_OAUTH_TOKEN"
    tok_file.write_text("sk-ant-oat01-first-dummy-value\n")
    (tmp_path / "o").write_text(result_event())
    env_f = tmp_path / "env"
    bin_ = fake_claude(tmp_path, f"env > {env_f}; cat > /dev/null; cat {tmp_path / 'o'}\n")
    prov = ClaudeCLIProvider(bin_, "sk-ant-oat01-env-fallback", str(tok_file))
    prov.complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-first-dummy-value" in env_f.read_text()
    tok_file.write_text("sk-ant-oat01-rotated-dummy-value")  # kubelet refresh of the Secret volume
    prov.complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-rotated-dummy-value" in env_f.read_text()


def test_resolve_secret_precedence(tmp_path):
    f = tmp_path / "k"
    assert resolve_secret("env", str(f)) == "env"
    f.write_text("  \n")
    assert resolve_secret("env", str(f)) == "env"
    f.write_text("file\n")
    assert resolve_secret("env", str(f)) == "file"
    assert resolve_secret(None, None) is None


def test_claude_cli_rejects_schema_violation_even_with_structured_output(tmp_path):
    bad = {k: v for k, v in GOOD.items() if k != "confidence"}
    (tmp_path / "o").write_text(result_event(structured_output=bad, result=json.dumps(bad)))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert ei.value.kind == "invalid_output" and ei.value.usage.output_tokens == 120
    assert "confidence" not in ei.value.message


def test_claude_cli_success_scrubs_token_from_text_and_nested_data(tmp_path):
    output = {**GOOD, "root_cause_candidates": [
        {"cause": f"observed {DUMMY_TOKEN}", "confidence": 0.9, "evidence_refs": ["E2_pods"]}]}
    (tmp_path / "o").write_text(result_event(structured_output=output, result=json.dumps(output)))
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}\n")
    r = ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert DUMMY_TOKEN not in r.text and DUMMY_TOKEN not in json.dumps(r.data)
    assert "[REDACTED]" in json.dumps(r.data)


def test_claude_cli_nonzero_exit_does_not_accept_success_envelope(tmp_path):
    (tmp_path / "o").write_text(result_event())
    bin_ = fake_claude(tmp_path, f"cat > /dev/null; cat {tmp_path / 'o'}; exit 1\n")
    with pytest.raises(LLMError) as ei:
        ClaudeCLIProvider(bin_, DUMMY_TOKEN).complete("r", "p", AGENT_SCHEMA, "m", None, 10)
    assert ei.value.kind == "provider"


# ----------------------------------------------------------------- Anthropic API (KAN-77)
class _Msgs:
    def __init__(self, resp=None, exc=None):
        self.kw, self.resp, self.exc = None, resp, exc

    def create(self, **kw):
        self.kw = kw
        if self.exc:
            raise self.exc
        return self.resp


def _resp(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
        usage=SimpleNamespace(input_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=30,
                              output_tokens=40))


def test_anthropic_api_structured_output_request_and_usage():
    msgs = _Msgs(_resp(json.dumps(GOOD)))
    r = AnthropicAPIProvider(client=SimpleNamespace(messages=msgs)).complete(
        "safety_verifier", "PROMPT", AGENT_SCHEMA, "claude-opus-5", "high", 30)
    AgentOutput.model_validate(r.data)
    assert r.provider == "anthropic_api" and r.model == "claude-opus-5" and r.effort == "high"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_input_tokens) == (80, 40, 30)
    kw = msgs.kw
    assert kw["model"] == "claude-opus-5" and kw["messages"] == [{"role": "user", "content": "PROMPT"}]
    oc = kw["output_config"]
    assert oc["effort"] == "high" and oc["format"]["type"] == "json_schema"
    assert oc["format"]["schema"]["additionalProperties"] is False


def test_anthropic_api_no_effort_when_none():
    msgs = _Msgs(_resp(json.dumps(GOOD)))
    AnthropicAPIProvider(client=SimpleNamespace(messages=msgs)).complete("router", "p", AGENT_SCHEMA,
                                                                        "claude-haiku-4-5", None, 30)
    assert "effort" not in msgs.kw["output_config"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_anthropic_api_refusal_or_truncation_is_invalid_output(stop):
    msgs = _Msgs(_resp("{", stop=stop))
    with pytest.raises(LLMError) as ei:
        AnthropicAPIProvider(client=SimpleNamespace(messages=msgs)).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "invalid_output" and ei.value.usage.output_tokens == 40


def _sdk_errors():
    import anthropic
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_err(cls, code):
        return cls(f"Error code: {code} invalid x-api-key {DUMMY_KEY}", response=httpx.Response(code, request=req),
                   body=None)
    return [
        (status_err(anthropic.AuthenticationError, 401), "auth"),
        (status_err(anthropic.PermissionDeniedError, 403), "auth"),
        (anthropic.APITimeoutError(request=req), "timeout"),
        (status_err(anthropic.RateLimitError, 429), "provider"),
        (status_err(anthropic.InternalServerError, 500), "provider"),
    ]


@pytest.mark.parametrize("exc,kind", _sdk_errors())
def test_anthropic_api_real_sdk_errors_classified_and_redacted(exc, kind):
    msgs = _Msgs(exc=exc)
    with pytest.raises(LLMError) as ei:
        AnthropicAPIProvider(client=SimpleNamespace(messages=msgs)).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == kind and DUMMY_KEY not in ei.value.message and "sk-ant" not in ei.value.message


def test_anthropic_api_without_key_is_config_error(tmp_path):
    with pytest.raises(LLMError) as ei:
        AnthropicAPIProvider(None, str(tmp_path / "absent")).complete("r", "p", AGENT_SCHEMA, "m", None, 5)
    assert ei.value.kind == "config"


def test_anthropic_api_builds_real_sdk_client_from_secret_file(tmp_path):
    f = tmp_path / "ANTHROPIC_API_KEY"
    f.write_text(DUMMY_KEY)
    client = AnthropicAPIProvider(None, str(f))._get_client(30)
    assert type(client).__name__ == "Anthropic" and client.max_retries == 1


def test_openai_api_secret_file_rotation_and_factory(tmp_path, monkeypatch):
    from app.config import AppConfig
    from openai import OpenAI

    f = tmp_path / "OPENAI_API_KEY"
    f.write_text("file-one\n")  # synthetic test value, never a credential
    seen = []
    def fake_client(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr("openai.OpenAI", fake_client)
    p = make_provider(AppConfig.model_validate({"llm": {"provider": "openai_api"}}),
                      Settings(openai_api_key="env-fallback", openai_api_key_file=str(f)))
    assert isinstance(p, OpenAIAPIProvider)
    p._get_client(12)
    f.write_text("file-two\n")
    p._get_client(12)
    assert [x["api_key"] for x in seen] == ["file-one", "file-two"]
    assert all(x["timeout"] == 12 and x["max_retries"] == 1 for x in seen)
    f.unlink()
    p._get_client(12)
    assert seen[-1]["api_key"] == "env-fallback"


def test_openai_api_missing_file_without_fallback_is_config(tmp_path):
    with pytest.raises(LLMError) as ei:
        OpenAIAPIProvider(api_key_file=str(tmp_path / "missing"))._get_client(5)
    assert ei.value.kind == "config"


def test_llm_secret_mount_is_read_only_and_optional_keys(tmp_path):
    import yaml
    from pathlib import Path
    manifest = Path(__file__).resolve().parents[2] / "deploy/base/liveprobe/liveprobe.yaml"
    deploy = next(d for d in yaml.safe_load_all(manifest.read_text())
                  if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "liveprobe-api")
    pod = deploy["spec"]["template"]["spec"]
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "llm-auth")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == "/var/run/secrets/liveprobe-llm"
    assert next(v for v in pod["volumes"] if v["name"] == "llm-auth")["secret"]["secretName"] == "liveprobe-llm"
    assert not any(e["name"] in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
                   for e in pod["containers"][0]["env"])


# ----------------------------------------------------------------- factory / role map / prices
@pytest.mark.parametrize("name,cls", [("codex_cli", "CodexCLIProvider"), ("claude_cli", "ClaudeCLIProvider"),
                                      ("openai_api", "OpenAIAPIProvider"), ("anthropic_api", "AnthropicAPIProvider")])
def test_factory_all_four_providers(name, cls):
    from app.config import AppConfig
    cfg = AppConfig.model_validate({"llm": {"provider": name}})
    p = make_provider(cfg, Settings(claude_oauth_token_file=None, anthropic_api_key_file=None))
    assert type(p).__name__ == cls and p.name == name


def test_role_map_follows_provider_and_rejects_cross_vendor_models():
    assert LLMConfig(provider="codex_cli").roles["router"].model == "gpt-6-luna"
    a = LLMConfig(provider="anthropic_api")
    assert a.roles["router"].model == "claude-haiku-4-5" and a.roles["router"].effort is None
    assert a.roles["safety_verifier"].model == "claude-opus-5-5"
    assert set(a.roles) == set(LLMConfig(provider="openai_api").roles)
    # legacy ConfigMap: explicit OpenAI roles + provider switch to claude -> loud config error
    with pytest.raises(ValueError):
        LLMConfig.model_validate({"provider": "claude_cli", "roles": {"router": {"model": "gpt-6-luna"}}})
    # explicit provider_roles for the active provider wins over legacy roles
    c = LLMConfig.model_validate({"provider": "claude_cli", "roles": {"router": {"model": "gpt-6-luna"}},
                                  "provider_roles": {"claude_cli": {"router": {"model": "claude-sonnet-5"}}}})
    assert c.roles == {"router": c.roles["router"]} and c.roles["router"].model == "claude-sonnet-5"
    with pytest.raises(ValueError):
        LLMConfig(provider="gemini_api")


def test_existing_configmap_shape_still_valid():
    c = LLMConfig.model_validate({"provider": "codex_cli", "timeout_s": 180,
                                  "roles": {"router": {"model": "gpt-6-luna", "effort": "low"}}})
    assert c.roles["router"].model == "gpt-6-luna"


def test_estimate_cost_versioned_prices_and_unknown():
    from app.config import AppConfig
    cfg = AppConfig()
    assert cfg.llm.prices_version == "2026-10-18"
    # 1M fresh input + 1M output on sonnet 5 = 2 + 10
    assert estimate_cost(cfg, "claude-sonnet-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)) == 12.0
    # cached part billed at cache-read rate: 0.5M fresh * 1 + 0.5M cached * 0.1 + 0 output
    assert estimate_cost(cfg, "claude-haiku-4-5", Usage(input_tokens=1_000_000, output_tokens=0,
                                                        cached_input_tokens=500_000)) == 0.55
    assert estimate_cost(cfg, "gpt-6-luna", Usage(input_tokens=1_000_000, output_tokens=0)) == 0.1
    assert estimate_cost(cfg, "unknown-model", Usage(input_tokens=10, output_tokens=10)) is None
    assert estimate_cost(cfg, "claude-sonnet-5", Usage()) is None
    for role_map in cfg.llm.provider_roles.values():
        for rm in role_map.values():
            assert rm.model in cfg.llm.prices, rm.model


def test_redaction_covers_anthropic_credentials():
    s = (f"x-api-key: {DUMMY_KEY} ANTHROPIC_API_KEY={DUMMY_KEY} "
         f'{{"CLAUDE_CODE_OAUTH_TOKEN": "{DUMMY_TOKEN}"}} CLAUDE_CODE_OAUTH_TOKEN=abc123def456')
    out = redact(s)
    assert DUMMY_KEY not in out and DUMMY_TOKEN not in out and "abc123def456" not in out and "sk-ant" not in out
