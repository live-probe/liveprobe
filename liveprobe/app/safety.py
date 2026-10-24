"""Safety executor: allowlist, target checks, policy, lock, rollout_undo + verification, audit."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import k8s as K
from .config import AppConfig
from .db import audit
from .models import Action, AuditLog, Incident, Setting, INCIDENT_OPEN_STATES
from . import rollback_grant

log = logging.getLogger(__name__)

POLICY_MODES = ("auto", "approval", "suggest_only")
# User decision 2026-10-05: agents act on their own up to COMPLEX; HIGH_RISK is always an operator handoff.
# For NORMAL/COMPLEX the only action is rollout_undo, proposed only when the group verified a rollback-fixable cause
# (see agent_groups.rollback_proposal). Per-incident and global overrides (approval / suggest_only) still tighten.
LEVEL_POLICY = {"SIMPLE": "auto", "NORMAL": "auto", "COMPLEX": "auto", "HIGH_RISK": "handoff"}


class Denied(Exception):
    pass


def global_mode(s: Session) -> str:
    row = s.get(Setting, "policy_mode")
    v = (row.value or {}).get("mode") if row else None
    return v if v in POLICY_MODES else "auto"


_PROFILE = "assessment"       # set once at startup from policy.resolve_profile; fail-closed default


def set_profile(name: str) -> None:
    global _PROFILE
    _PROFILE = name if name in ("assessment", "poc_autonomy") else "assessment"


def active_profile() -> str:
    return _PROFILE


def high_risk_may_execute() -> bool:
    """True only under poc_autonomy on the pinned PoC cluster (and then only through operator approval)."""
    return _PROFILE == "poc_autonomy"


def has_network_risk(incident: Incident | None) -> bool:
    """Late NetworkDrop observations are never downgraded by an earlier routing snapshot."""
    return incident is not None and 'NetworkDrop' in (incident.signal_reasons or [])


def effective_policy(level: str, mode: str) -> str:
    """Returns auto | approval | suggest_only | handoff. In `assessment` HIGH_RISK is never overridable."""
    from .policy import high_risk_policy
    over = high_risk_policy(level, mode, _PROFILE)
    if over:
        return over
    base = LEVEL_POLICY.get(level, "approval")
    if base == "handoff":
        return "handoff"
    if mode == "suggest_only":
        return "suggest_only"
    if mode == "approval":
        return "approval"
    return base  # mode auto -> level default


def check_action(cfg: AppConfig, kube: Any, action_type: str, ns: str, kind: str, name: str) -> dict:
    if action_type not in cfg.allowlist:
        raise Denied(f"action type {action_type!r} is not in the allowlist {cfg.allowlist}")
    if ns != cfg.target_namespace:
        raise Denied(f"target namespace {ns!r} is not the configured target namespace {cfg.target_namespace!r}")
    if kind != "Deployment":
        raise Denied(f"rollout_undo only supports Deployment, got {kind!r}")
    dep = kube.get_deployment(ns, name)
    if not dep:
        raise Denied(f"Deployment {ns}/{name} does not exist")
    return dep


def select_previous_revision(dep: dict, replicasets: list[dict]) -> dict | None:
    """Newest ReplicaSet revision (older than current) whose pod template differs from the current one."""
    name = dep["metadata"]["name"]
    cur_rev = K.revision_of(dep)
    cur_tpl = K.strip_template(dep["spec"]["template"])
    owned = [r for r in replicasets if K.owner_of(r, "Deployment") == name]
    candidates = sorted((r for r in owned if K.revision_of(r) < cur_rev or cur_rev == 0),
                        key=K.revision_of, reverse=True)
    for r in candidates:
        if K.strip_template(r["spec"]["template"]) != cur_tpl:
            return r
    return None


def rollout_undo(kube: Any, ns: str, name: str, *, incident_id: int | None = None,
                 action_id: int | None = None, grant_key: bytes | None = None) -> dict:
    dep = kube.get_deployment(ns, name)
    prev = select_previous_revision(dep, kube.list_replicasets(ns))
    if prev is None:
        raise Denied("no previous revision with a different pod template")
    before = {
        "revision": K.revision_of(dep),
        "template": K.sanitize_template(dep["spec"]["template"]),
        "images": K.container_images(dep["spec"]["template"]),
    }
    if incident_id is None or action_id is None:
        # Unit-test substitutes may implement the legacy two-argument method;
        # the real Kube client rejects requests without action provenance.
        res = kube.request_rollback(ns, name)
    else:
        if grant_key is None:
            raise Denied("approved rollback grant key unavailable")
        grant = rollback_grant.issue(grant_key, incident_id=incident_id, action_id=action_id,
                                     name=name, expected_revision=K.revision_of(dep),
                                     expected_source=prev["metadata"]["name"])
        res = kube.request_rollback(ns, name, grant=grant)
    if (res["to_revision"] != K.revision_of(prev) or
            res["to_replicaset"] != prev["metadata"]["name"] or
            res["before"]["revision"] != K.revision_of(dep)):
        raise Denied("rollback revision provenance differs from audited action")
    return {"before": before, "to_revision": res["to_revision"], "to_images": res["to_images"],
            "to_replicaset": res["to_replicaset"]}


def verify_rollout(kube: Any, ns: str, name: str, timeout_s: int, poll_s: float = 3.0,
                   sleep=time.sleep) -> tuple[bool, dict]:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while True:
        dep = kube.get_deployment(ns, name) or {}
        st, spec = dep.get("status") or {}, dep.get("spec") or {}
        replicas = spec.get("replicas", 1)
        sel = (spec.get("selector") or {}).get("matchLabels") or {}
        pods = [p for p in kube.list_pods(ns, sel) if not (p.get("metadata") or {}).get("deletionTimestamp")]
        waiting = sorted({r for p in pods for r in K.pod_waiting_reasons(p)})
        last = {
            "generation": (dep.get("metadata") or {}).get("generation"),
            "observed_generation": st.get("observedGeneration"),
            "replicas": replicas, "updated": st.get("updatedReplicas", 0) or 0,
            "available": st.get("availableReplicas", 0) or 0, "total": st.get("replicas", 0) or 0,
            "waiting_reasons": waiting, "images": K.container_images(spec.get("template") or {}),
            "revision": K.revision_of(dep),
        }
        ok = (last["observed_generation"] or 0) >= (last["generation"] or 0) \
            and last["updated"] == replicas and last["available"] == replicas and last["total"] == replicas \
            and not waiting
        if ok:
            return True, last
        if time.monotonic() > deadline:
            return False, last
        sleep(poll_s)


@dataclass
class ExecResult:
    action_id: int
    status: str
    reason: str | None = None


def propose(s: Session, cfg: AppConfig, incident: Incident, level: str, action_type: str, kind: str,
            name: str, actor: str = "liveprobe") -> Action:
    from .actions import incident_mode, stricter, workload_lock
    mode = global_mode(s)
    policy = 'handoff' if has_network_risk(incident) else effective_policy(level, mode)
    inc_mode = incident_mode(s, incident.id) if incident.id is not None else None
    if inc_mode:
        policy = stricter(policy, inc_mode)  # per-incident override may only tighten
    target = f"{incident.namespace}/{kind}/{name}"
    act = Action(incident_id=incident.id, type=action_type, target=target, params={"kind": kind, "name": name},
                 policy_mode=policy, status="proposed",
                 lock_key=workload_lock(incident.namespace, name) if kind == "Deployment" else None)
    s.add(act)
    s.flush()
    audit(s, actor, "action_proposed", incident.id, action_id=act.id, type=action_type, target=target,
          level=level, global_mode=mode, policy=policy)
    if policy == "handoff":
        act.status, act.reason = "denied", "HIGH_RISK policy: automatic actions are not allowed"
        audit(s, actor, "action_denied", incident.id, action_id=act.id, reason=act.reason)
    elif policy == "suggest_only":
        act.status, act.reason = "denied", "global policy suggest_only: action recorded as suggestion only"
        audit(s, actor, "action_suggested_only", incident.id, action_id=act.id)
    elif policy == "approval":
        act.status = "pending_approval"
        audit(s, actor, "action_pending_approval", incident.id, action_id=act.id)
    return act


def _authorized(s: Session, act: Action, inc: Incident | None) -> bool:
    if inc is None or has_network_risk(inc):
        return False
    if act.type != "rollout_undo" or inc.status not in INCIDENT_OPEN_STATES or inc.severity_level == "HIGH_RISK":
        return False
    from .actions import incident_mode
    if incident_mode(s, inc.id) in ("suggest_only", "handoff") or (
            act.status == "proposed" and incident_mode(s, inc.id) == "approval"):
        return False
    if act.status == "proposed":
        return act.policy_mode == "auto" and effective_policy(inc.severity_level or "COMPLEX", global_mode(s)) == "auto"
    if act.status == "pending_approval":
        return act.policy_mode == "approval" and global_mode(s) != "suggest_only" and s.scalar(
            select(AuditLog.id).where(AuditLog.event == "action_approved", AuditLog.incident_id == inc.id,
                                      AuditLog.detail["action_id"].as_integer() == act.id).limit(1)) is not None
    return False


def execute(session_factory, cfg: AppConfig, kube: Any, action_id: int, actor: str,
            level: str | None = None) -> ExecResult:
    """Run all checks, take the per-target lock (unique index), execute, verify. Separate txns per phase."""
    from . import actions as typed
    with session_factory() as s:
        act = s.get(Action, action_id)
        is_typed = bool(act) and act.type in typed.TYPED
    if is_typed:  # KAN-135: the five typed safe actions have their own validated executor
        r = typed.execute_typed(session_factory, cfg, kube, action_id, actor)
        return ExecResult(r.action_id, r.status, r.reason)
    # phase 1: checks + lock (persisted audit row before any cluster change)
    with session_factory() as s:
        act = s.get(Action, action_id)
        if not act:
            return ExecResult(action_id, "denied", "action not found")
        inc = s.get(Incident, act.incident_id)
        if not inc or not _authorized(s, act, inc):
            if act.status == "proposed":
                act.status, act.reason = "denied", "action lacks executable approval or policy"
                audit(s, actor, "action_denied", act.incident_id, action_id=act.id, reason=act.reason)
            return ExecResult(act.id, "denied", "action lacks executable approval")
        lvl = level or inc.severity_level or "COMPLEX"
        if lvl == "HIGH_RISK":
            act.status, act.reason = "denied", "HIGH_RISK incidents never execute actions"
            audit(s, actor, "action_denied", inc.id, action_id=act.id, reason=act.reason)
            return ExecResult(act.id, act.status, act.reason)
        try:
            ns, kind, name = act.target.split("/", 2)
            check_action(cfg, kube, act.type, ns, kind, name)
        except Denied as e:
            act.status, act.reason = "denied", str(e)
            audit(s, actor, "action_denied", inc.id, action_id=act.id, reason=str(e))
            return ExecResult(act.id, act.status, act.reason)
    try:
        with session_factory() as s:
            act = s.scalar(select(Action).where(Action.id == action_id).with_for_update())
            if not _authorized(s, act, s.get(Incident, act.incident_id)):
                return ExecResult(act.id, "denied", "action lacks executable approval")
            act.status = "executing"
            if act.lock_key is None:
                from .actions import workload_lock
                act.lock_key = workload_lock(*act.target.split("/", 2)[::2])
            s.flush()
            audit(s, actor, "action_executing", act.incident_id, action_id=act.id, type=act.type, target=act.target)
    except IntegrityError:
        with session_factory() as s:
            act = s.get(Action, action_id)
            act.status, act.reason = "denied", "another action is already executing on this target"
            audit(s, actor, "action_denied", act.incident_id, action_id=act.id, reason=act.reason)
            return ExecResult(act.id, act.status, act.reason)

    # phase 2: execute + verify
    ns, kind, name = act.target.split("/", 2)
    try:
        res = rollout_undo(kube, ns, name, incident_id=act.incident_id, action_id=act.id,
                           grant_key=rollback_grant.load_key())
    except Exception as e:  # noqa: BLE001
        with session_factory() as s:
            act = s.get(Action, action_id)
            act.status, act.reason = ("denied" if isinstance(e, Denied) else "failed"), f"{type(e).__name__}: {e}"[:500]
            audit(s, actor, "action_failed", act.incident_id, action_id=act.id, reason=act.reason)
            return ExecResult(act.id, act.status, act.reason)
    with session_factory() as s:
        act = s.get(Action, action_id)
        act.before_snapshot, act.executed = res["before"], True
        act.params = {**act.params, "to_revision": res["to_revision"], "to_images": res["to_images"]}
        audit(s, actor, "action_applied", act.incident_id, action_id=act.id, from_revision=res["before"]["revision"],
              from_images=res["before"]["images"], to_revision=res["to_revision"], to_images=res["to_images"])
    ok, state = verify_rollout(kube, ns, name, cfg.rollout_verify_timeout_seconds)
    with session_factory() as s:
        act = s.get(Action, action_id)
        act.after_state = state
        if ok:
            act.status, act.reason = "succeeded", "rollout complete; no waiting pods"
            audit(s, actor, "action_verified", act.incident_id, action_id=act.id, state=state)
        else:
            act.status = "failed"
            act.reason = "verification failed within timeout; broken template NOT re-applied; snapshot kept"
            audit(s, actor, "action_verification_failed", act.incident_id, action_id=act.id, state=state)
        return ExecResult(act.id, act.status, act.reason)


def pending_actions(s: Session, incident_id: int) -> list[Action]:
    return list(s.scalars(select(Action).where(Action.incident_id == incident_id,
                                               Action.status == "pending_approval")))
