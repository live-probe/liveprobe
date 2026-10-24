"""Local-only access guard (no user authentication) and the Alertmanager webhook token check.

The dashboard is a local PoC: there are no user accounts, no login and no session cookie. Because a
browser on the same machine can be driven by any web page, every API request is checked instead for

* a Host header in the allow list (blocks DNS rebinding), and
* for writes, a same-origin Origin / Fetch-Metadata value plus the dashboard's custom header
  (a cross-site form or fetch cannot set it without a CORS preflight, which is never granted).

This is not an authorization system. Kubernetes RBAC, the safe-action allowlist and the webhook
bearer token (service-to-service) are unchanged.
"""
from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings

ACTOR = "local-operator"
CLIENT_HEADER = "x-liveprobe-client"
CLIENT_VALUE = "dashboard"


def _host_only(value: str) -> str:
    return (value or "").strip().lower().rsplit(":", 1)[0] if value.count(":") == 1 else (value or "").strip().lower()


def host_allowed(settings: Settings, host_header: str | None) -> bool:
    allowed = {h.strip().lower() for h in settings.allowed_hosts if h.strip()}
    host = (host_header or "").strip().lower()
    return bool(host) and (host in allowed or _host_only(host) in allowed)


def require_user(request: Request, settings: Settings = Depends(get_settings)) -> str:
    """Local access guard. Returns the fixed actor name used in audit rows."""
    if not host_allowed(settings, request.headers.get("host")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "host not allowed")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin mutation denied")
        origin = request.headers.get("origin")
        if origin and not host_allowed(settings, urlsplit(origin).netloc):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin mutation denied")
        if request.headers.get(CLIENT_HEADER) != CLIENT_VALUE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "client header required")
    return ACTOR


def require_webhook(request: Request, settings: Settings = Depends(get_settings)) -> None:
    auth = request.headers.get("authorization", "")
    expected = settings.webhook_token
    if not expected or not auth.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")
    if not hmac.compare_digest(auth[7:].strip().encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
