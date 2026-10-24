"""Synthetic-only regression tests for the private Claude Secret input path."""
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "update-claude-secret.py"
spec = importlib.util.spec_from_file_location("update_claude_secret", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("raw, expected", [
    (b"CLAUDE_CODE_OAUTH_TOKEN=synthetic-token\n", b"synthetic-token"),
    (b"CLAUDE_CODE_OAUTH_TOKEN='synthetic-token'\n", b"synthetic-token"),
    (b'CLAUDE_CODE_OAUTH_TOKEN="synthetic-token"\n', b"synthetic-token"),
])
def test_assignment(raw, expected):
    assert module.parse_assignment(raw) == expected


@pytest.mark.parametrize("raw", [
    b"", b"OTHER=synthetic-token\n", b"CLAUDE_CODE_OAUTH_TOKEN=\n",
    b"CLAUDE_CODE_OAUTH_TOKEN=one\nOTHER=two\n",
    b"CLAUDE_CODE_OAUTH_TOKEN=one\nCLAUDE_CODE_OAUTH_TOKEN=two\n",
    b"CLAUDE_CODE_OAUTH_TOKEN='unterminated\n",
    b"CLAUDE_CODE_OAUTH_TOKEN=one two\n",
])
def test_reject_malformed(raw):
    with pytest.raises(ValueError):
        module.parse_assignment(raw)


def test_update_preserves_existing_keys(monkeypatch, tmp_path):
    import json
    original = {"metadata": {"resourceVersion": "7"}, "type": "Opaque",
                "data": {"auth.json": "synthetic-codex", "EXTRA": "synthetic-extra"}}
    captured = []

    def fake_run(args, input_data=None):
        if "replace" in args:
            captured.append(json.loads(input_data))
            return b""
        if captured:
            return json.dumps(captured[0]).encode()
        return json.dumps(original).encode()

    monkeypatch.setattr(module, "run", fake_run)
    private = tmp_path / "token.env"
    private.write_bytes(b"CLAUDE_CODE_OAUTH_TOKEN=synthetic-token\n")
    private.chmod(0o600)
    digest, keys = module.update(private, tmp_path / "backup")
    assert len(digest) == 64
    assert keys == ["CLAUDE_CODE_OAUTH_TOKEN", "EXTRA", "auth.json"]
    assert captured[0]["data"]["auth.json"] == original["data"]["auth.json"]
    assert captured[0]["data"]["EXTRA"] == original["data"]["EXTRA"]
    assert (tmp_path / "backup" / "liveprobe-llm.json").stat().st_mode & 0o777 == 0o600
