"""Credential redaction applied to anything that is logged, stored, sent to an LLM or returned."""
from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM private keys (multi-line)
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
     '[REDACTED_PRIVATE_KEY]'),
    # JSON-style token fields
    (re.compile(r'("?(?:access_token|refresh_token|id_token|api_key|OPENAI_API_KEY|ANTHROPIC_API_KEY'
                r'|CLAUDE_CODE_OAUTH_TOKEN|x-api-key)"?\s*[:=]\s*)"[^"]*"', re.I),
     r'\1"[REDACTED]"'),
    (re.compile(r'((?:access_token|refresh_token|id_token|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|x-api-key)'
                r'\s*[:=]\s*)[^\s,;&"]+', re.I), r'\1[REDACTED]'),
    # Generic secret-bearing assignments in logs / event messages (password=..., "secret": "...")
    (re.compile(r'("?\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|apikey|token|auth[_-]?token)"?'
                r'\s*[:=]\s*)"[^"]*"', re.I), r'\1"[REDACTED]"'),
    (re.compile(r'(\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|apikey|token|auth[_-]?token)'
                r'\s*[:=]\s*)(?!\[REDACTED)[^\s,;&"\'}\]]+', re.I), r'\1[REDACTED]'),
    # Credentials embedded in URLs / DSNs: scheme://user:pass@host
    (re.compile(r'(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+@', re.I), r'\1[REDACTED]@'),
    # OpenAI / Anthropic keys and Claude OAuth tokens (sk-..., sk-ant-api..., sk-ant-oat...)
    (re.compile(r'sk-[A-Za-z0-9_\-]{8,}'), '[REDACTED_KEY]'),
    # AWS access key ids
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '[REDACTED_KEY]'),
    # JWTs
    (re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}'), '[REDACTED_JWT]'),
    # Bearer / Basic authorization headers
    (re.compile(r'(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}', re.I), r'\1[REDACTED]'),
    (re.compile(r'(Basic\s+)[A-Za-z0-9+/=]{8,}', re.I), r'\1[REDACTED]'),
]

# Never store the literal names of token fields either: API/DB/log searches for these
# strings are used as a leak check, so we normalise them.
_NAMES = re.compile(r'(access|refresh|id)_token', re.I)

# Mapping keys whose values are credentials regardless of their shape (nested payloads,
# Kubernetes object fragments, HTTP headers). Matched on the normalised key.
_SENSITIVE_KEY = re.compile(
    r'^(?:password|passwd|pwd|secret|client_secret|token|auth_token|access_token|refresh_token|id_token'
    r'|api_key|apikey|x_api_key|authorization|proxy_authorization|cookie|set_cookie|private_key'
    r'|openai_api_key|anthropic_api_key|claude_code_oauth_token|credentials?)$'
    r'|(?:_password|_passwd|_secret|_api_key|_private_key)$',
    re.I,
)
_REDACTED = "[REDACTED]"


def redact(text: str | None) -> str | None:
    if text is None:
        return None
    out = text
    for pat, rep in _PATTERNS:
        out = pat.sub(rep, out)
    out = _NAMES.sub(lambda m: f"{m.group(1)}-tok", out)
    return out


def sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key.strip().replace("-", "_")))


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and sensitive_key(k) and v not in (None, "", [], {}):
                out[redact(k)] = _REDACTED
            else:
                out[redact(k) if isinstance(k, str) else k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj
