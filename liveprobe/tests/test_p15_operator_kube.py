"""Official Kubernetes DynamicClient exercised over a loopback HTTP API, never a cluster."""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from kubernetes import client

from test_p15_commands import cm, operation


@pytest.fixture
def http_api():
    state = SimpleNamespace(calls=[], obj=cm(), status=200, response=None, delete=None, enforce_delete_options=False)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def handle_request(self):
            path = urlsplit(self.path)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or "null")
            state.calls.append({"method": self.command, "path": path.path, "query": parse_qs(path.query), "body": body,
                                "content_type": self.headers.get("Content-Type"), "auth": self.headers.get("Authorization")})
            status, response = state.status, state.response
            if state.delete is not None:
                status, response = (state.delete if self.command == "DELETE" else (200, None))
            if response is None and path.path == "/api/v1":
                response = {"kind": "APIResourceList", "groupVersion": "v1", "resources": [
                    {"name": "configmaps", "kind": "ConfigMap", "namespaced": True, "verbs": ["get", "create", "patch", "delete"]},
                    {"name": "secrets", "kind": "Secret", "namespaced": True, "verbs": ["get"]},
                    {"name": "pods/exec", "kind": "PodExecOptions", "namespaced": True, "verbs": ["create"]},
                    {"name": "nodes", "kind": "Node", "namespaced": False, "verbs": ["get", "patch"]}]}
            elif response is None and self.command == "GET":
                response = copy.deepcopy(state.obj)
                status = 200 if response else 404
            elif response is None:
                response = copy.deepcopy(state.obj or cm())
            if state.enforce_delete_options and self.command == "DELETE" and status == 200:
                # Kubernetes decodes a non-empty DeleteOptions body instead of merging query options.
                # A query-only dryRun is NOT a deletion dry-run when that body is present.
                if (body or {}).get("dryRun") != ["All"]:
                    state.obj = None
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = handle_request
        do_POST = handle_request
        do_PATCH = handle_request
        do_DELETE = handle_request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = client.Configuration()
    cfg.host = f"http://127.0.0.1:{server.server_port}"
    cfg.api_key = {"authorization": "Bearer synthetic-operator-token"}
    cfg.debug = False
    cfg.retries = 0
    api = client.ApiClient(cfg)
    state.api = api
    try:
        yield state
    finally:
        api.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_dynamic_client_read_dry_run_patch_uses_atomic_uid_and_rv_tests(http_api):
    from app.operator_kube import OperatorKube
    h = http_api
    k = OperatorKube(h.api)
    before = k.get(operation())
    after = copy.deepcopy(before)
    after["data"]["mode"] = "new"
    out = k.mutate(operation(), before, after, dry_run=True)
    assert out["kind"] == "ConfigMap"
    call = h.calls[-1]
    assert call["method"] == "PATCH" and call["query"]["dryRun"] == ["All"]
    assert call["query"]["fieldValidation"] == ["Strict"]
    assert call["content_type"] == "application/json-patch+json"
    assert call["auth"] == "Bearer synthetic-operator-token"
    assert call["body"][:2] == [{"op": "test", "path": "/metadata/uid", "value": "uid-1"},
                                  {"op": "test", "path": "/metadata/resourceVersion", "value": "1"}]
    assert {"op": "replace", "path": "/data/mode", "value": "new"} in call["body"]
    assert all(c["path"] != "/api/v1/secrets" for c in h.calls)


def test_create_and_delete_http_contract(http_api):
    from app.operator_kube import OperatorKube
    k = OperatorKube(http_api.api)
    manifest = cm()
    manifest["metadata"].pop("uid")
    manifest["metadata"].pop("resourceVersion")
    k.mutate(operation("apply", manifest=manifest), None, manifest, dry_run=True)
    assert http_api.calls[-1]["method"] == "POST"
    assert http_api.calls[-1]["path"] == "/api/v1/namespaces/sandbox/configmaps"
    k.mutate(operation("delete"), cm(), None, dry_run=True)
    call = http_api.calls[-1]
    assert call["method"] == "DELETE"
    assert call["body"]["preconditions"] == {"uid": "uid-1", "resourceVersion": "1"}
    assert call["body"]["propagationPolicy"] == "Orphan"
    assert call["query"]["dryRun"] == ["All"]


def test_secret_and_subresource_reads_are_denied_before_http(http_api):
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    k = OperatorKube(http_api.api)
    for kind in ("Secret", "secret", "Pod/exec", "Node/proxy", "ServiceAccount/token"):
        with pytest.raises(CommandError):
            k.get(operation(kind=kind))
    assert http_api.calls == []


def test_http_failure_exposes_only_status_not_response_body(http_api):
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    k = OperatorKube(http_api.api)
    k.get(operation())
    http_api.status, http_api.response = 403, {"message": "HTTP_BODY_SENTINEL", "reason": "Forbidden"}
    with pytest.raises(CommandError) as e:
        k.get(operation())
    assert "HTTP_BODY_SENTINEL" not in str(e.value)
    assert "403" in str(e.value)


def test_adapter_rejects_effective_namespace_delete_even_when_declared_as_apply(http_api):
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "sandbox", "uid": "u", "resourceVersion": "1"}}
    op = {"op": "apply", "apiVersion": "v1", "kind": "Namespace", "namespace": None, "name": "sandbox", "manifest": {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "sandbox"}}}
    with pytest.raises(CommandError):
        OperatorKube(http_api.api).mutate(op, ns, None)
    assert http_api.calls == []


def test_adapter_rejects_object_identity_mismatch_before_http(http_api):
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    after = cm("other")
    with pytest.raises(CommandError):
        OperatorKube(http_api.api).mutate(operation(), cm(), after)
    assert http_api.calls == []


def test_bound_client_rotation_uses_only_explicit_token_file(tmp_path):
    from app.operator_kube import OperatorKube
    token = tmp_path / "operator-synthetic-token"
    token.write_text("synthetic-one")
    k = OperatorKube.from_token_file(str(token), host="https://localhost:6443")
    try:
        def auth():
            headers = {}
            k.api_client.update_params_for_auth(headers, [], ["BearerToken"])
            return headers["authorization"]
        assert auth() == "Bearer synthetic-one"
        token.write_text("synthetic-two")
        assert auth() == "Bearer synthetic-two"
        assert k.api_client.configuration.debug is False
    finally:
        k.close()


def test_delete_conflict_is_rejected_even_when_same_content_is_terminating(http_api, monkeypatch):
    """같은 UID와 내용이어도 다른 행위자가 삭제했을 수 있으므로 409를 성공으로 바꾸지 않는다."""
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    h = http_api
    k = OperatorKube(h.api)
    terminating = cm()
    terminating["metadata"].update(deletionTimestamp="2026-10-19T00:00:00Z", finalizers=["orphan"], resourceVersion="2")
    monkeypatch.setattr(k, "get", lambda op: terminating)
    monkeypatch.setattr(k, "_await_gone", lambda op, uid: None)
    h.delete = (409, {"kind": "Status", "code": 409})
    with pytest.raises(CommandError, match="kubernetes_error:409"):
        k.mutate(operation("delete"), cm(), None)


def test_delete_conflict_without_matching_termination_still_fails(http_api, monkeypatch):
    import app.operator_kube as ok
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    monkeypatch.setattr(ok, "DELETE_WAIT_S", 0.3)
    monkeypatch.setattr(ok, "DELETE_POLL_S", 0.05)
    h = http_api
    k = OperatorKube(h.api)
    other = cm()
    other["metadata"]["uid"] = "uid-other"
    monkeypatch.setattr(k, "get", lambda op: other)
    h.delete = (409, {"kind": "Status", "code": 409})
    with pytest.raises(CommandError, match="kubernetes_error:409"):
        k.mutate(operation("delete"), cm(), None)
    live = cm()
    monkeypatch.setattr(k, "get", lambda op: live)
    with pytest.raises(CommandError, match="kubernetes_error:409"):
        k.mutate(operation("delete"), cm(), None)


def test_accepted_delete_that_never_disappears_is_not_reported_done(http_api, monkeypatch):
    import app.operator_kube as ok
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    monkeypatch.setattr(ok, "DELETE_WAIT_S", 0.3)
    monkeypatch.setattr(ok, "DELETE_POLL_S", 0.05)
    h = http_api
    k = OperatorKube(h.api)
    stuck = cm()
    stuck["metadata"].update(deletionTimestamp="2026-10-19T00:00:00Z", finalizers=["example.com/hold"])
    monkeypatch.setattr(k, "get", lambda op: stuck)
    h.delete = (200, {"kind": "Status", "status": "Success"})
    with pytest.raises(CommandError, match="delete_not_completed"):
        k.mutate(operation("delete"), cm(), None)


def test_conflicting_delete_must_not_accept_concurrent_content_change(http_api, monkeypatch):
    """독립 검토 재현: 승인 이후 다른 행위자가 data를 바꾸고 삭제를 시작했다면 409를 우리 성공으로 삼키면 안 된다."""
    import app.operator_kube as ok
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    monkeypatch.setattr(ok, "DELETE_WAIT_S", 0.3)
    monkeypatch.setattr(ok, "DELETE_POLL_S", 0.05)
    h = http_api
    k = OperatorKube(h.api)
    concurrent = cm()
    concurrent["data"]["mode"] = "concurrent-unapproved"
    concurrent["metadata"].update(resourceVersion="3", deletionTimestamp="2026-10-21T00:00:00Z", finalizers=["orphan"])
    observed = iter([concurrent, None])
    monkeypatch.setattr(k, "get", lambda op: next(observed))
    h.delete = (409, {"kind": "Status", "code": 409, "reason": "Conflict"})
    with pytest.raises(CommandError, match="kubernetes_error:409"):
        k.mutate(operation("delete"), cm(), None)


def test_conflicting_delete_with_extra_finalizer_or_label_is_rejected(http_api, monkeypatch):
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    h = http_api
    k = OperatorKube(h.api)
    h.delete = (409, {"kind": "Status", "code": 409})
    for mutate in (lambda m: m.update(finalizers=["orphan", "example.com/other"]),
                   lambda m: m.setdefault("labels", {}).update(changed="yes")):
        terminating = cm()
        terminating["metadata"].update(resourceVersion="2", deletionTimestamp="2026-10-21T00:00:00Z", finalizers=["orphan"])
        mutate(terminating["metadata"])
        monkeypatch.setattr(k, "get", lambda op, obj=terminating: obj)
        with pytest.raises(CommandError, match="kubernetes_error:409"):
            k.mutate(operation("delete"), cm(), None)


def test_conflicting_delete_whose_object_is_already_gone_is_unverified(http_api, monkeypatch):
    """409 뒤 이미 사라졌다면 우리 삭제인지 다른 행위자의 삭제인지 구별할 수 없으므로 완료로 기록하지 않는다."""
    from app.commands import CommandError
    from app.operator_kube import OperatorKube
    h = http_api
    k = OperatorKube(h.api)
    monkeypatch.setattr(k, "get", lambda op: None)
    h.delete = (409, {"kind": "Status", "code": 409})
    with pytest.raises(CommandError, match="kubernetes_error:409"):
        k.mutate(operation("delete"), cm(), None)


@pytest.mark.parametrize("declared_op", ["delete", "apply"])
def test_delete_dry_run_preserves_target_including_creation_rollback(http_api, declared_op):
    """삭제 예행연습과 생성 원복 예행연습은 본문에 dryRun을 넣어 UID·RV·내용을 그대로 보존한다."""
    from app.commands import content
    from app.operator_kube import OperatorKube
    h = http_api
    h.enforce_delete_options = True
    k = OperatorKube(h.api)
    before = copy.deepcopy(h.obj)
    op = operation(declared_op, **({"manifest": content(before)} if declared_op == "apply" else {}))
    assert k.mutate(op, before, None, dry_run=True) is None
    assert k.get(op) == before
    call = next(c for c in reversed(h.calls) if c["method"] == "DELETE")
    assert call["body"].get("dryRun") == ["All"]
    assert call["body"]["preconditions"] == {"uid": "uid-1", "resourceVersion": "1"}
    assert k.mutate(op, before, None) is None
    assert k.get(op) is None
    real = next(c for c in reversed(h.calls) if c["method"] == "DELETE")
    assert "dryRun" not in real["body"]
