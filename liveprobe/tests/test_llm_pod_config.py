"""The deployed ConfigMap must be switchable without a cross-vendor role mismatch."""
from pathlib import Path

import pytest
import yaml

from app.config import AppConfig, LLMConfig, Settings
from app.llm import make_provider


MANIFEST = Path(__file__).resolve().parents[2] / "deploy/base/liveprobe/liveprobe.yaml"


def _deployed_config() -> dict:
    configmap = next(doc for doc in yaml.safe_load_all(MANIFEST.read_text())
                     if doc and doc.get("kind") == "ConfigMap"
                     and doc["metadata"]["name"] == "liveprobe-config")
    return yaml.safe_load(configmap["data"]["config.yaml"])


@pytest.mark.parametrize("provider,vendor", [
    ("codex_cli", "gpt-"), ("openai_api", "gpt-"),
    ("claude_cli", "claude-"), ("anthropic_api", "claude-"),
])
def test_deployed_config_selects_provider_and_matching_models(provider, vendor):
    config = _deployed_config()
    config["llm"]["provider"] = provider
    cfg = AppConfig.model_validate(config)
    assert cfg.llm.roles
    assert all(role.model.startswith(vendor) for role in cfg.llm.roles.values())
    assert make_provider(cfg, Settings()).name == provider


def test_deployed_config_rejects_unknown_provider():
    config = _deployed_config()
    config["llm"]["provider"] = "unknown"
    with pytest.raises(ValueError, match="llm.provider"):
        AppConfig.model_validate(config)


def test_explicit_provider_roles_override_defaults_without_cross_vendor_fallback():
    cfg = LLMConfig.model_validate({
        "provider": "claude_cli",
        "provider_roles": {"claude_cli": {"router": {"model": "claude-sonnet-5", "effort": "low"}}},
    })
    assert cfg.roles["router"].model == "claude-sonnet-5"
    with pytest.raises(ValueError, match="do not belong"):
        LLMConfig.model_validate({
            "provider": "claude_cli", "roles": {"router": {"model": "gpt-6-luna"}},
        })
