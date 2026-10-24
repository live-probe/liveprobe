"""Write-only Secret merge patches. Secret values are transient and never returned or logged.

The generated Kubernetes REST client reads error response bodies even when
_preload_content=False. Use its dedicated authenticated pool directly instead: status only,
preload_content=False, no redirects/retries, close and release without reading any body.
Never GET/LIST, deserialize a Secret, or claim value verification/rollback.
"""
from __future__ import annotations

import json
import re

import urllib3

from .commands import CommandError, safe_name, validate_operation
from .operator_kube import TIMEOUT, bound_api_client


def parse_secret_payload(body: bytes) -> tuple[str, dict]:
    """For a raw Request.body() endpoint; never expose JSON/Pydantic input in validation errors.

    Limit request streaming at the HTTP layer as well; this helper rejects bodies over 40 KiB.
    Returned values are ephemeral and must be passed straight to secret_values(), never logged.
    """
    if not isinstance(body, bytes) or len(body) > 40960:
        raise CommandError("invalid_secret_request", 422)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CommandError("invalid_secret_request", 422)
            result[key] = value
        return result
    try:
        obj = json.loads(body, object_pairs_hook=unique)
        if not isinstance(obj, dict) or set(obj) != {"plan_sha256", "values"} or not isinstance(obj["plan_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", obj["plan_sha256"]) or not isinstance(obj["values"], dict):
            raise CommandError("invalid_secret_request", 422)
        validate_values(obj["values"], list(obj["values"]))
    except Exception:
        raise CommandError("invalid_secret_request", 422) from None
    return obj["plan_sha256"], obj["values"]


def validate_values(values, expected_keys):
    if not isinstance(expected_keys, (list, tuple)) or not 1 <= len(expected_keys) <= 16 or any(not isinstance(k, str) or not re.fullmatch(r"[-._A-Za-z0-9]{1,253}", k) for k in expected_keys) or len(set(expected_keys)) != len(expected_keys):
        raise CommandError("invalid_secret_keys", 422)
    if not isinstance(values, dict) or not 1 <= len(values) <= 16 or set(values) != set(expected_keys):
        raise CommandError("secret_key_set_mismatch", 422)
    try:
        valid = all(isinstance(v, str) and len(v.encode("utf-8")) <= 2048 for v in values.values())
    except (UnicodeError, TypeError):
        valid = False
    if not valid:
        raise CommandError("invalid_secret_value", 422)
    return sorted(expected_keys)


class SecretWriter:
    def __init__(self, api_client):
        if api_client.configuration.debug:
            raise CommandError("debug_client_forbidden", 503)
        self.api_client = api_client

    @classmethod
    def from_token_file(cls, token_file, ca_file="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt", *, host=None):
        return cls(bound_api_client(token_file, ca_file, host=host))

    def close(self):
        self.api_client.close()

    def dry_run(self, namespace, name, keys):
        # Empty placeholders are synthetic, never current/new user values. Admission is still exercised.
        return self._patch(namespace, name, {k: "" for k in keys}, keys, dry_run=True, _preload_content=False)

    def write(self, namespace, name, values, *, expected_keys):
        # Re-check admission against the actual transient values, after explicit confirmation.
        self._patch(namespace, name, values, expected_keys, dry_run=True, _preload_content=False)
        return self._patch(namespace, name, values, expected_keys, dry_run=False, _preload_content=False)

    def _patch(self, namespace, name, values, keys, *, dry_run, _preload_content):
        keys = validate_values(values, keys)
        validate_operation({"op": "secret_update", "apiVersion": "v1", "kind": "Secret", "namespace": namespace, "name": name, "keys": keys})
        if _preload_content is not False:
            raise CommandError("secret_response_preload_forbidden", 500)
        response = None
        try:
            headers = {"Accept": "application/json", "Content-Type": "application/merge-patch+json"}
            self.api_client.update_params_for_auth(headers, [], ["BearerToken"])
            # Names were validated before interpolation; no arbitrary URLs or subresources.
            url = self.api_client.configuration.host + f"/api/v1/namespaces/{namespace}/secrets/{name}?fieldValidation=Strict"
            if dry_run:
                url += "&dryRun=All"
            response = self.api_client.rest_client.pool_manager.request(
                "PATCH", url, body=json.dumps({"stringData": values}, ensure_ascii=False).encode("utf-8"),
                headers=headers, preload_content=_preload_content, redirect=False, retries=False,
                timeout=urllib3.Timeout(connect=TIMEOUT[0], read=TIMEOUT[1]))
            status = response.status
            if not isinstance(status, int) or not 200 <= status < 300:
                raise CommandError("secret_write_rejected:" + str(status if isinstance(status, int) else "transport"), 502)
            return {"status": "succeeded", "keys": keys}
        except CommandError:
            raise
        except Exception as exc:
            # Do not stringify an HTTP exception: it may contain echoed stringData or Authorization.
            raise CommandError("secret_transport_error:" + type(exc).__name__, 502) from None
        finally:
            if response is not None:
                # close drops the unread body; release_conn returns only the closed connection to the pool.
                try:
                    try:
                        response.close()
                    finally:
                        response.release_conn()
                except Exception:
                    raise CommandError("secret_response_cleanup_failed", 502) from None
