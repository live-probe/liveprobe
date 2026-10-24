"""LLM provider interface and shared types."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Classified provider failure. `kind` in {timeout, auth, invalid_output, provider, config, quota}."""

    def __init__(self, kind: str, message: str, *, usage: "Usage | None" = None, duration_ms: int | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.usage = usage or Usage()
        self.duration_ms = duration_ms
        self.reset_seconds: float | None = None   # quota only: seconds until the limit resets, when known


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass
class LLMResult:
    text: str
    data: dict[str, Any] | None
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    provider: str = ""
    model: str = ""
    effort: str | None = None


class Provider(Protocol):
    name: str

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str,
                 effort: str | None, timeout: int) -> LLMResult: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output (tolerates ```json fences)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    t = t.strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise LLMError("invalid_output", "no JSON object in model output")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError("invalid_output", f"JSON decode error: {e.msg}") from e
    if not isinstance(obj, dict):
        raise LLMError("invalid_output", "model output is not an object")
    return obj


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI structured outputs require additionalProperties=false and all props required."""
    s = json.loads(json.dumps(schema))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if (t == "object" or (isinstance(t, list) and "object" in t)) and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s
