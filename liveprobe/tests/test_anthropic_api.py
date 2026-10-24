"""Isolated Anthropic API contract and credential-safety fixtures (no network or real keys)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import AppConfig, Settings
from app.llm import AGENT_SCHEMA, AgentOutput, LLMError, Usage, estimate_cost, make_provider
from app.llm.anthropic_api import AnthropicAPIProvider


class Messages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def response(text: str, *, stop: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
        usage=SimpleNamespace(input_tokens=100, cache_creation_input_tokens=20,
                              cache_read_input_tokens=30, output_tokens=40),
    )


OUTPUT = {"summary": "rollout failed", "root_cause_candidates": [], "counter_evidence": [],
          "confidence": 0.9, "next_action": "handoff", "recommended_steps": [], "action": None}


def test_factory_structured_request_usage_and_versioned_cost():
    cfg = AppConfig.model_validate({"llm": {"provider": "anthropic_api"}})
    msgs = Messages(response(json.dumps(OUTPUT)))
    provider = make_provider(cfg, Settings(anthropic_api_key_file=None))
    assert isinstance(provider, AnthropicAPIProvider)
    provider._client = SimpleNamespace(messages=msgs)
    model = cfg.llm.roles["safety_verifier"]
    result = provider.complete("safety_verifier", "diagnose", AGENT_SCHEMA, model.model, model.effort, 10)
    AgentOutput.model_validate(result.data)
    assert (result.provider, result.model, result.effort) == ("anthropic_api", "claude-opus-5-5", "high")
    assert (result.usage.input_tokens, result.usage.cached_input_tokens, result.usage.output_tokens) == (150, 30, 40)
    assert estimate_cost(cfg, result.model, result.usage) is not None
    assert cfg.llm.prices_version
    sent = msgs.calls[0]
    assert sent["messages"] == [{"role": "user", "content": "diagnose"}]
    assert sent["output_config"]["effort"] == "high"
    assert sent["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_rotated_secret_is_loaded_for_each_request(tmp_path, monkeypatch):
    key_file = tmp_path / "ANTHROPIC_API_KEY"
    key_file.write_text("synthetic-one")
    seen = []

    def client(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(messages=Messages(response(json.dumps(OUTPUT))))

    monkeypatch.setattr("anthropic.Anthropic", client)
    provider = AnthropicAPIProvider(api_key="synthetic-fallback", api_key_file=str(key_file))
    provider.complete("router", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 8)
    key_file.write_text("synthetic-two")
    provider.complete("router", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 8)
    assert [item["api_key"] for item in seen] == ["synthetic-one", "synthetic-two"]
    assert all(item["timeout"] == 8 and item["max_retries"] == 1 for item in seen)


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (408, "timeout"),
                                          (429, "provider"), (500, "provider")])
def test_status_errors_never_surface_arbitrary_key(status, kind):
    marker = "syntheticKeyThatDoesNotMatchAnyRedactionPattern42"
    class StatusError(RuntimeError):
        def __init__(self):
            super().__init__(f"request header contained {marker}")
            self.status_code = status
    error = StatusError()
    provider = AnthropicAPIProvider(client=SimpleNamespace(messages=Messages(error=error)))
    with pytest.raises(LLMError) as exc:
        provider.complete("router", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 5)
    assert exc.value.kind == kind
    assert marker not in str(exc.value)
    assert exc.value.__cause__ is None


def test_timeout_exception_and_client_construction_are_sanitized(monkeypatch):
    marker = "syntheticSecretUnknownShape123"
    class SecretTimeout(Exception):
        pass
    msgs = Messages(error=SecretTimeout(marker))
    provider = AnthropicAPIProvider(client=SimpleNamespace(messages=msgs))
    with pytest.raises(LLMError) as exc:
        provider.complete("r", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 5)
    assert exc.value.kind == "timeout" and marker not in str(exc.value)

    def broken_client(**kwargs):
        raise ValueError(marker)
    monkeypatch.setattr("anthropic.Anthropic", broken_client)
    with pytest.raises(LLMError) as exc:
        AnthropicAPIProvider(api_key=marker).complete("r", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 5)
    assert exc.value.kind == "provider" and marker not in str(exc.value)


@pytest.mark.parametrize("raw,stop", [("not-json", "end_turn"), ("{}", "end_turn"),
                                          ("{}", "refusal"), ("{}", "max_tokens")])
def test_invalid_output_retains_usage_without_echoing_response(raw, stop):
    provider = AnthropicAPIProvider(client=SimpleNamespace(messages=Messages(response(raw, stop=stop))))
    with pytest.raises(LLMError) as exc:
        provider.complete("r", "p", AGENT_SCHEMA, "claude-haiku-4-5", None, 5)
    assert exc.value.kind == "invalid_output" and exc.value.usage.input_tokens == 150
    assert raw not in exc.value.message


def test_unknown_model_or_usage_does_not_invent_cost():
    cfg = AppConfig()
    assert estimate_cost(cfg, "unknown-model", Usage(input_tokens=1, output_tokens=1)) is None
    assert estimate_cost(cfg, "claude-haiku-4-5", Usage()) is None
