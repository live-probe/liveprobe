"""In-pod smoke test: `python -m app.llm.smoke [role] [--provider NAME]` -> one real call.

Prints only sanitized metadata (provider/model/usage/cost/duration/schema validity); never the
credential, the prompt or raw provider output.
"""
from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from ..config import get_app_config, get_settings
from ..redact import redact
from . import AGENT_SCHEMA, AgentOutput, LLMError, estimate_cost, make_provider


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("role", nargs="?", default="signal_analyst")
    ap.add_argument("--provider", help="override llm.provider (uses that provider's role map)")
    args = ap.parse_args(argv)
    cfg, settings = get_app_config(), get_settings()
    if args.provider:
        data = cfg.model_dump()
        data["llm"]["provider"] = args.provider
        data["llm"].pop("roles", None)  # re-derive from provider_roles[provider]
        cfg = type(cfg).model_validate(data)
    rm = cfg.llm.roles[args.role]
    prov = make_provider(cfg, settings)
    prompt = (
        "Smoke test. A Deployment 'demo' has a pod in ImagePullBackOff for image demo:does-not-exist "
        "right after a rollout from demo:1.0. Return your analysis. Use next_action='rollout_undo' "
        "if reverting the rollout is appropriate."
    )
    try:
        r = prov.complete(args.role, prompt, AGENT_SCHEMA, rm.model, rm.effort, cfg.llm.timeout_s)
        AgentOutput.model_validate(r.data)
    except LLMError as e:
        print(json.dumps({"ok": False, "provider": prov.name, "model": rm.model, "kind": e.kind,
                          "error": (redact(e.message) or "")[:300], "duration_ms": e.duration_ms}))
        return 1
    except ValidationError as e:
        print(json.dumps({"ok": False, "provider": prov.name, "model": rm.model, "kind": "invalid_output",
                          "error": f"schema validation failed ({e.error_count()} errors)"}))
        return 1
    print(json.dumps({
        "ok": True, "provider": r.provider, "model": r.model, "effort": r.effort,
        "input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
        "cached_input_tokens": r.usage.cached_input_tokens,
        "estimated_cost_usd": estimate_cost(cfg, r.model, r.usage), "prices_version": cfg.llm.prices_version,
        "duration_ms": r.duration_ms, "next_action": r.data.get("next_action"),
        "confidence": r.data.get("confidence"), "schema_valid": True,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
