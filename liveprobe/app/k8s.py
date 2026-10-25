"""Kubernetes observation helpers; writes go through a separate rollback controller."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_api = None


class Kube:
    """Thin wrapper around the official client so tests can substitute a fake."""

    def __init__(self, in_cluster: bool = True):
        from kubernetes import client, config
        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        self.client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.api_client = client.ApiClient()

    def to_dict(self, obj: Any) -> dict:
        return self.api_client.sanitize_for_serialization(obj)

    def executor(self, token_file: str, ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                 namespace: str | None = None, cfg=None):
        """Write client for the dedicated safe-action-executor identity, or None when its token is not mounted.

        The API's own service account stays read-only (and is denied writes by admission); only this projected,
        separately-bound token can patch the two target Deployments / delete controller-owned target Pods."""
        import os
        from .actions import SafeActionKube
        from .config import get_app_config
        app_cfg = cfg or get_app_config()
        if not os.path.isfile(token_file):
            return None
        cfg = self.client.Configuration()
        cfg.host = "https://%s:%s" % (os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
                                      os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        cfg.ssl_ca_cert = ca_file
        cfg.api_key = {"authorization": "Bearer " + open(token_file).read().strip()}

        class _Tok:   # re-read per request so the projected token's rotation needs no restart
            pass
        api = self.client.ApiClient(cfg)
        return SafeActionKube(self, self.client.CoreV1Api(api), self.client.AppsV1Api(api), self.client.ApiException,
                              namespace=namespace or app_cfg.target_namespaces[0], cfg=app_cfg)

    # --- reads -------------------------------------------------------------
    def list_pods(self, ns: str, selector: dict[str, str] | None = None) -> list[dict]:
        ls = ",".join(f"{k}={v}" for k, v in (selector or {}).items()) or None
        return [self.to_dict(p) for p in self.core.list_namespaced_pod(ns, label_selector=ls, _request_timeout=8).items]

    def get_pod(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_pod(name, ns))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_deployment(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.apps.read_namespaced_deployment(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_deployments(self, ns: str) -> list[dict]:
        return [self.to_dict(d) for d in self.apps.list_namespaced_deployment(ns).items]

    def get_replicaset(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.apps.read_namespaced_replica_set(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_replicasets(self, ns: str) -> list[dict]:
        return [self.to_dict(r) for r in self.apps.list_namespaced_replica_set(ns, _request_timeout=8).items]

    def get_service(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_service(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_services(self, ns: str) -> list[dict]:
        return [self.to_dict(s) for s in self.core.list_namespaced_service(ns, _request_timeout=8).items]

    def get_endpoints(self, ns: str, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_namespaced_endpoints(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def list_events(self, ns: str) -> list[dict]:
        return [self.to_dict(e) for e in self.core.list_namespaced_event(ns, _request_timeout=8).items]

    def get_node(self, name: str) -> dict | None:
        try:
            return self.to_dict(self.core.read_node(name, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def configmap_meta(self, ns: str, name: str) -> dict | None:
        """Read one referenced ConfigMap; values and annotations never cross this boundary."""
        try:
            cm = self.to_dict(self.core.read_namespaced_config_map(name, ns, _request_timeout=8))
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise
        return configmap_fingerprint(cm)

    def get_namespace_uid(self, name: str) -> str | None:
        """Cluster identity probe: the UID of a namespace that exists in every cluster (read-only)."""
        try:
            return (self.to_dict(self.core.read_namespace(name)).get("metadata") or {}).get("uid")
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    # --- cluster-wide, read-only inventory for the topology map (P3) --------
    def cluster_inventory(self) -> dict:
        """Nodes, pods, services and endpoints of every namespace. Read-only; fields are trimmed to what the map needs."""
        def pod_view(p: dict) -> dict:
            md, st = p.get("metadata") or {}, p.get("status") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {},
                                 "ownerReferences": md.get("ownerReferences") or []},
                    "spec": {"nodeName": (p.get("spec") or {}).get("nodeName"),
                             "images": [c.get("image") for c in (p.get("spec") or {}).get("containers") or []]},
                    "status": {"phase": st.get("phase"), "podIP": st.get("podIP"),
                               "containerStatuses": [{"ready": c.get("ready")} for c in st.get("containerStatuses") or []]}}
        def svc_view(s: dict) -> dict:
            md, sp = s.get("metadata") or {}, s.get("spec") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"clusterIP": sp.get("clusterIP"), "selector": sp.get("selector") or {},
                             "ports": [{"port": x.get("port")} for x in sp.get("ports") or []]}}
        def ep_view(e: dict) -> dict:
            md = e.get("metadata") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "subsets": [{"addresses": [{"ip": a.get("ip")} for a in s.get("addresses") or []]} for s in e.get("subsets") or []]}
        def ing_view(i: dict) -> dict:
            md, sp = i.get("metadata") or {}, i.get("spec") or {}
            rules = []
            for r in sp.get("rules") or []:
                for pth in ((r.get("http") or {}).get("paths") or []):
                    svc = ((pth.get("backend") or {}).get("service") or {})
                    rules.append({"host": r.get("host"), "path": pth.get("path") or "/", "service": svc.get("name"),
                                  "port": (svc.get("port") or {}).get("number") or (svc.get("port") or {}).get("name")})
            dflt = ((sp.get("default_backend") or sp.get("defaultBackend") or {}).get("service") or {})
            if dflt.get("name"):
                rules.append({"host": None, "path": "/", "service": dflt["name"], "port": (dflt.get("port") or {}).get("number")})
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"ingressClassName": sp.get("ingress_class_name") or sp.get("ingressClassName"), "rules": rules}}
        def dep_view(d: dict) -> dict:
            md, sp, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
            return {"metadata": {"namespace": md.get("namespace"), "name": md.get("name"), "labels": md.get("labels") or {}},
                    "spec": {"replicas": sp.get("replicas")},
                    "status": {"readyReplicas": st.get("readyReplicas", 0)}}
        errors: list[str] = []
        extra = {"ingresses": [], "deployments": [], "namespaces": []}
        for key, load, trim in (
            ("ingresses", self.client.NetworkingV1Api().list_ingress_for_all_namespaces, ing_view),
            ("deployments", self.apps.list_deployment_for_all_namespaces, dep_view),
            ("namespaces", self.core.list_namespace, lambda n: {"metadata": {"name": (n.get("metadata") or {}).get("name")}}),
        ):
            try:
                extra[key] = [trim(self.to_dict(i)) for i in load(_request_timeout=8).items]
            except Exception as e:  # noqa: BLE001 - retain partial inventory with explicit source errors
                errors.append(f"{key}: {type(e).__name__}")
        return {
            "errors": errors, **extra,
            "nodes": [self.to_dict(n) for n in self.core.list_node(_request_timeout=8).items],
            "pods": [pod_view(self.to_dict(p)) for p in self.core.list_pod_for_all_namespaces(_request_timeout=8).items],
            "services": [svc_view(self.to_dict(s)) for s in self.core.list_service_for_all_namespaces(_request_timeout=8).items],
            "endpoints": [ep_view(self.to_dict(e)) for e in self.core.list_endpoints_for_all_namespaces(_request_timeout=8).items],
        }

    def cluster_warning_events(self) -> list[dict]:
        """Bounded raw Warning events for the operations overview; no pod logs or Secret reads."""
        return [self.to_dict(e) for e in self.core.list_event_for_all_namespaces(
            field_selector="type=Warning", limit=500, _request_timeout=8).items]

    # --- bounded request to independent, admission-guarded controller --------
    def request_rollback(self, ns: str, name: str, *, grant: dict | None = None) -> dict:
        import os
        from pathlib import Path
        import httpx
        from .config import get_app_config
        cfg = get_app_config()
        if ns not in cfg.target_namespaces:
            raise ValueError("rollback target not allowlisted")
        url = os.environ.get("ROLLBACK_CONTROLLER_URL")
        if url != "https://rollback-controller.liveprobe-system.svc:443/undo":
            raise RuntimeError("rollback controller is not configured")
        if grant is None or grant.get("name") != name or grant.get("namespace") != ns:
            raise RuntimeError("approved action grant required")
        try:
            token = Path("/var/run/secrets/rollback-caller/token").read_text().strip()
            with httpx.Client(verify="/var/run/rollback-ca/ca.crt", timeout=10) as client:
                response = client.post(url, headers={"Authorization": "Bearer " + token}, json=grant)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise RuntimeError("rollback controller rejected or unavailable") from None


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def owner_of(obj: dict, kind: str) -> str | None:
    for o in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if o.get("kind") == kind:
            return o.get("name")
    return None


def configmap_fingerprint(cm: dict) -> dict:
    """Value-free change metadata; data hash matches the approved-version contract."""
    import hashlib
    import json
    md, data = cm.get("metadata") or {}, cm.get("data") or {}
    times = [f["time"] for f in md.get("managedFields") or [] if f.get("time")]
    return {
        "name": md.get("name"), "keys": sorted(data), "binary_keys": sorted(cm.get("binaryData") or {}),
        "data_sha256": hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "content_sha256": hashlib.sha256(json.dumps({"data": data, "binaryData": cm.get("binaryData") or {}},
                                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "created": md.get("creationTimestamp"), "last_changed": max(times) if times else md.get("creationTimestamp"),
        "resource_version": md.get("resourceVersion"),
    }


def configmap_refs(tpl: dict) -> list[str]:
    """Only actual ConfigMap references, including init containers and projected volumes."""
    spec = tpl.get("spec") or {}
    names = set()
    for c in [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]:
        for env in c.get("envFrom") or []:
            if (env.get("configMapRef") or {}).get("name"):
                names.add(env["configMapRef"]["name"])
        for env in c.get("env") or []:
            ref = ((env.get("valueFrom") or {}).get("configMapKeyRef") or {}).get("name")
            if ref:
                names.add(ref)
    for vol in spec.get("volumes") or []:
        if (vol.get("configMap") or {}).get("name"):
            names.add(vol["configMap"]["name"])
        for src in (vol.get("projected") or {}).get("sources") or []:
            if (src.get("configMap") or {}).get("name"):
                names.add(src["configMap"]["name"])
    return sorted(names)


def node_summary(node: dict) -> dict:
    """Only scheduling/pressure evidence, never node annotations, addresses or messages."""
    st, spec = node.get("status") or {}, node.get("spec") or {}
    keep = {"Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}
    return {
        "name": (node.get("metadata") or {}).get("name"),
        "conditions": {c["type"]: {"status": c.get("status"), "reason": c.get("reason"),
                                    "since": c.get("lastTransitionTime")}
                       for c in st.get("conditions") or [] if c.get("type") in keep},
        "allocatable": {k: (st.get("allocatable") or {}).get(k) for k in ("cpu", "memory", "pods")},
        "taints": [{k: t[k] for k in ("key", "value", "effect") if k in t} for t in spec.get("taints") or []],
    }


def revision_of(obj: dict) -> int:
    ann = (obj.get("metadata") or {}).get("annotations") or {}
    try:
        return int(ann.get("deployment.kubernetes.io/revision", "0"))
    except ValueError:
        return 0


def strip_template(tpl: dict) -> dict:
    """Pod template without pod-template-hash and with env *values* removed (for storage/compare)."""
    t = copy.deepcopy(tpl or {})
    labels = (t.get("metadata") or {}).get("labels") or {}
    labels.pop("pod-template-hash", None)
    t.get("metadata", {}).pop("creationTimestamp", None)
    return t


def sanitize_template(tpl: dict) -> dict:
    t = strip_template(tpl)
    for c in ((t.get("spec") or {}).get("containers") or []) + ((t.get("spec") or {}).get("initContainers") or []):
        for e in c.get("env") or []:
            if "value" in e:
                e["value"] = "[dropped]"
    return t


def container_images(tpl: dict) -> list[str]:
    return [c.get("image", "") for c in ((tpl or {}).get("spec") or {}).get("containers") or []]


def pod_waiting_reasons(pod: dict) -> list[str]:
    out = []
    for cs in ((pod.get("status") or {}).get("containerStatuses") or []) + \
              ((pod.get("status") or {}).get("initContainerStatuses") or []):
        w = (cs.get("state") or {}).get("waiting")
        if w and w.get("reason"):
            out.append(w["reason"])
    return out


def pod_summary(pod: dict) -> dict:
    st = pod.get("status") or {}
    cs_out = []
    spec_images = {c.get("name"): c.get("image") for c in (pod.get("spec") or {}).get("containers") or []}
    for cs in st.get("containerStatuses") or []:
        state = cs.get("state") or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        spec_image = spec_images.get(cs.get("name"))
        image_id = cs.get("imageID")
        # Runtime status.image can be a local tag alias even for a digest-pinned
        # Pod. Only an exact repository+digest imageID match proves provenance;
        # absent or differently formatted IDs remain unverified, not safe.
        pinned = spec_image if spec_image and "@sha256:" in spec_image else None
        resolved = image_id.removeprefix("docker-pullable://") if image_id else None
        provenance = ("digest_matches_spec" if pinned and resolved == pinned else
                      "digest_mismatch" if pinned and resolved and "@sha256:" in resolved else
                      "unverified")
        cs_out.append({
            "name": cs.get("name"), "spec_image": spec_image, "image": cs.get("image"),
            "image_id": image_id, "image_provenance": provenance, "ready": cs.get("ready"),
            "restart_count": cs.get("restartCount"),
            "waiting_reason": (state.get("waiting") or {}).get("reason"),
            "waiting_message": ((state.get("waiting") or {}).get("message") or "")[:300] or None,
            "last_terminated_reason": last.get("reason"),
            "last_exit_code": last.get("exitCode"),
            "last_terminated_at": last.get("finishedAt"),
        })
    md = pod.get("metadata") or {}
    return {
        "name": md.get("name"), "labels": md.get("labels") or {}, "phase": st.get("phase"),
        "node": (pod.get("spec") or {}).get("nodeName"),
        "replicaset": owner_of(pod, "ReplicaSet"), "terminating": bool(md.get("deletionTimestamp")),
        "containers": cs_out,
    }


def labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def ready_addresses(ep: dict | None) -> int:
    if not ep:
        return 0
    return sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])


def deployment_for_pod(kube: Kube, ns: str, pod: dict) -> str | None:
    rs_name = owner_of(pod, "ReplicaSet")
    if not rs_name:
        return None
    rs = kube.get_replicaset(ns, rs_name)
    return owner_of(rs, "Deployment") if rs else None