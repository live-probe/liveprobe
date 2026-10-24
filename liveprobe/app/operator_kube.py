"""Bounded official dynamic Kubernetes adapter, using a separately projected identity.

No shell, kubeconfig fallback, object listing, subresources, or on-disk discovery cache.
Only exact named non-Secret resources cross this boundary. Admission/RBAC remain required.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from kubernetes import client
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.resource import Resource

from .commands import CommandError, changes, content, identity, validate_operation

TIMEOUT = (3, 5)
# Real deletes finish asynchronously (graceful Pods, orphan finalizer). Completion is observed, never assumed.
DELETE_WAIT_S = 60.0
DELETE_POLL_S = 1.0


def bound_api_client(token_file: str, ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt", *, host: str | None = None):
    """Dedicated Configuration (never mutate global/read-only credentials); rotate token per call."""
    token_path = Path(token_file)
    if not token_path.is_file():
        raise CommandError("operator_token_unavailable", 503)
    cfg = client.Configuration()
    cfg.host = host or "https://%s:%s" % (os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"), os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
    if not cfg.host.startswith("https://"):
        raise CommandError("operator_https_required", 503)
    cfg.ssl_ca_cert, cfg.verify_ssl, cfg.debug = ca_file, True, False
    cfg.retries = 0
    def refresh(configuration):
        try:
            token = token_path.read_text().strip()
        except OSError:
            raise CommandError("operator_token_unavailable", 503) from None
        if not token:
            raise CommandError("operator_token_unavailable", 503)
        configuration.api_key["authorization"] = "Bearer " + token
    cfg.api_key = {"authorization": ""}
    cfg.refresh_api_key_hook = refresh
    return client.ApiClient(cfg)


class _MemoryDiscovery:
    """Query a single requested API group/version; retain only its safe resource descriptors."""
    def __init__(self, dynamic, _cache_file):
        self.dynamic, self.cache = dynamic, {}

    def get(self, *, api_version, kind):
        if api_version not in self.cache:
            prefix = "/apis/" if "/" in api_version else "/api/"
            result = self.dynamic.request("get", prefix + api_version, _request_timeout=TIMEOUT).to_dict()
            if result.get("groupVersion") != api_version:
                raise CommandError("discovery_version_mismatch", 503)
            self.cache[api_version] = [r for r in result.get("resources", [])
                                       if "/" not in r.get("name", "") and r.get("kind", "").lower() != "secret"]
        matches = [r for r in self.cache[api_version] if r.get("kind") == kind]
        if len(matches) != 1:
            raise CommandError("resource_discovery_failed", 422)
        descriptor = matches[0]
        group, version = api_version.split("/", 1) if "/" in api_version else ("", api_version)
        return Resource(prefix="apis" if group else "api", group=group, api_version=version,
                        kind=kind, namespaced=descriptor.get("namespaced", False), name=descriptor["name"],
                        verbs=descriptor.get("verbs", []), client=self.dynamic)


class OperatorKube:
    def __init__(self, api_client):
        if api_client.configuration.debug:
            raise CommandError("debug_client_forbidden", 503)
        self.api_client = api_client
        self.dynamic = DynamicClient(api_client, discoverer=_MemoryDiscovery)

    @classmethod
    def from_token_file(cls, token_file, ca_file="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt", *, host=None):
        return cls(bound_api_client(token_file, ca_file, host=host))

    def close(self):
        self.api_client.close()

    def _resource(self, op):
        validate_operation(op, defer_workload_delta=True)
        if op["kind"].lower() == "secret" or op["op"] == "secret_update":
            raise CommandError("secret_write_only", 403)
        resource = self.dynamic.resources.get(api_version=op["apiVersion"], kind=op["kind"])
        if resource.kind != op["kind"] or resource.group_version != op["apiVersion"] or "/" in resource.name:
            raise CommandError("resource_identity_mismatch", 403)
        if bool(op.get("namespace")) != bool(resource.namespaced):
            raise CommandError("resource_namespace_mismatch", 422)
        return resource

    @staticmethod
    def _error(exc):
        if isinstance(exc, CommandError):
            return exc
        status = getattr(exc, "status", None)
        return CommandError("kubernetes_error:" + str(status if isinstance(status, int) else "transport"), 502)

    def get(self, op):
        try:
            resource = self._resource(op)
            return self.dynamic.get(resource, name=op["name"], namespace=op.get("namespace"), _request_timeout=TIMEOUT).to_dict()
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise self._error(exc) from None

    def _await_gone(self, op, uid):
        deadline = time.monotonic() + DELETE_WAIT_S
        while True:
            now = self.get(op)
            if now is None or ((now.get("metadata") or {}).get("uid") != uid):
                return
            if time.monotonic() >= deadline:
                raise CommandError("delete_not_completed", 504)
            time.sleep(DELETE_POLL_S)

    def mutate(self, op, before, after, *, dry_run=False):
        try:
            # Check the actual effect, not just the operation label (rollback uses original labels).
            validate_operation(op, defer_workload_delta=True)
            if after is None:
                validate_operation({k: op[k] for k in ("apiVersion", "kind", "namespace", "name") if k in op} | {"op": "delete"})
            for obj in (before, after):
                if obj is not None:
                    md = obj.get("metadata") or {}
                    if (obj.get("apiVersion"), obj.get("kind"), md.get("namespace"), md.get("name")) != (op["apiVersion"], op["kind"], op.get("namespace"), op["name"]):
                        raise CommandError("object_target_mismatch", 403)
            resource = self._resource(op)
            kwargs = {"namespace": op.get("namespace"), "_request_timeout": TIMEOUT,
                      "query_params": [("fieldValidation", "Strict")], "field_manager": "liveprobe-operator"}
            if dry_run:
                kwargs["dry_run"] = "All"
            if before is None:
                if after is None:
                    raise CommandError("empty_mutation", 422)
                # POST is an atomic absence precondition: an existing object yields 409.
                return self.dynamic.create(resource, body=content(after), **kwargs).to_dict()
            snap = identity(before)
            assert snap is not None  # before is non-null after the creation branch.
            if after is None:
                options = {"apiVersion": "v1", "kind": "DeleteOptions",
                           "preconditions": {"uid": snap["uid"], "resourceVersion": snap["resourceVersion"]},
                           "propagationPolicy": "Orphan"}
                if dry_run:
                    # 본문이 있으면 Kubernetes는 URL의 삭제 옵션을 병합하지 않는다.
                    # URL에만 dryRun을 넣으면 예행연습이 실제 삭제가 된다.
                    options["dryRun"] = ["All"]
                # 409는 항상 실패: 이후 소멸 여부로 다른 행위자의 삭제를 우리 성공으로 오인하지 않는다.
                self.dynamic.delete(resource, name=op["name"], body=options, **kwargs)
                if not dry_run:
                    self._await_gone(op, snap["uid"])
                return None
            patch = [{"op": "test", "path": "/metadata/uid", "value": snap["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion", "value": snap["resourceVersion"]}]
            for d in changes(content(before), content(after)):
                patch.append({"op": "remove", "path": d["path"]} if not d["after_exists"] else
                             {"op": "replace" if d["before_exists"] else "add", "path": d["path"], "value": d["after"]})
            return self.dynamic.patch(resource, name=op["name"], body=patch,
                                      content_type="application/json-patch+json", **kwargs).to_dict()
        except Exception as exc:
            raise self._error(exc) from None
