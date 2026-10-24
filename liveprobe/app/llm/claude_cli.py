"""Claude Code CLI provider (Claude subscription OAuth token, non-interactive `claude -p`).

Auth lifecycle: the operator runs `claude setup-token` on a workstation (interactive, human-only;
never automated here) and stores the resulting long-lived OAuth token in the Kubernetes Secret
`liveprobe-llm` key `CLAUDE_CODE_OAUTH_TOKEN`. The Secret is mounted read-only and the file is
re-read on every call, so a rotated/renewed token takes effect without a Pod restart. Each call
runs with a private throw-away HOME/CLAUDE_CONFIG_DIR so no developer ~/.claude state, keychain,
settings, hooks, MCP servers or session history is reused, and ANTHROPIC_* API keys are removed
from the child environment so this provider can never silently fall back to API billing.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError as SchemaValidationError

from ..redact import redact
from .base import LLMError, LLMResult, Usage, parse_json_object, strict_schema
from .secrets import resolve_secret

_AUTH_HINTS = ("not logged in", "/login", "failed to authenticate", "invalid bearer", "oauth token",
               "token has expired", "token expired", "unauthorized", "invalid api key", "authentication")
_AUTH_STATUS = (401, 403)

SYSTEM_PROMPT = (
    "You are a component of an automated Kubernetes incident-analysis system. "
    "You have no tools. Answer only from the data in the user message and reply with a single "
    "JSON object that matches the provided output schema."
)


class ClaudeCLIProvider:
    name = "claude_cli"

    def __init__(self, claude_bin: str = "claude", oauth_token: str | None = None,
                 oauth_token_file: str | None = None):
        self.claude_bin = claude_bin
        self._token = oauth_token
        self._token_file = oauth_token_file

    def _cmd(self, model: str, effort: str | None, schema: dict[str, Any]) -> list[str]:
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--model", model,
               "--json-schema", json.dumps(strict_schema(schema)),
               "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def _env(self, home: str, token: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC_", "CLAUDE_", "OPENAI_"))}
        env.update({
            "HOME": home,
            "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_COLOR": "1",
        })
        return env

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str,
                 effort: str | None, timeout: int) -> LLMResult:
        token = resolve_secret(self._token, self._token_file)
        if not token:
            raise LLMError("config", "CLAUDE_CODE_OAUTH_TOKEN is not configured")
        try:
            validator = Draft202012Validator(strict_schema(schema))
            validator.check_schema(validator.schema)
        except (SchemaError, TypeError, ValueError):
            raise LLMError("config", "invalid output schema") from None
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="lp-claude-") as home:
            try:
                proc = subprocess.Popen(
                    self._cmd(model, effort, schema), cwd=home, env=self._env(home, token),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
            except FileNotFoundError as e:
                raise LLMError("config", f"claude binary not found: {self.claude_bin}") from e
            try:
                stdout, stderr = proc.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise LLMError("timeout", f"claude -p exceeded {timeout}s",
                               duration_ms=int((time.monotonic() - t0) * 1000))
        duration_ms = int((time.monotonic() - t0) * 1000)
        return self._parse(stdout, stderr, proc.returncode, duration_ms, model, effort, token, validator)

    def _parse(self, stdout: str, stderr: str, rc: int, duration_ms: int, model: str,
               effort: str | None, token: str, validator: Draft202012Validator) -> LLMResult:
        def clean(s: str) -> str:
            if token:
                s = s.replace(token, "[REDACTED]")
            return (redact(s) or "")[:800]

        ev: dict[str, Any] | None = None
        s = (stdout or "").strip()
        if s:
            # --output-format json prints one result object; tolerate stray leading lines.
            for cand in (s, s.splitlines()[-1]):
                try:
                    obj = json.loads(cand)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    ev = obj
                    break
        usage = Usage()
        if ev is not None:
            u = ev.get("usage") or {}
            base_in = u.get("input_tokens")
            cc, cr = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
            usage = Usage(
                input_tokens=None if base_in is None else base_in + cc + cr,
                output_tokens=u.get("output_tokens"),
                cached_input_tokens=cr if base_in is not None else None,
            )
        if ev is None or rc != 0 or ev.get("is_error") or ev.get("type") != "result":
            detail = str((ev or {}).get("result") or "") or (stderr or "")[-600:] or f"exit code {rc}"
            status = (ev or {}).get("api_error_status")
            low = detail.lower()
            if status in _AUTH_STATUS or any(h in low for h in _AUTH_HINTS):
                kind = "auth"
            elif "timeout" in low or "timed out" in low:
                kind = "timeout"
            else:
                kind = "provider"
            prefix = f"api_error_status={status}: " if status else ""
            raise LLMError(kind, clean(prefix + detail), usage=usage, duration_ms=duration_ms)
        data = ev.get("structured_output")
        text = ev.get("result") or ""
        if isinstance(data, dict):
            text = text or json.dumps(data)
        else:
            try:
                data = parse_json_object(text)
            except LLMError as e:
                e.usage, e.duration_ms = usage, duration_ms
                raise
        # Neither the raw result nor nested output fields may carry the OAuth token
        # into a caller, persistence layer, API response, or log.
        def scrub(value: Any) -> Any:
            if isinstance(value, str):
                return redact(value.replace(token, "[REDACTED]"))
            if isinstance(value, dict):
                return {scrub(k): scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value

        data = scrub(data)
        text = scrub(text)
        try:
            validator.validate(data)
        except SchemaValidationError:
            raise LLMError("invalid_output", "schema validation failed", usage=usage,
                           duration_ms=duration_ms) from None
        return LLMResult(text=text, data=data, usage=usage, duration_ms=duration_ms,
                         provider=self.name, model=model, effort=effort)
