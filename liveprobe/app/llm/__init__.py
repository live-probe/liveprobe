"""Provider factory + role mapping + agent output schema."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..config import AppConfig, Settings
from .base import LLMError, LLMResult, Provider, Usage  # noqa: F401
from .anthropic_api import AnthropicAPIProvider
from .claude_cli import ClaudeCLIProvider
from .codex_cli import CodexCLIProvider
from .openai_api import OpenAIAPIProvider


class RootCause(BaseModel):
    cause: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str]


class ActionOut(BaseModel):
    type: str
    target: str | None = None
    replicas: int | None = None
    memory_limit_mi: int | None = None
    version: str | None = None
    why: str = ""


class AgentOutput(BaseModel):
    summary: str
    root_cause_candidates: list[RootCause] = Field(max_length=5)
    counter_evidence: list[str]
    confidence: float = Field(ge=0, le=1)
    next_action: str  # rollout_undo | act | handoff | none
    recommended_steps: list[str] = Field(default_factory=list)
    action: ActionOut | None = None   # always present in model output (null = no action)


class RouterOutput(BaseModel):
    level: str  # SIMPLE | NORMAL | COMPLEX | HIGH_RISK
    rationale: str
    confidence: float = Field(ge=0, le=1)


AGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "counter_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "next_action": {"type": "string", "enum": ["rollout_undo", "act", "handoff", "none"]},
        "recommended_steps": {"type": "array", "items": {"type": "string"}},
        "action": {"type": ["object", "null"], "properties": {
            "type": {"type": "string"}, "target": {"type": ["string", "null"]},
            "replicas": {"type": ["integer", "null"]}, "memory_limit_mi": {"type": ["integer", "null"]},
            "version": {"type": ["string", "null"]}, "why": {"type": "string"}}},
    },
}

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["SIMPLE", "NORMAL", "COMPLEX", "HIGH_RISK"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
    },
}


def make_provider(cfg: AppConfig, settings: Settings) -> Provider:
    p = cfg.llm.provider
    if p == "codex_cli":
        primary = CodexCLIProvider(settings.codex_bin, settings.codex_home)
        import os
        # A spare is registered only when its login exists: an account that cannot authenticate must not be
        # advertised as available (it would turn a primary limit into an auth failure instead of a clean wait).
        if not settings.codex_home_spare or not os.path.isfile(os.path.join(settings.codex_home_spare, "auth.json")):
            return primary
        from .codex_pool import Account, CodexPoolProvider
        return CodexPoolProvider([Account("primary", primary, settings.codex_home),
                                  Account("spare", CodexCLIProvider(settings.codex_bin, settings.codex_home_spare),
                                          settings.codex_home_spare)])
    if p == "claude_cli":
        return ClaudeCLIProvider(settings.claude_bin, settings.claude_oauth_token,
                                 settings.claude_oauth_token_file)
    if p == "openai_api":
        return OpenAIAPIProvider(settings.openai_api_key, api_key_file=settings.openai_api_key_file)
    if p == "anthropic_api":
        return AnthropicAPIProvider(settings.anthropic_api_key, settings.anthropic_api_key_file)
    raise LLMError("config", f"unknown llm.provider {p!r}")


def estimate_cost(cfg: AppConfig, model: str, usage: Usage) -> float | None:
    """API-list-price estimate in USD from the versioned price table; None when price/usage unknown.

    `input_tokens` is the total prompt size (cached included); cached tokens are billed at
    `cached_input` when both are known, otherwise everything is billed at the full input rate.
    """
    price = cfg.llm.prices.get(model)
    if not price or usage.input_tokens is None or "input" not in price or "output" not in price:
        return None
    cached = usage.cached_input_tokens or 0
    cached_rate = price.get("cached_input")
    if cached_rate is None or cached > usage.input_tokens:
        cached = 0
    fresh = usage.input_tokens - cached
    return round(fresh / 1e6 * price["input"] + cached / 1e6 * (cached_rate or 0)
                 + (usage.output_tokens or 0) / 1e6 * price["output"], 6)
