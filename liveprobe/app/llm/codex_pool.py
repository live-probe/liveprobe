"""Codex account pool: primary account first, spare account when the primary hits its usage limit.

Rules (docs/plans/liveprobe-revamp-plan.md 5.5, interview round 5):
* the primary account serves every call until it reports a usage limit; then the spare takes over;
* when both are limited the pool raises `quota` with the earliest known reset, the caller stores its state and waits;
  it NEVER falls back to an API key or to Claude;
* each account has its own CODEX_HOME (credentials and CLI state are isolated, nothing is shared);
* a call is one `codex exec --ephemeral`, i.e. stateless, so a call that failed on the primary can be repeated on
  the spare without any duplicated side effect (the CLI runs read-only and never touches the cluster).

How a usage-limit failure looks is not documented, so detection is a configurable pattern list plus an explicit
"unclassified" path: an unrecognised failure is NOT treated as a quota failure and is not retried on the spare.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import LLMError, LLMResult

log = logging.getLogger(__name__)

# Lower-case substrings that mean "this account is out of usage right now". Overridable via settings.
DEFAULT_QUOTA_PATTERNS = ("usage limit", "rate limit", "quota", "too many requests", "429",
                          "limit reached", "try again in", "try again at", "exceeded your")
_RESET_RE = re.compile(r"(?:try again (?:in|after)|resets? in|retry after)\s*(\d+)\s*(second|sec|s|minute|min|m|hour|hr|h)", re.I)
_UNIT = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}
DEFAULT_COOLDOWN_S = 30 * 60      # used when the message carries no reset time


def classify_quota(message: str, patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS) -> bool:
    low = (message or "").lower()
    return any(p in low for p in patterns)


def parse_reset_seconds(message: str) -> float | None:
    m = _RESET_RE.search(message or "")
    if not m:
        return None
    return float(m.group(1)) * _UNIT[m.group(2).lower()]


@dataclass
class Account:
    name: str                 # "primary" | "spare"
    provider: Any             # object with .complete(...) -> LLMResult (CodexCLIProvider)
    home: str | None = None
    blocked_until: float = 0.0
    calls: int = 0
    quota_hits: int = 0
    auth_failures: int = 0
    last_error_kind: str | None = None


class CodexPoolProvider:  # noqa: D101
    """Provider that routes each call to the first account that is not rate-limited."""
    name = "codex_cli"

    def __init__(self, accounts: list[Account], *, clock: Callable[[], float] = time.time,
                 quota_patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS,
                 on_event: Callable[[str, dict], None] | None = None,
                 cooldown_s: float = DEFAULT_COOLDOWN_S):
        if not accounts:
            raise LLMError("config", "codex pool needs at least one account")
        self.accounts, self.clock, self.quota_patterns = accounts, clock, quota_patterns
        self.on_event, self.cooldown_s = on_event, cooldown_s
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict]] = []
        self.active: str | None = accounts[0].name

    # ---- observable state (dashboard / tests)
    def status(self) -> dict:
        now = self.clock()
        return {"active": self.active, "accounts": [
            {"name": a.name, "state": "limited" if a.blocked_until > now else ("auth_failed" if a.last_error_kind == "auth" else "available"),
             "blocked_for_s": max(0, round(a.blocked_until - now)) if a.blocked_until > now else 0,
             "calls": a.calls, "quota_hits": a.quota_hits, "last_error_kind": a.last_error_kind} for a in self.accounts]}

    def drain_events(self) -> list[tuple[str, dict]]:
        with self._lock:
            out, self._events = self._events, []
        return out

    def _emit(self, kind: str, detail: dict) -> None:
        with self._lock:
            self._events.append((kind, detail))
            del self._events[:-50]
        if self.on_event:
            try:
                self.on_event(kind, detail)
            except Exception:  # noqa: BLE001 - observers must never break a call
                log.warning("pool event handler failed")

    def next_available_in(self) -> float:
        now = self.clock()
        return max(0.0, min(a.blocked_until for a in self.accounts) - now)

    def complete(self, role: str, prompt: str, schema: dict[str, Any], model: str, effort: str | None, timeout: int) -> LLMResult:
        tried: list[str] = []
        last_quota: LLMError | None = None
        for acct in self.accounts:
            with self._lock:
                if acct.blocked_until > self.clock():
                    continue
            tried.append(acct.name)
            try:
                res = acct.provider.complete(role, prompt, schema, model, effort, timeout)
            except LLMError as e:
                acct.last_error_kind = e.kind
                if self._is_quota(e):
                    wait = e.reset_seconds or self.cooldown_s
                    with self._lock:
                        acct.blocked_until, acct.quota_hits = self.clock() + wait, acct.quota_hits + 1
                    last_quota = e
                    self._emit("account_limited", {"account": acct.name, "blocked_for_s": round(wait)})
                    continue
                if e.kind == "auth":
                    acct.auth_failures += 1
                    self._emit("account_auth_failed", {"account": acct.name})
                    last_quota = None
                    # an auth failure is not a limit: do not silently burn the spare on a bad primary login
                    if acct is self.accounts[-1] or all(a.last_error_kind == "auth" for a in self.accounts):
                        raise
                    continue
                raise
            acct.calls += 1
            acct.last_error_kind = None
            if self.active != acct.name:
                self._emit("account_switched", {"from": self.active, "to": acct.name})
                self.active = acct.name
            return res
        # nothing could serve the call
        wait = self.next_available_in()
        self._emit("waiting_quota", {"retry_in_s": round(wait), "accounts": [a.name for a in self.accounts]})
        raise LLMError("quota", f"all codex accounts are at their usage limit; next retry in about {round(wait)}s",
                       usage=getattr(last_quota, "usage", None))

    def _is_quota(self, e: LLMError) -> bool:
        # The CLI provider classifies from the raw diagnostic and never exposes it, so only the kind is used here.
        return e.kind == "quota"
