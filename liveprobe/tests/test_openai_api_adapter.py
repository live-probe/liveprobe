"""Offline Responses API fixtures: success, classification and non-leakage."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from app.llm import AGENT_SCHEMA, AgentOutput, LLMError
from app.llm.openai_api import OpenAIAPIProvider
from app.redact import redact_obj

# Deliberately synthetic and not shaped like an SDK key, to catch format-only redaction.
SYNTHETIC_KEY = "fixture-secret-opaque-7392"
VALID = {"summary": "ImagePullBackOff", "root_cause_candidates": [],
         "counter_evidence": [], "confidence": 0.9, "next_action": "handoff",
         "recommended_steps": []}


class ResponsesFixture:
    def __init__(self, response=None, error=None):
        self.responses = self
        self.response = response
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def response(data=VALID, **overrides):
    defaults = dict(output_text=json.dumps(data), status="completed", output=[],
                    usage=NS(input_tokens=44, output_tokens=12,
                             input_tokens_details=NS(cached_tokens=10),
                             output_tokens_details=NS(reasoning_tokens=3)))
    defaults.update(overrides)
    return NS(**defaults)


def invoke(client, key=SYNTHETIC_KEY):
    return OpenAIAPIProvider(key, client=client).complete(
        "signal_analyst", "fixture prompt", AGENT_SCHEMA, "gpt-fixture", "low", 2)


def test_structured_success_usage_and_schema():
    client = ResponsesFixture(response())
    result = invoke(client)
    assert AgentOutput.model_validate(result.data).summary == "ImagePullBackOff"
    assert (result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cached_input_tokens, result.usage.reasoning_output_tokens) == (44, 12, 10, 3)
    assert result.provider == "openai_api" and result.duration_ms >= 0
    assert client.kwargs is not None
    assert client.kwargs["text"]["format"]["strict"] is True
    assert client.kwargs["text"]["format"]["schema"]["additionalProperties"] is False
    assert client.kwargs["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("exception,expected", [
    (type("AuthenticationError", (Exception,), {"status_code": 401})(SYNTHETIC_KEY), "auth"),
    (type("PermissionDeniedError", (Exception,), {"status_code": 403})(SYNTHETIC_KEY), "auth"),
    (type("APITimeoutError", (Exception,), {})(SYNTHETIC_KEY), "timeout"),
    (type("APIStatusError", (Exception,), {"status_code": 504})(SYNTHETIC_KEY), "timeout"),
    (type("APIStatusError", (Exception,), {"status_code": 429})(SYNTHETIC_KEY), "provider"),
])
def test_errors_classified_without_exception_body(exception, expected):
    with pytest.raises(LLMError) as info:
        invoke(ResponsesFixture(error=exception))
    assert info.value.kind == expected
    assert info.value.duration_ms is not None
    assert SYNTHETIC_KEY not in str(info.value)
    assert SYNTHETIC_KEY not in str(redact_obj({"error": info.value.message}))


@pytest.mark.parametrize("fixture", [
    response(status="incomplete", incomplete_details=NS(reason="max_output_tokens")),
    response(output=[NS(content=[NS(type="refusal")])]),
    response(output_text="no JSON object"),
])
def test_incomplete_refused_and_malformed_are_invalid_output(fixture):
    with pytest.raises(LLMError) as info:
        invoke(ResponsesFixture(fixture))
    assert info.value.kind == "invalid_output"
    assert info.value.usage.input_tokens == 44
    assert info.value.duration_ms is not None


def test_sdk_initialization_failure_does_not_leak_key(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError(f"initialization failed: {kwargs['api_key']}")

    monkeypatch.setattr("openai.OpenAI", fail)
    with pytest.raises(LLMError) as info:
        OpenAIAPIProvider(SYNTHETIC_KEY)._get_client(2)
    assert info.value.kind == "config"
    assert SYNTHETIC_KEY not in str(info.value)


def test_output_credential_redacted_before_persistence_or_api():
    raw = {**VALID, "summary": f"provider echoed {SYNTHETIC_KEY}"}
    result = invoke(ResponsesFixture(response(raw)))
    db_record = {"output": redact_obj(result.data), "error": None}
    api_output = redact_obj(db_record)
    assert SYNTHETIC_KEY not in result.text
    assert SYNTHETIC_KEY not in json.dumps(db_record)
    assert SYNTHETIC_KEY not in json.dumps(api_output)
    assert AgentOutput.model_validate(result.data).summary.endswith("[REDACTED_KEY]")
