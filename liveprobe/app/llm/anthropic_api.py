"""Anthropic Messages API provider (API key) with JSON-schema structured output."""
from __future__ import annotations

import time
from typing import Any

from jsonschema import ValidationError, validate

from .base import LLMError, LLMResult, Usage, parse_json_object, strict_schema, validation_schema
from .secrets import resolve_secret

SYSTEM_PROMPT = (
    "You are a component of an automated Kubernetes incident-analysis system. "
    "Answer only from the data in the user message and reply with a single JSON object "
    "that matches the provided output schema."
)


def classify_exception(e: Exception) -> str:
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    if "Timeout" in name or status in (408, 504):
        return "timeout"
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth"
    return "provider"


class AnthropicAPIProvider:
    name = "anthropic_api"

    def __init__(self, api_key: str | None = None, api_key_file: str | None = None, client: Any = None,
                 max_tokens: int = 8192):
        self._api_key = api_key
        self._api_key_file = api_key_file
        self._client = client
        self.max_tokens = max_tokens

    def _get_client(self, timeout: int):
        if self._client is not None:
            return self._client
        key = resolve_secret(self._api_key, self._api_key_file)
        if not key:
            raise LLMError("config", "ANTHROPIC_API_KEY is not configured")
        from anthropic import Anthropic
        return Anthropic(api_key=key, timeout=timeout, max_retries=1)

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str,
                 effort: str | None, timeout: int) -> LLMResult:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": strict_schema(schema)}}
        if effort:
            output_config["effort"] = effort
        t0 = time.monotonic()
        try:
            client = self._get_client(timeout)
            resp = client.messages.create(
                model=model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,  # type: ignore[arg-type]
            )
        except LLMError:
            raise
        except Exception as e:  # SDK failures may contain request headers or key values
            kind = classify_exception(e)
            status = getattr(e, "status_code", None)
            # Never propagate the SDK exception text: arbitrary credentials can appear in it,
            # including values that cannot be recognized by generic regex redaction.
            detail = f"HTTP {status}" if isinstance(status, int) else type(e).__name__
            raise LLMError(kind, f"Anthropic request failed ({detail})",
                           duration_ms=int((time.monotonic() - t0) * 1000)) from None
        duration_ms = int((time.monotonic() - t0) * 1000)
        u = getattr(resp, "usage", None)
        base_in = getattr(u, "input_tokens", None)
        cc = getattr(u, "cache_creation_input_tokens", None) or 0
        cr = getattr(u, "cache_read_input_tokens", None) or 0
        usage = Usage(input_tokens=None if base_in is None else base_in + cc + cr,
                      output_tokens=getattr(u, "output_tokens", None),
                      cached_input_tokens=cr if base_in is not None else None)
        text = "".join(getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])
                       if getattr(b, "type", None) == "text")
        stop = getattr(resp, "stop_reason", None)
        if stop in ("refusal", "max_tokens"):
            raise LLMError("invalid_output", f"stop_reason={stop}", usage=usage, duration_ms=duration_ms)
        try:
            data = parse_json_object(text)
            validate(data, validation_schema(schema))
        except ValidationError:
            raise LLMError("invalid_output", "schema validation failed", usage=usage,
                           duration_ms=duration_ms) from None
        except LLMError as e:
            e.usage, e.duration_ms = usage, duration_ms
            raise
        return LLMResult(text=text, data=data, usage=usage, duration_ms=duration_ms,
                         provider=self.name, model=model, effort=effort)
