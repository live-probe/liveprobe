"""KAN-103: dedicated, image-only, previous-revision rollback boundary.

No Secret or arbitrary template access is required. The API may request only a
workload name; a separate ServiceAccount owns the named Deployment patch.
"""
from __future__ import annotations

import copy
import json
import os
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from . import rollback_grant
from .rollback_grant import InvalidGrant

NAMESPACE = "liveprobe-target"
TARGETS = ("inventory", "order")
CONTROLLER_USER = "system:serviceaccount:liveprobe-system:rollback-controller"
CALLER_USER = "system:serviceaccount:liveprobe-system:liveprobe"
AUDIENCE = "liveprobe-rollback"
DIGEST = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*@sha256:[0-9a-f]{64}$")


class Rejected(Exception):
    pass


def trusted_images(env=None):
    env = env if env is not None else os.environ
    images = {name: env.get("ROLLBACK_" + name.upper() + "_IMAGE", "") for name in TARGETS}
    if any(not DIGEST.fullmatch(image) for image in images.values()):
        raise Rejected("operator must configure digest-pinned rollback images for both targets")
    return images


def _template(obj):
    template = copy.deepcopy(obj["spec"]["template"])
    metadata = template.get("metadata") or {}
    (metadata.get("labels") or {}).pop("pod-template-hash", None)
    metadata.pop("creationTimestamp", None)
    return template


def _revision(obj):
    try:
        return int(obj["metadata"]["annotations"]["deployment.kubernetes.io/revision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Rejected("missing or invalid revision provenance") from exc


def _image(template, name):
    containers = template["spec"]["containers"]
    if len(containers) != 1 or containers[0]["name"] != name:
        raise Rejected("unexpected container layout")
    return containers[0]["image"]


def _only_image_changed(before, after, name, approved_image):
    old = copy.deepcopy(before)
    new = copy.deepcopy(after)
    if _image(new, name) != approved_image or _image(old, name) == approved_image:
        raise Rejected("rollback image is not the approved digest or is unchanged")
    new["spec"]["containers"][0]["image"] = old["spec"]["containers"][0]["image"]
    if new != old:
        raise Rejected("rollback may change only the primary container image")


def plan_undo(deployment, replicasets, name, approved_image):
    """Return CAS patch only if an owned, older RS records the exact safe template."""
    if name not in TARGETS or deployment["metadata"]["name"] != name:
        raise Rejected("target is not allowlisted")
    if not DIGEST.fullmatch(approved_image):
        raise Rejected("rollback image must be digest-pinned")
    uid = deployment["metadata"].get("uid")
    resource_version = deployment["metadata"].get("resourceVersion")
    if not uid or not resource_version:
        raise Rejected("missing Deployment identity")
    current = _template(deployment)
    current_revision = _revision(deployment)
    owned = [rs for rs in replicasets if any(
        o.get("uid") == uid and o.get("kind") == "Deployment" and
        o.get("name") == name and o.get("controller") is True
        for o in rs["metadata"].get("ownerReferences") or [])]
    candidates = sorted((rs for rs in owned if _revision(rs) < current_revision),
                        key=_revision, reverse=True)
    for rs in candidates:
        previous = _template(rs)
        if previous == current:
            continue
        _only_image_changed(current, previous, name, approved_image)
        return [
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
            {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": _image(current, name)},
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": approved_image},
        ], _revision(rs), rs["metadata"]["name"]
    raise Rejected("no owned previous revision with the exact approved image and unchanged template")


def validate_admission(review, approved):
    req = review.get("request") or {}
    uid = req.get("uid")
    response = {"uid": uid, "allowed": False, "status": {"message": "rollback admission rejected"}}
    try:
        if (req.get("userInfo") or {}).get("username") != CONTROLLER_USER:
            raise Rejected("not the rollback controller")
        if req.get("namespace") != NAMESPACE or req.get("operation") != "UPDATE":
            raise Rejected("wrong namespace or operation")
        resource = req.get("resource") or {}
        if resource.get("group") != "apps" or resource.get("resource") != "deployments":
            raise Rejected("not a Deployment update")
        name = req.get("name")
        if name not in TARGETS:
            raise Rejected("target not allowlisted")
        old, new = req["oldObject"], req["object"]
        if old["metadata"]["uid"] != new["metadata"]["uid"] or old["metadata"]["name"] != name:
            raise Rejected("Deployment identity changed")
        if old["spec"].get("selector") != new["spec"].get("selector"):
            raise Rejected("selector changed")
        spec_old, spec_new = copy.deepcopy(old["spec"]), copy.deepcopy(new["spec"])
        _only_image_changed(_template(old), _template(new), name, approved[name])
        spec_new["template"]["spec"]["containers"][0]["image"] = spec_old["template"]["spec"]["containers"][0]["image"]
        if spec_old != spec_new:
            raise Rejected("non-image Deployment spec changed")
        if any(old["metadata"].get(key) != new["metadata"].get(key)
               for key in ("labels", "annotations", "ownerReferences", "finalizers")):
            raise Rejected("Deployment metadata changed")
        response = {"uid": uid, "allowed": True}
    except (Rejected, KeyError, IndexError, TypeError, AttributeError):
        pass  # Fail closed; do not echo request content (may contain credentials).
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": response}


def authenticate(auth, header):
    if not header or not header.startswith("Bearer ") or len(header) > 8192:
        raise Rejected("rollback caller not authorized")
    token = header[7:]
    if not token or any(ch.isspace() for ch in token):
        raise Rejected("rollback caller not authorized")
    # The apiserver verifies signature, expiry and dedicated projected audience.
    review = auth.create_token_review({"apiVersion": "authentication.k8s.io/v1",
                                       "kind": "TokenReview", "spec": {"token": token,
                                       "audiences": [AUDIENCE]}})
    status = review.status
    if (not status.authenticated or status.error or
            status.user.username != CALLER_USER or
            AUDIENCE not in (status.audiences or [])):
        raise Rejected("rollback caller not authorized")


def undo_json(apps, api_client, name, approved, expected_revision, expected_source):
    if name not in TARGETS:
        raise Rejected("target not allowlisted")
    dep = api_client.sanitize_for_serialization(apps.read_namespaced_deployment(name, NAMESPACE))
    replicasets = [api_client.sanitize_for_serialization(rs) for rs in
                   apps.list_namespaced_replica_set(NAMESPACE).items]
    ops, revision, rs_name = plan_undo(dep, replicasets, name, approved[name])
    if _revision(dep) != expected_revision or rs_name != expected_source:
        raise Rejected("approved revision provenance changed")
    apps.patch_namespaced_deployment(name, NAMESPACE, ops)
    return {"to_revision": revision, "to_replicaset": rs_name, "to_images": [approved[name]],
            "before": {"revision": _revision(dep), "images": [_image(_template(dep), name)]}}


class Handler(BaseHTTPRequestHandler):
    apps: ClassVar[Any] = None
    api_client: ClassVar[Any] = None
    auth: ClassVar[Any] = None
    approved: ClassVar[dict[str, str]] = {}
    grant_key: ClassVar[bytes] = b""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Never log URLs or request bodies.

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            path = self.path.split("?", 1)[0]  # kube-apiserver appends ?timeout= to webhook calls
            limit = 128 * 1024 if path == "/validate" else 2048
            if size < 1 or size > limit:
                raise Rejected("invalid request size")
            body = json.loads(self.rfile.read(size))
            if path == "/validate" and getattr(self.server, "server_port", None) == 8443:
                result = validate_admission(body, self.approved)
                status = 200
            elif self.path == "/undo" and getattr(self.server, "server_port", None) == 8443:
                authenticate(self.auth, self.headers.get("Authorization"))
                rollback_grant.verify(self.grant_key, body)
                result = undo_json(self.apps, self.api_client, body["name"], self.approved,
                                   body["expected_revision"], body["expected_source"])
                status = 200
            else:
                raise Rejected("unknown route")
        except (Rejected, InvalidGrant) as exc:
            result, status = {"error": str(exc)}, 403
        except (ValueError, TypeError):
            result, status = {"error": "rollback denied"}, 403
        except Exception:
            result, status = {"error": "rollback unavailable"}, 503
        payload = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    from kubernetes import client, config
    approved = trusted_images()
    config.load_incluster_config()
    Handler.apps = client.AppsV1Api()
    Handler.api_client = client.ApiClient()
    Handler.auth = client.AuthenticationV1Api()
    Handler.approved = approved
    Handler.grant_key = rollback_grant.load_key()
    secured = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/var/run/rollback-tls/tls.crt", "/var/run/rollback-tls/tls.key")
    secured.socket = context.wrap_socket(secured.socket, server_side=True)
    secured.serve_forever()


if __name__ == "__main__":
    main()
