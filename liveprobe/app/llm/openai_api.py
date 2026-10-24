"""OpenAI API provider (API key) using the Responses API with JSON-schema output."""
from __future__ import annotations

import time
from typing import Any

from ..redact import redact, redact_obj
from .base import LLMError, LLMResult, Usage, parse_json_object, strict_schema
from .secrets import resolve_secret


class OpenAIAPIProvider:
    name = "openai_api"

    def __init__(self, api_key: str | None = None, client: Any = None,
                 api_key_file: str | None = None):
        self._api_key = api_key
        self._api_key_file = api_key_file
        self._client = client

    def _get_client(self, timeout: int, key: str | None = None):
        if self._client is not None:
            return self._client
        key = key or resolve_secret(self._api_key, self._api_key_file)
        if not key:
            raise LLMError("config", "OPENAI_API_KEY is not configured")
        from openai import OpenAI
        try:
            return OpenAI(api_key=key, timeout=timeout, max_retries=1)
        except Exception:
            # SDK initialization errors may include its options (including api_key).
            raise LLMError("config", "OpenAI client initialization failed") from None

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str,
                 effort: str | None, timeout: int) -> LLMResult:
        key = resolve_secret(self._api_key, self._api_key_file) if self._client is None else self._api_key
        client = self._get_client(timeout, key)
        t0 = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "text": {"format": {"type": "json_schema", "name": role, "schema": strict_schema(schema),
                                "strict": True}},
        }
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        try:
            resp = client.responses.create(**kwargs)
        except Exception as e:  # SDK exceptions may contain request headers or arbitrary key values.
            name = type(e).__name__
            status = getattr(e, "status_code", None)
            status = status if isinstance(status, int) else None
            kind = ("timeout" if "Timeout" in name or status in (408, 504) else
                    "auth" if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError")
                    else "provider")
            # Never return an SDK exception body: it can include credentials in an unknown format.
            raise LLMError(kind, f"OpenAI request failed ({name}, status={status})",
                           duration_ms=int((time.monotonic() - t0) * 1000)) from None
        duration_ms = int((time.monotonic() - t0) * 1000)
        u = getattr(resp, "usage", None)
        details = getattr(u, "input_tokens_details", None)
        output_details = getattr(u, "output_tokens_details", None)
        usage = Usage(input_tokens=getattr(u, "input_tokens", None),
                      output_tokens=getattr(u, "output_tokens", None),
                      cached_input_tokens=getattr(details, "cached_tokens", None),
                      reasoning_output_tokens=getattr(output_details, "reasoning_tokens", None))
        if getattr(resp, "status", None) not in (None, "completed"):
            raise LLMError("invalid_output", "response not completed",
                           usage=usage, duration_ms=duration_ms)
        if any(getattr(part, "type", None) == "refusal"
               for item in (getattr(resp, "output", None) or [])
               for part in (getattr(item, "content", None) or [])):
            raise LLMError("invalid_output", "response refused", usage=usage, duration_ms=duration_ms)
        text = getattr(resp, "output_text", None) or ""
        try:
            data = parse_json_object(text)
        except LLMError as e:
            e.usage, e.duration_ms = usage, duration_ms
            raise
        # Provider output is untrusted and may echo a credential. Sanitize before it can
        # reach the engine, DB or API, including nonstandard test/rotated key formats.
        if key and len(key) >= 8:
            text = text.replace(key, "[REDACTED_KEY]")
            data = _replace_secret(data, key)
        return LLMResult(text=redact(text) or "", data=redact_obj(data), usage=usage,
                         duration_ms=duration_ms, provider=self.name, model=model, effort=effort)


def _replace_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED_KEY]")
    if isinstance(value, list):
        return [_replace_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {_replace_secret(key, secret) if isinstance(key, str) else key:
                _replace_secret(item, secret) for key, item in value.items()}
    return value
