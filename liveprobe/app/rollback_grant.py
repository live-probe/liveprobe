"""Short-lived, target/revision-bound authorization minted only by the action executor."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

KEY_PATH = "/var/run/secrets/rollback-grant/key"
TTL_SECONDS = 20


class InvalidGrant(Exception):
    pass


def load_key(path: str = KEY_PATH) -> bytes:
    key = Path(path).read_bytes()
    if len(key) < 32:
        raise InvalidGrant("rollback grant key is missing or too short")
    return key


def _payload(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue(key: bytes, *, incident_id: int, action_id: int, name: str,
          expected_revision: int, expected_source: str, now: int | None = None, namespace: str = "liveprobe-target") -> dict:
    now = int(time.time()) if now is None else now
    fields = {"incident_id": incident_id, "action_id": action_id,
              "namespace": namespace, "name": name, "expected_revision": expected_revision,
              "expected_source": expected_source, "expires_at": now + TTL_SECONDS,
              "nonce": secrets.token_hex(16)}
    fields["grant"] = hmac.new(key, _payload(fields), hashlib.sha256).hexdigest()
    return fields


def verify(key: bytes, fields: dict, *, now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    if (not isinstance(fields, dict) or set(fields) != {"incident_id", "action_id", "namespace", "name",
            "expected_revision", "expected_source", "expires_at", "nonce", "grant"} or
            type(fields["incident_id"]) is not int or fields["incident_id"] <= 0 or
            type(fields["action_id"]) is not int or fields["action_id"] <= 0 or
            not isinstance(fields["namespace"], str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", fields["namespace"]) or
            not isinstance(fields["name"], str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", fields["name"]) or
            type(fields["expected_revision"]) is not int or fields["expected_revision"] <= 0 or
            not isinstance(fields["expected_source"], str) or
            not fields["expected_source"].startswith(fields["name"] + "-") or
            len(fields["expected_source"]) > 253 or
            type(fields["expires_at"]) is not int or
            not now < fields["expires_at"] <= now + TTL_SECONDS or
            not isinstance(fields["nonce"], str) or len(fields["nonce"]) != 32 or
            not isinstance(fields["grant"], str) or len(fields["grant"]) != 64):
        raise InvalidGrant("invalid rollback grant")
    signature = fields["grant"]
    expected = hmac.new(key, _payload({k: v for k, v in fields.items() if k != "grant"}),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidGrant("invalid rollback grant")
