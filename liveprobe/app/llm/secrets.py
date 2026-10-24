"""Credential resolution for LLM providers.

Credentials come only from the approved Secret mechanism: a Kubernetes Secret mounted as a
read-only file (preferred; kubelet refreshes the file on rotation, so a renewed token is picked
up on the next call without a Pod restart) or an env var injected from the same Secret.
Values are never logged, returned in errors or persisted.
"""
from __future__ import annotations

from pathlib import Path


def resolve_secret(value: str | None, file: str | None) -> str | None:
    """Return the credential, reading `file` fresh on every call when it exists and is non-empty."""
    if file:
        try:
            v = Path(file).read_text().strip()
        except OSError:
            v = ""
        if v:
            return v
    return value or None
