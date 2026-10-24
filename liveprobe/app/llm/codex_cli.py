"""Codex CLI provider (ChatGPT OAuth credentials in CODEX_HOME/auth.json)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .base import LLMError, LLMResult, Usage, parse_json_object, strict_schema, validation_schema

_AUTH_HINTS = ("401", "403", "unauthorized", "not logged in", "login required",
               "token expired", "expired token", "refresh token", "authentication failed",
               "invalid credentials")

SYSTEM_PREAMBLE = (
    "You are a component of an automated Kubernetes incident-analysis system. "
    "Do NOT run any shell commands, do NOT read files, and do NOT call tools: "
    "answer only from the data in this prompt. Reply with a single JSON object that "
    "matches the provided output schema.\n\n"
)


class CodexCLIProvider:
    name = "codex_cli"

    def __init__(self, codex_bin: str = "codex", codex_home: str | None = None):
        self.codex_bin = codex_bin
        self.codex_home = codex_home

    def _cmd(self, model: str, effort: str | None, schema_path: str, workdir: str) -> list[str]:
        cmd = [self.codex_bin, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
               "--ignore-user-config", "--ignore-rules",
               "-C", workdir, "-m", model, "--output-schema", schema_path, "--json", "--color", "never"]
        # P15: read-only is NOT a no-read sandbox. These are prompt-only components, so remove the actual
        # tool channels as well as requesting no tools in the preamble. Flags were checked against the
        # deployed Codex feature catalog; the app must not inherit user MCP/plugins/hooks from CODEX_HOME.
        for feature in ("shell_tool", "unified_exec", "shell_snapshot", "view_image", "apps", "plugins", "remote_plugin",
                        "hooks", "multi_agent", "multi_agent_v2", "browser_use", "browser_use_external",
                        "browser_use_full_cdp_access", "computer_use", "code_mode_host", "image_generation",
                        "sleep_tool", "skill_search", "skill_mcp_dependency_install", "workspace_dependencies"):
            cmd += ["--disable", feature]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd.append("-")
        return cmd

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str,
                 effort: str | None, timeout: int) -> LLMResult:
        # Do not pass API credentials or unrelated Secret environment values to the child.
        # CODEX_HOME is the approved, read-only OAuth Secret mount; never use API-key fallback.
        env = {k: v for k, v in os.environ.items() if k in (
            "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        )}
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        env["NO_COLOR"] = "1"
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="lp-codex-") as td:
            env["HOME"] = td  # no host dotfiles or other CLI credential stores
            schema_path = os.path.join(td, "schema.json")
            with open(schema_path, "w") as f:
                json.dump(strict_schema(schema), f)
            try:
                proc = subprocess.Popen(
                    self._cmd(model, effort, schema_path, td), cwd=td, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
            except FileNotFoundError as e:
                raise LLMError("config", f"codex binary not found: {self.codex_bin}") from e
            try:
                stdout, stderr = proc.communicate(SYSTEM_PREAMBLE + prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise LLMError("timeout", f"codex exec exceeded {timeout}s",
                               duration_ms=int((time.monotonic() - t0) * 1000))
        duration_ms = int((time.monotonic() - t0) * 1000)
        return self._parse(stdout, stderr, proc.returncode, duration_ms, model, effort, schema)

    def _parse(self, stdout: str, stderr: str, rc: int, duration_ms: int, model: str,
               effort: str | None, schema: dict[str, Any]) -> LLMResult:
        usage = Usage()
        final_text: str | None = None
        errors: list[str] = []
        completed = False
        failed = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            item = ev.get("item")
            if t == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                final_text = item.get("text") if isinstance(item.get("text"), str) else None
            elif t == "turn.completed":
                completed = True
                u = ev.get("usage") or {}
                usage = Usage(
                    input_tokens=u.get("input_tokens"),
                    output_tokens=u.get("output_tokens"),
                    cached_input_tokens=u.get("cached_input_tokens"),
                    reasoning_output_tokens=u.get("reasoning_output_tokens"),
                )
            elif t in ("error", "turn.failed"):
                failed = failed or t == "turn.failed"
                error = ev.get("error")
                msg = ev.get("message") or (error.get("message") if isinstance(error, dict) else error)
                errors.append(str(msg or "provider error"))
        if rc != 0 or failed or not completed or final_text is None:
            # CLI diagnostics may contain arbitrary OAuth material (not necessarily a known
            # token shape). Classify from raw output, but never persist/return that output.
            diagnostic = " | ".join(errors) + " " + (stderr or "")
            low = diagnostic.lower()
            from .codex_pool import classify_quota, parse_reset_seconds
            # Usage-limit text is checked first: a limit message can also contain "429"/"token" words.
            if classify_quota(low) and not any(h in low for h in ("unauthorized", "not logged in", "login required", "token expired")):
                err = LLMError("quota", "codex usage limit reached", usage=usage, duration_ms=duration_ms)
                err.reset_seconds = parse_reset_seconds(low)   # a number only, never the raw text
                raise err
            kind = "auth" if any(h in low for h in _AUTH_HINTS) else "provider"
            raise LLMError(kind, "codex authentication failed" if kind == "auth" else
                           "codex execution failed or did not complete",
                           usage=usage, duration_ms=duration_ms)
        try:
            data = parse_json_object(final_text)
        except LLMError as e:
            e.usage, e.duration_ms = usage, duration_ms
            raise
        try:
            Draft202012Validator(validation_schema(schema)).validate(data)
        except SchemaError:
            raise LLMError("config", "invalid output schema", usage=usage,
                           duration_ms=duration_ms) from None
        except ValidationError:
            # Validation errors include raw model output; never chain their traceback.
            raise LLMError("invalid_output", "schema validation failed", usage=usage,
                           duration_ms=duration_ms) from None
        return LLMResult(text=final_text, data=data, usage=usage, duration_ms=duration_ms,
                         provider=self.name, model=model, effort=effort)
