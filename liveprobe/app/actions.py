"""KAN-135 (KAN-55 / KAN-84 / KAN-85): typed safe actions beyond rollout_undo.

Five narrowly scoped action types, each with:
  * a strict parameter schema (exact key set, typed values, no free-form fields),
  * target validation at proposal AND immediately before execution
    (API group / kind / name / namespace / ownership labels / bounds / policy),
  * a sanitized snapshot (only the fields the action touches + identity/resourceVersion),
  * an optimistic-concurrency write (JSON-patch ``test`` on resourceVersion, or delete preconditions),
  * a per-type post-verification and a compensating rollback whose failure is reported
    as ``indeterminate`` -- never as success,
  * a workload-scoped DB lock (``actions.lock_key`` partial unique index, migration 0005),
  * persisted audit rows for every proposal, denial, conflict, verification and rollback.

No free-form kubectl/YAML, no Secret/Node/control-plane access, no cross-namespace writes.
HIGH_RISK incidents never execute. Actions that change capacity, resources or configuration
require human approval even for SIMPLE incidents (policy may only become stricter).

The writer interface (``delete_pod`` / ``patch_deployment_json`` / ``patch_configmap_json`` /
``get_configmap`` and ``typed_actions_enabled``) is implemented by :class:`SafeActionKube`, a
client that must be constructed explicitly with the dedicated executor identity
(deploy/components/safe-actions). The API's observe-only ``Kube`` does not expose it, so typed
actions are denied (and audited) until an operator wires the executor.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import k8s as K
from .config import AppConfig
from .db import audit
from .models import Action, AuditLog, Incident, Setting, INCIDENT_OPEN_STATES

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
_RESTART_PTR = "kubectl.kubernetes.io~1restartedAt"
_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_MEM = re.compile(r"^([1-9][0-9]{0,5})(Mi|Gi)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# policy strictness order; overrides may only move to the right
_STRICTNESS = ("auto", "approval", "suggest_only", "handoff")


class Denied(Exception):
    pass


class TargetConflict(Exception):
    """The target changed after the snapshot (resourceVersion / precondition / JSON-patch test)."""


@dataclass(frozen=True)
class ActionSpec:
    api_group: str
    kind: str
    params: dict[str, type]
    min_policy: str          # the least strict policy this type may ever run under
    reversible: bool


TYPED: dict[str, ActionSpec] = {
    "pod_delete": ActionSpec("", "Pod", {}, "auto", False),
    "rollout_restart": ActionSpec("apps", "Deployment", {}, "auto", True),
    # Policy C (2026-10-05, user decision): everything below HIGH_RISK may run automatically. These three used to be pinned to
    # "approval"; the per-type floor is now "auto" and the real limits are the bounds (replicas 1..2, memory 256..768Mi,
    # approved ConfigMap versions only), reversibility and the admission policy -- plus global approval/suggest_only modes.
    "scale_replicas": ActionSpec("apps", "Deployment", {"replicas": int}, "auto", True),
    "set_memory_limit": ActionSpec("apps", "Deployment", {"memory_limit_mi": int}, "auto", True),
    "configmap_restore": ActionSpec("", "ConfigMap", {"version": str}, "auto", True),
}
ALL_ACTIONS = ("rollout_undo", *TYPED)


# --------------------------------------------------------------------------- helpers
def stricter(*modes: str) -> str:
    """Return the strictest policy; unknown values are treated as handoff (fail closed)."""
    return max(modes, key=lambda m: _STRICTNESS.index(m) if m in _STRICTNESS else len(_STRICTNESS))


def data_hash(data: dict | None) -> str:
    return hashlib.sha256(json.dumps(data or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def workload_lock(ns: str, workload: str) -> str:
    return f"{ns}/workload/{workload}"


def parse_mem_mi(v: Any) -> int:
    m = _MEM.fullmatch(str(v or ""))
    if not m:
        raise Denied(f"memory quantity {v!r} has unknown units/bounds; refusing")
    n = int(m.group(1))
    return n * 1024 if m.group(2) == "Gi" else n


def _meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


def _labels_ok(labels: dict | None, required: dict[str, str]) -> bool:
    labels = labels or {}
    return bool(required) and all(labels.get(k) == v for k, v in required.items())


def _check_params(action_type: str, params: dict) -> dict:
    spec = TYPED[action_type]
    if not isinstance(params, dict) or set(params) != set(spec.params):
        raise Denied(f"{action_type} parameters must be exactly {sorted(spec.params)}, got {sorted(params or {})}")
    for k, typ in spec.params.items():
        v = params[k]
        if isinstance(v, bool) or not isinstance(v, typ):
            raise Denied(f"{action_type} parameter {k!r} must be {typ.__name__}")
    return dict(params)


def _single_container(dep: dict) -> dict:
    containers = (((dep.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    name = _meta(dep).get("name")
    if len(containers) != 1 or containers[0].get("name") != name:
        raise Denied("unexpected container layout; only the single primary container may be changed")
    return containers[0]


def incident_mode(s: Session, incident_id: int) -> str | None:
    row = s.get(Setting, f"incident_policy_mode:{incident_id}")
    v = (row.value or {}).get("mode") if row else None
    return v if v in _STRICTNESS else None


def typed_policy(s: Session, level: str, action_type: str, incident_id: int) -> str:
    from .safety import effective_policy, global_mode, has_network_risk
    if has_network_risk(s.get(Incident, incident_id)):
        return 'handoff'
    base = effective_policy(level, global_mode(s))
    modes = [base, TYPED[action_type].min_policy]
    inc_mode = incident_mode(s, incident_id)
    if inc_mode:
        modes.append(inc_mode)
    return stricter(*modes)


# --------------------------------------------------------------------------- validation
@dataclass
class Validated:
    action_type: str
    ns: str
    kind: str
    name: str
    workload: str
    params: dict
    snapshot: dict                 # sanitized; persisted
    fingerprint: dict              # spec-level identity compared between proposal and execution
    private: dict = field(default_factory=dict)  # in-memory only (e.g. ConfigMap data), never persisted

    @property
    def lock_key(self) -> str:
        return workload_lock(self.ns, self.workload)


def _deployment(cfg: AppConfig, kube: Any, ns: str, name: str) -> dict:
    pol = cfg.safe_actions
    if name not in pol.deployments:
        raise Denied(f"Deployment {name!r} is not an allowlisted safe-action target {pol.deployments}")
    dep = kube.get_deployment(ns, name)
    if not dep:
        raise Denied(f"Deployment {ns}/{name} does not exist")
    if _meta(dep).get("name") != name or _meta(dep).get("namespace", ns) != ns:
        raise Denied("Deployment identity mismatch")
    tpl_labels = (((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels")
    if not _labels_ok(_meta(dep).get("labels"), pol.ownership_labels) or not _labels_ok(tpl_labels, pol.ownership_labels):
        raise Denied(f"Deployment {name!r} lacks required ownership labels {pol.ownership_labels}")
    if not _meta(dep).get("resourceVersion") or not _meta(dep).get("uid"):
        raise Denied("Deployment is missing uid/resourceVersion; cannot guarantee optimistic concurrency")
    return dep


def _dep_snapshot(dep: dict) -> dict:
    spec = dep.get("spec") or {}
    tpl = spec.get("template") or {}
    c = ((tpl.get("spec") or {}).get("containers") or [{}])[0]
    return {
        "uid": _meta(dep).get("uid"), "resourceVersion": _meta(dep).get("resourceVersion"),
        "generation": _meta(dep).get("generation"), "revision": K.revision_of(dep),
        "replicas": spec.get("replicas", 1),
        "restarted_at": ((tpl.get("metadata") or {}).get("annotations") or {}).get(RESTART_ANNOTATION),
        "memory_limit": ((c.get("resources") or {}).get("limits") or {}).get("memory"),
        "memory_request": ((c.get("resources") or {}).get("requests") or {}).get("memory"),
        "images": K.container_images(tpl),
    }


def validate(cfg: AppConfig, kube: Any, action_type: str, ns: str, kind: str, name: str, params: dict,
             api_group: str | None = None) -> Validated:
    """All static and live checks. Raises Denied; never mutates."""
    if action_type not in TYPED:
        raise Denied(f"action type {action_type!r} is not a known typed safe action")
    if action_type not in cfg.allowlist:
        raise Denied(f"action type {action_type!r} is not in the allowlist {cfg.allowlist}")
    if ns != cfg.target_namespace:
        raise Denied(f"target namespace {ns!r} is not the configured target namespace {cfg.target_namespace!r}")
    spec = TYPED[action_type]
    if kind != spec.kind:
        raise Denied(f"{action_type} only supports kind {spec.kind}, got {kind!r}")
    if api_group is not None and api_group != spec.api_group:
        raise Denied(f"{action_type} only supports API group {spec.api_group!r}, got {api_group!r}")
    if not isinstance(name, str) or not _DNS1123.fullmatch(name):
        raise Denied(f"target name {name!r} is not a valid DNS-1123 name")
    params = _check_params(action_type, params)
    pol = cfg.safe_actions
    handler = {"pod_delete": _v_pod_delete, "rollout_restart": _v_restart, "scale_replicas": _v_scale,
               "set_memory_limit": _v_limit, "configmap_restore": _v_configmap}[action_type]
    return handler(cfg, pol, kube, ns, name, params)


def _v_pod_delete(cfg, pol, kube, ns, name, params) -> Validated:
    pod = kube.get_pod(ns, name)
    if not pod:
        raise Denied(f"Pod {ns}/{name} does not exist")
    md = _meta(pod)
    if md.get("deletionTimestamp"):
        raise Denied("Pod is already terminating")
    if not _labels_ok(md.get("labels"), pol.ownership_labels):
        raise Denied(f"Pod {name!r} lacks required ownership labels {pol.ownership_labels}")
    rs_name = K.owner_of(pod, "ReplicaSet")
    rs = kube.get_replicaset(ns, rs_name) if rs_name else None
    dep_name = K.owner_of(rs, "Deployment") if rs else None
    if not dep_name:
        raise Denied("Pod is not controller-owned by a Deployment ReplicaSet; deletion would not be recreated")
    dep = _deployment(cfg, kube, ns, dep_name)
    if not md.get("uid") or not md.get("resourceVersion"):
        raise Denied("Pod is missing uid/resourceVersion; cannot use delete preconditions")
    if ((dep.get("spec") or {}).get("replicas") or 0) < 1:
        raise Denied("owning Deployment is scaled to zero")
    snap = {"pod": {"uid": md["uid"], "resourceVersion": md["resourceVersion"], "replicaset": rs_name},
            "deployment": _dep_snapshot(dep)}
    return Validated("pod_delete", ns, "Pod", name, dep_name, params, snap, {"pod_uid": md["uid"]})


def _v_restart(cfg, pol, kube, ns, name, params) -> Validated:
    dep = _deployment(cfg, kube, ns, name)
    snap = _dep_snapshot(dep)
    return Validated("rollout_restart", ns, "Deployment", name, name, params, snap,
                     {"uid": snap["uid"], "generation": snap["generation"]})


def _v_scale(cfg, pol, kube, ns, name, params) -> Validated:
    dep = _deployment(cfg, kube, ns, name)
    snap = _dep_snapshot(dep)
    want, cur = params["replicas"], snap["replicas"]
    lo, hi = pol.replicas_min, pol.replicas_max
    if not lo <= want <= hi:
        raise Denied(f"replicas {want} outside approved bounds [{lo}, {hi}]")
    if not isinstance(cur, int) or not lo <= cur <= hi:
        raise Denied(f"current replicas {cur!r} outside approved bounds; compensation could not be bounded")
    if want == cur:
        raise Denied("replicas already at requested value (no-op)")
    return Validated("scale_replicas", ns, "Deployment", name, name, params, snap,
                     {"uid": snap["uid"], "generation": snap["generation"], "replicas": cur})


def _v_limit(cfg, pol, kube, ns, name, params) -> Validated:
    dep = _deployment(cfg, kube, ns, name)
    _single_container(dep)
    snap = _dep_snapshot(dep)
    want = params["memory_limit_mi"]
    lo, hi = pol.memory_limit_min_mi, pol.memory_limit_max_mi
    if not lo <= want <= hi:
        raise Denied(f"memory limit {want}Mi outside approved bounds [{lo}, {hi}]Mi")
    if snap["memory_limit"] is None:
        raise Denied("current memory limit is unset; unknown bound, refusing")
    cur = parse_mem_mi(snap["memory_limit"])
    if not lo <= cur <= hi:
        raise Denied(f"current memory limit {cur}Mi outside approved bounds; compensation could not be bounded")
    if snap["memory_request"] is not None and want < parse_mem_mi(snap["memory_request"]):
        raise Denied("memory limit may not be lower than the memory request")
    if want == cur:
        raise Denied("memory limit already at requested value (no-op)")
    return Validated("set_memory_limit", ns, "Deployment", name, name, params, snap,
                     {"uid": snap["uid"], "generation": snap["generation"], "memory_limit": snap["memory_limit"]})


def _v_configmap(cfg, pol, kube, ns, name, params) -> Validated:
    if not hasattr(kube, "get_configmap"):
        raise Denied("ConfigMap reads are not available to this client; configmap_restore requires the executor")
    approved = pol.configmap_versions.get(name) or []
    if not approved:
        raise Denied(f"no approved immutable version history for ConfigMap {name!r}; refusing to reconstruct")
    version = params["version"]
    entry = next((v for v in approved if v.name == version), None)
    if entry is None:
        raise Denied(f"version {version!r} is not an approved immutable version of {name!r}")
    if not _DNS1123.fullmatch(version) or not _SHA256.fullmatch(entry.sha256):
        raise Denied("approved version reference is malformed")
    live = kube.get_configmap(ns, name)
    if not live:
        raise Denied(f"ConfigMap {ns}/{name} does not exist")
    lmd = _meta(live)
    if not _labels_ok(lmd.get("labels"), pol.ownership_labels):
        raise Denied(f"ConfigMap {name!r} lacks required ownership labels {pol.ownership_labels}")
    if live.get("immutable"):
        raise Denied("live ConfigMap is immutable; it cannot be restored in place")
    if live.get("binaryData"):
        raise Denied("ConfigMaps with binaryData are out of scope")
    consumer = (lmd.get("labels") or {}).get("app")
    dep = _deployment(cfg, kube, ns, consumer or "")
    if not lmd.get("resourceVersion") or not lmd.get("uid"):
        raise Denied("ConfigMap is missing uid/resourceVersion")
    src = kube.get_configmap(ns, version)
    if not src:
        raise Denied(f"approved version ConfigMap {version!r} does not exist; refusing to reconstruct")
    if src.get("immutable") is not True:
        raise Denied(f"version ConfigMap {version!r} is not immutable")
    if not _labels_ok(_meta(src).get("labels"), pol.ownership_labels) or src.get("binaryData"):
        raise Denied(f"version ConfigMap {version!r} lacks ownership labels or has binaryData")
    src_hash = data_hash(src.get("data"))
    if src_hash != entry.sha256:
        raise Denied(f"version ConfigMap {version!r} content hash does not match the approved hash")
    live_hash = data_hash(live.get("data"))
    if live_hash == src_hash:
        raise Denied("live ConfigMap already matches the approved version (no-op)")
    snap = {"configmap": {"uid": lmd["uid"], "resourceVersion": lmd["resourceVersion"], "data_sha256": live_hash,
                          "keys": sorted((live.get("data") or {}).keys())},
            "version": {"name": version, "data_sha256": src_hash}, "deployment": _dep_snapshot(dep)}
    return Validated("configmap_restore", ns, "ConfigMap", name, _meta(dep)["name"], params, snap,
                     {"uid": lmd["uid"], "data_sha256": live_hash, "version_sha256": src_hash},
                     private={"old_data": dict(live.get("data") or {}), "new_data": dict(src.get("data") or {})})


# --------------------------------------------------------------------------- mutation / verification
def _restart_ops(dep_snap: dict, current_tpl_annotations: dict | None, value: str, *, test_rv: bool) -> list[dict]:
    ops: list[dict] = []
    if test_rv:
        ops.append({"op": "test", "path": "/metadata/resourceVersion", "value": dep_snap["resourceVersion"]})
    if current_tpl_annotations is None:
        ops.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": {RESTART_ANNOTATION: value}})
    else:
        ops.append({"op": "add", "path": f"/spec/template/metadata/annotations/{_RESTART_PTR}", "value": value})
    return ops


def _tpl_annotations(dep: dict) -> dict | None:
    return (((dep.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def apply(kube: Any, v: Validated, *, now: Callable[[], str] = _now) -> dict:
    """Perform the single bounded write. Raises TargetConflict if the snapshot is stale."""
    ns, name = v.ns, v.name
    if v.action_type == "pod_delete":
        p = v.snapshot["pod"]
        kube.delete_pod(ns, name, uid=p["uid"], resource_version=p["resourceVersion"])
        return {"deleted_pod_uid": p["uid"]}
    dsnap = v.snapshot["deployment"] if v.action_type == "configmap_restore" else v.snapshot
    if v.action_type == "rollout_restart":
        dep = kube.get_deployment(ns, name) or {}
        stamp = now()
        ops = _restart_ops(dsnap, _tpl_annotations(dep), stamp, test_rv=True)
        if dsnap["restarted_at"] is not None:
            ops.insert(1, {"op": "test", "path": f"/spec/template/metadata/annotations/{_RESTART_PTR}",
                           "value": dsnap["restarted_at"]})
        kube.patch_deployment_json(ns, name, ops)
        return {"restarted_at": stamp}
    if v.action_type == "scale_replicas":
        kube.patch_deployment_json(ns, name, [
            {"op": "test", "path": "/metadata/resourceVersion", "value": dsnap["resourceVersion"]},
            {"op": "test", "path": "/spec/replicas", "value": dsnap["replicas"]},
            {"op": "replace", "path": "/spec/replicas", "value": v.params["replicas"]}])
        return {"replicas": v.params["replicas"]}
    if v.action_type == "set_memory_limit":
        new = f"{v.params['memory_limit_mi']}Mi"
        kube.patch_deployment_json(ns, name, [
            {"op": "test", "path": "/metadata/resourceVersion", "value": dsnap["resourceVersion"]},
            {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": name},
            {"op": "test", "path": "/spec/template/spec/containers/0/resources/limits/memory",
             "value": dsnap["memory_limit"]},
            {"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value": new}])
        return {"memory_limit": new}
    if v.action_type == "configmap_restore":
        cm = v.snapshot["configmap"]
        res = kube.patch_configmap_json(ns, name, [
            {"op": "test", "path": "/metadata/resourceVersion", "value": cm["resourceVersion"]},
            {"op": "replace", "path": "/data", "value": v.private["new_data"]}])
        applied = {"configmap_resourceVersion": _meta(res or {}).get("resourceVersion"),
                   "data_sha256": v.snapshot["version"]["data_sha256"]}
        # envFrom is read at container start: restart the single consumer Deployment.
        dep = kube.get_deployment(ns, v.workload) or {}
        stamp = now()
        cur = _meta(dep).get("resourceVersion")
        try:
            kube.patch_deployment_json(ns, v.workload, _restart_ops(
                {"resourceVersion": cur}, _tpl_annotations(dep), stamp, test_rv=True))
        except Exception as e:  # noqa: BLE001 - the ConfigMap already changed; caller must compensate
            applied["restart_error"] = f"{type(e).__name__}"
            return applied
        applied["restarted_at"] = stamp
        return applied
    raise Denied(f"no handler for {v.action_type}")


def verify(kube: Any, cfg: AppConfig, v: Validated, applied: dict, *, sleep=time.sleep,
           timeout_s: int | None = None) -> tuple[bool, dict]:
    from .safety import verify_rollout
    timeout = cfg.rollout_verify_timeout_seconds if timeout_s is None else timeout_s
    ns = v.ns
    if v.action_type == "configmap_restore" and applied.get("restart_error"):
        return False, {"reason": "consumer restart failed after ConfigMap write", **applied}
    ok, state = verify_rollout(kube, ns, v.workload, timeout, sleep=sleep)
    dep = kube.get_deployment(ns, v.workload) or {}
    snap = _dep_snapshot(dep) if dep else {}
    checks: dict[str, bool] = {"rollout_healthy": ok}
    if v.action_type == "pod_delete":
        pod = kube.get_pod(ns, v.name)
        checks["old_pod_gone"] = not pod or _meta(pod).get("uid") != applied["deleted_pod_uid"]
    elif v.action_type == "rollout_restart":
        checks["annotation_applied"] = snap.get("restarted_at") == applied["restarted_at"]
    elif v.action_type == "scale_replicas":
        checks["replicas_applied"] = snap.get("replicas") == applied["replicas"]
    elif v.action_type == "set_memory_limit":
        checks["limit_applied"] = snap.get("memory_limit") == applied["memory_limit"]
    elif v.action_type == "configmap_restore":
        live = kube.get_configmap(ns, v.name) or {}
        checks["configmap_matches_version"] = data_hash(live.get("data")) == applied["data_sha256"]
        checks["consumer_restarted"] = snap.get("restarted_at") == applied.get("restarted_at")
    state = {**state, "checks": checks}
    return all(checks.values()), state


def compensate(kube: Any, v: Validated, applied: dict, *, now: Callable[[], str] = _now) -> dict:
    """Reverse the bounded write, guarded by a JSON-patch test on the value we wrote.

    Raises on any failure; the caller records the outcome as indeterminate.
    """
    ns, name = v.ns, v.name
    if v.action_type == "pod_delete":
        raise Denied("pod deletion is not reversible")
    if v.action_type == "rollout_restart":
        prev = v.snapshot["restarted_at"]
        test = {"op": "test", "path": f"/spec/template/metadata/annotations/{_RESTART_PTR}",
                "value": applied["restarted_at"]}
        op = ({"op": "remove", "path": f"/spec/template/metadata/annotations/{_RESTART_PTR}"} if prev is None else
              {"op": "replace", "path": f"/spec/template/metadata/annotations/{_RESTART_PTR}", "value": prev})
        kube.patch_deployment_json(ns, name, [test, op])
        return {"restarted_at": prev}
    if v.action_type == "scale_replicas":
        kube.patch_deployment_json(ns, name, [
            {"op": "test", "path": "/spec/replicas", "value": applied["replicas"]},
            {"op": "replace", "path": "/spec/replicas", "value": v.snapshot["replicas"]}])
        return {"replicas": v.snapshot["replicas"]}
    if v.action_type == "set_memory_limit":
        path = "/spec/template/spec/containers/0/resources/limits/memory"
        kube.patch_deployment_json(ns, name, [
            {"op": "test", "path": path, "value": applied["memory_limit"]},
            {"op": "replace", "path": path, "value": v.snapshot["memory_limit"]}])
        return {"memory_limit": v.snapshot["memory_limit"]}
    if v.action_type == "configmap_restore":
        if "old_data" not in v.private:
            raise Denied("pre-action ConfigMap content unavailable (not persisted by design)")
        rv = applied.get("configmap_resourceVersion")
        if not rv:
            raise TargetConflict("resourceVersion after restore unknown; refusing blind compensation")
        kube.patch_configmap_json(ns, name, [
            {"op": "test", "path": "/metadata/resourceVersion", "value": rv},
            {"op": "replace", "path": "/data", "value": v.private["old_data"]}])
        dep = kube.get_deployment(ns, v.workload) or {}
        stamp = now()
        kube.patch_deployment_json(ns, v.workload, _restart_ops(
            {"resourceVersion": _meta(dep).get("resourceVersion")}, _tpl_annotations(dep), stamp, test_rv=True))
        return {"data_sha256": v.snapshot["configmap"]["data_sha256"], "restarted_at": stamp}
    raise Denied(f"no compensation for {v.action_type}")


def verify_compensation(kube: Any, cfg: AppConfig, v: Validated, comp: dict, *, sleep=time.sleep,
                        timeout_s: int | None = None) -> tuple[bool, dict]:
    from .safety import verify_rollout
    timeout = cfg.rollout_verify_timeout_seconds if timeout_s is None else timeout_s
    ok, state = verify_rollout(kube, v.ns, v.workload, timeout, sleep=sleep)
    snap = _dep_snapshot(kube.get_deployment(v.ns, v.workload) or {"metadata": {}})
    checks = {"rollout_healthy": ok}
    if v.action_type == "rollout_restart":
        checks["annotation_restored"] = snap.get("restarted_at") == comp["restarted_at"]
    elif v.action_type == "scale_replicas":
        checks["replicas_restored"] = snap.get("replicas") == comp["replicas"]
    elif v.action_type == "set_memory_limit":
        checks["limit_restored"] = snap.get("memory_limit") == comp["memory_limit"]
    elif v.action_type == "configmap_restore":
        live = kube.get_configmap(v.ns, v.name) or {}
        checks["configmap_restored"] = data_hash(live.get("data")) == comp["data_sha256"]
    return all(checks.values()), {**state, "checks": checks}


# --------------------------------------------------------------------------- proposal / execution
def _target(ns: str, kind: str, name: str) -> str:
    return f"{ns}/{kind}/{name}"


def propose_typed(s: Session, cfg: AppConfig, kube: Any, incident: Incident, level: str, action_type: str,
                  kind: str, name: str, params: dict | None = None, api_group: str | None = None,
                  actor: str = "liveprobe") -> Action:
    params = dict(params or {})
    target = _target(incident.namespace, kind, name)
    known = action_type in TYPED
    policy = typed_policy(s, level, action_type, incident.id) if known else "handoff"
    act = Action(incident_id=incident.id, type=action_type, target=target,
                 params={"kind": kind, "name": name, "api_group": api_group, "args": params},
                 policy_mode=policy, status="proposed")
    s.add(act)
    s.flush()
    audit(s, actor, "action_proposed", incident.id, action_id=act.id, type=action_type, target=target,
          level=level, policy=policy, args=params)
    try:
        if not known:
            raise Denied(f"action type {action_type!r} is not a known typed safe action")
        v = validate(cfg, kube, action_type, incident.namespace, kind, name, params, api_group)
        if v.workload != incident.workload:
            raise Denied(f"action workload {v.workload!r} differs from incident workload {incident.workload!r}")
    except Denied as e:
        act.status, act.reason = "denied", str(e)[:500]
        audit(s, actor, "action_denied", incident.id, action_id=act.id, stage="proposal", reason=act.reason)
        return act
    act.lock_key = v.lock_key
    act.params = {**act.params, "workload": v.workload, "proposal_fingerprint": v.fingerprint,
                  "proposal_snapshot": v.snapshot}
    if policy == "handoff":
        act.status, act.reason = "denied", "HIGH_RISK policy: automatic actions are not allowed"
        audit(s, actor, "action_denied", incident.id, action_id=act.id, stage="proposal", reason=act.reason)
    elif policy == "suggest_only":
        act.status, act.reason = "denied", "policy suggest_only: action recorded as suggestion only"
        audit(s, actor, "action_suggested_only", incident.id, action_id=act.id)
    elif policy == "approval":
        act.status = "pending_approval"
        audit(s, actor, "action_pending_approval", incident.id, action_id=act.id)
    return act


def _authorized(s: Session, act: Action, inc: Incident | None) -> str | None:
    """Return None when executable, else a denial reason."""
    if act.type not in TYPED:
        return "not a typed safe action"
    if inc is None or inc.status not in INCIDENT_OPEN_STATES:
        return "incident is not open"
    from .safety import has_network_risk
    if has_network_risk(inc):
        return 'NetworkDrop: operator handoff; automatic actions are not allowed'
    level = inc.severity_level or "COMPLEX"
    from .safety import high_risk_may_execute
    if level == "HIGH_RISK" and not high_risk_may_execute():
        return "HIGH_RISK incidents never execute actions"
    now_policy = typed_policy(s, level, act.type, inc.id)
    if act.status == "proposed":
        return None if act.policy_mode == "auto" and now_policy == "auto" else "action lacks executable policy"
    if act.status == "pending_approval":
        if act.policy_mode != "approval" or now_policy not in ("auto", "approval"):
            return "policy no longer permits execution"
        approved = s.scalar(select(AuditLog.id).where(
            AuditLog.event == "action_approved", AuditLog.incident_id == inc.id,
            AuditLog.detail["action_id"].as_integer() == act.id).limit(1))
        return None if approved is not None else "action lacks executable approval"
    return f"action is {act.status}"


@dataclass
class TypedResult:
    action_id: int
    status: str
    reason: str | None = None


def _finish(session_factory, action_id: int, actor: str, status: str, reason: str, event: str, **detail):
    with session_factory() as s:
        act = s.get(Action, action_id)
        act.status, act.reason = status, reason[:500]
        for k in ("after_state",):
            if k in detail:
                act.after_state = detail[k]
        audit(s, actor, event, act.incident_id, action_id=act.id, reason=act.reason,
              **{k: v for k, v in detail.items() if k != "after_state"})
    return TypedResult(action_id, status, reason[:500])


def execute_typed(session_factory, cfg: AppConfig, kube: Any, action_id: int, actor: str, *,
                  sleep=time.sleep, now: Callable[[], str] = _now, verify_timeout_s: int | None = None) -> TypedResult:
    # ---- phase 1: authorization + pre-execution re-validation (no cluster writes)
    v: Validated | None = None
    with session_factory() as s:
        act = s.get(Action, action_id)
        if not act:
            return TypedResult(action_id, "denied", "action not found")
        inc = s.get(Incident, act.incident_id)
        why = _authorized(s, act, inc)
        if why is None and not getattr(kube, "typed_actions_enabled", False):
            why = "typed action executor is not configured (observe-only client)"
        if why is None:
            try:
                ns, kind, name = act.target.split("/", 2)
                p = act.params or {}
                v = validate(cfg, kube, act.type, ns, kind, name, p.get("args") or {}, p.get("api_group"))
                if v.workload != inc.workload or v.lock_key != act.lock_key:
                    raise Denied("action workload/lock no longer matches the incident")
                if v.fingerprint != p.get("proposal_fingerprint"):
                    raise Denied(f"target changed since proposal: {p.get('proposal_fingerprint')} -> {v.fingerprint}")
            except Denied as e:
                why = str(e)
        if why is not None:
            if act.status in ("proposed", "pending_approval"):
                act.status, act.reason = "denied", why[:500]
            audit(s, actor, "action_denied", act.incident_id, action_id=act.id, stage="pre_execution", reason=why[:500])
            return TypedResult(act.id, "denied", why[:500])
    assert v is not None
    # ---- phase 1b: take the workload lock (partial unique indexes) + persist snapshot before mutating
    try:
        with session_factory() as s:
            act = s.scalar(select(Action).where(Action.id == action_id).with_for_update())
            why = _authorized(s, act, s.get(Incident, act.incident_id))
            if why is not None:
                audit(s, actor, "action_denied", act.incident_id, action_id=act.id, stage="lock", reason=why)
                return TypedResult(act.id, "denied", why)
            act.status, act.lock_key, act.before_snapshot = "executing", v.lock_key, v.snapshot
            s.flush()
            audit(s, actor, "action_executing", act.incident_id, action_id=act.id, type=act.type,
                  target=act.target, lock_key=v.lock_key, snapshot=v.snapshot)
    except IntegrityError:
        return _finish(session_factory, action_id, actor, "denied",
                       "another action is already executing on this workload", "action_denied", stage="lock")
    # ---- phase 2: single bounded write with optimistic concurrency
    try:
        applied = apply(kube, v, now=now)
    except TargetConflict as e:
        return _finish(session_factory, action_id, actor, "failed",
                       f"conflict: target changed after snapshot; no mutation applied ({e})"[:500], "action_conflict")
    except Denied as e:
        return _finish(session_factory, action_id, actor, "denied", str(e), "action_denied", stage="apply")
    except Exception as e:  # noqa: BLE001 - request may or may not have reached the API server
        return _finish(session_factory, action_id, actor, "indeterminate",
                       f"write outcome unknown ({type(e).__name__}); inspect target manually", "action_indeterminate")
    with session_factory() as s:
        act = s.get(Action, action_id)
        act.executed = True
        act.params = {**act.params, "applied": applied}
        audit(s, actor, "action_applied", act.incident_id, action_id=act.id, applied=applied)
    # ---- phase 3: post-verification, compensation on failure
    ok, state = verify(kube, cfg, v, applied, sleep=sleep, timeout_s=verify_timeout_s)
    if ok:
        return _finish(session_factory, action_id, actor, "succeeded", "post-verification passed",
                       "action_verified", after_state=state)
    with session_factory() as s:
        act = s.get(Action, action_id)
        audit(s, actor, "action_verification_failed", act.incident_id, action_id=act.id, state=state)
    if not TYPED[v.action_type].reversible:
        return _finish(session_factory, action_id, actor, "failed",
                       "post-verification failed; pod deletion is not reversible (controller recreates Pods); "
                       "handed off", "action_rollback_not_applicable", after_state=state)
    try:
        comp = compensate(kube, v, applied, now=now)
        cok, cstate = verify_compensation(kube, cfg, v, comp, sleep=sleep, timeout_s=verify_timeout_s)
    except Exception as e:  # noqa: BLE001
        return _finish(session_factory, action_id, actor, "indeterminate",
                       f"post-verification failed and compensating rollback failed ({type(e).__name__}: {e}); "
                       "target state indeterminate, inspect manually", "action_rollback_failed",
                       after_state={"verification": state})
    if cok:
        return _finish(session_factory, action_id, actor, "rolled_back",
                       "post-verification failed; compensating rollback applied and verified",
                       "action_rolled_back", after_state={"verification": state, "rollback": cstate})
    return _finish(session_factory, action_id, actor, "indeterminate",
                   "post-verification failed; compensating rollback applied but not verified; inspect manually",
                   "action_rollback_failed", after_state={"verification": state, "rollback": cstate})


# --------------------------------------------------------------------------- real executor client
class SafeActionKube:
    """Write client for the dedicated ``safe-action-executor`` identity (render-checked RBAC only).

    Every call is pinned to the target namespace and the allowlisted object names; there is no
    generic apply/patch entry point. Not constructed by the API by default.
    """
    typed_actions_enabled = True

    def __init__(self, observe: Any, core: Any, apps: Any, api_exception: type[Exception], *,
                 namespace: str = "liveprobe-target", deployments: tuple[str, ...] = ("inventory", "order"),
                 configmaps: tuple[str, ...] = ("inventory-config", "order-config")):
        self._observe, self._core, self._apps, self._exc = observe, core, apps, api_exception
        self.ns, self.deployments, self.configmaps = namespace, deployments, configmaps

    def __getattr__(self, item):  # read-only helpers are delegated to the observe client
        if item in ("get_pod", "get_deployment", "get_replicaset", "list_pods", "list_replicasets"):
            return getattr(self._observe, item)
        raise AttributeError(item)

    def _guard(self, ns: str, name: str, allowed: tuple[str, ...] | None):
        if ns != self.ns or (allowed is not None and name not in allowed):
            raise Denied("executor target not allowlisted")

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except self._exc as e:
            if getattr(e, "status", None) in (409, 422):
                raise TargetConflict(f"HTTP {getattr(e, 'status', None)}") from None
            raise

    def get_configmap(self, ns: str, name: str) -> dict | None:
        self._guard(ns, name, None)
        try:
            obj = self._core.read_namespaced_config_map(name, ns)
        except self._exc as e:
            if getattr(e, "status", None) == 404:
                return None
            raise
        return self._observe.to_dict(obj) if hasattr(self._observe, "to_dict") else obj

    def delete_pod(self, ns: str, name: str, *, uid: str, resource_version: str) -> None:
        self._guard(ns, name, None)
        from kubernetes import client
        body = client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid, resource_version=resource_version))
        self._call(self._core.delete_namespaced_pod, name, ns, body=body)

    def patch_deployment_json(self, ns: str, name: str, ops: list[dict]) -> dict:
        self._guard(ns, name, self.deployments)
        return self._call(self._apps.patch_namespaced_deployment, name, ns, ops)

    def patch_configmap_json(self, ns: str, name: str, ops: list[dict]) -> dict:
        self._guard(ns, name, self.configmaps)
        res = self._call(self._core.patch_namespaced_config_map, name, ns, ops)
        return self._observe.to_dict(res) if hasattr(self._observe, "to_dict") else res
