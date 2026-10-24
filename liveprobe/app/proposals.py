"""Agent action proposals (policy C, 2026-10-05).

Rules (user decision): the agent reads logs/evidence, finds the cause and may act on KUBERNETES resources itself for every
level except HIGH_RISK (always an operator handoff). It cannot fix application bugs (a Spring exception, a bad query):
those stay handoffs. The agent only PROPOSES; nothing here touches the cluster. A proposal runs only if

  1. an independent verifier supported the cause,
  2. the type is allowlisted and its parameters validate (`actions.validate` / `safety.check_action`: bounded replicas
     and memory, ownership labels, target namespace, workload == incident workload, no-ops refused),
  3. the policy for the incident level is `auto` (global approval / suggest_only and per-incident modes still tighten),
  4. it runs through the existing executor with before-snapshot, verification and compensation.
"""
from __future__ import annotations

from typing import Any

# type -> (kind, params it needs)
CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "rollout_undo": ("Deployment", ()),
    "rollout_restart": ("Deployment", ()),
    "pod_delete": ("Pod", ()),
    "scale_replicas": ("Deployment", ("replicas",)),
    "set_memory_limit": ("Deployment", ("memory_limit_mi",)),
    "configmap_restore": ("ConfigMap", ("version",)),
}
ACTION_TYPES = tuple(CATALOG)

from .remediation import REMEDIATION_RULES

ACTION_RULES = (
    "Kubernetes actions you may propose (at most one, only when it fixes the VERIFIED cause; otherwise hand off): "
    "rollout_undo (previous revision; for a bad image/config rollout), rollout_restart (restart pods; for a stuck or "
    "corrupted process state), pod_delete (target = the exact Pod name from the evidence), scale_replicas (replicas 1..2), "
    "set_memory_limit (memory_limit_mi 256..768; for OOMKilled), configmap_restore (version = an approved ConfigMap version). "
    "Set next_action='act' and fill `action` {type, target, replicas|memory_limit_mi|version, why}. `target` is the "
    "Deployment name (Pod name for pod_delete, ConfigMap name for configmap_restore). "
    "Do NOT propose an action for application-code or data problems that a restart cannot relieve (startup exceptions that "
    "crash every new process, bad queries, failing dependencies outside Kubernetes) or when evidence is insufficient: use "
    "next_action='handoff' and explain. "
    "DO propose a mitigation when the root cause is in the application but the damage is held in the RUNNING process and a "
    "fresh process clears it (e.g. a JDBC/HTTP connection pool exhausted or leaked, threads wedged, a fault that started "
    "after the process was already healthy): rollout_restart (or pod_delete for one bad pod) restores service; state in "
    "`why` that it is a mitigation and the application root cause still needs a developer. "
    "A bounded, reversible change IS the right response to a verified resource/process cause even when the application-level "
    "reason is unknown (e.g. OOMKilled at the configured limit -> raise the limit within bounds; wedged process -> restart). "
    "Reviewers judge whether the evidence supports the cause and whether the SINGLE proposed action fixes it within bounds; "
    "they do not demand more investigation before a reversible change, and they must not widen the action."
)


def bounds_text(cfg: Any) -> str:
    """Approved limits, stated to the agent so it proposes only values the executor will accept."""
    p = cfg.safe_actions
    return (f"Approved bounds: replicas {p.replicas_min}..{p.replicas_max}; memory limit {p.memory_limit_min_mi}..{p.memory_limit_max_mi}Mi "
            f"(never below the memory request); only managed Deployments in {cfg.target_namespaces}, with a non-empty matching system label. Current limits/requests are in the "
            f"Deployment evidence under `resources`. For OOMKilled, raise the limit to the smallest allowed value that "
            f"clearly exceeds the current one (no more than 2x); if the limit is already at the maximum, hand off.")


def normalize(raw: Any, workload: str | None = None) -> dict | None:
    """Agent `action` object -> {type, kind, name, params} or None when malformed/unknown."""
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t not in CATALOG:
        return None
    kind, needs = CATALOG[t]
    name = raw.get("target") or (workload if kind == "Deployment" else None)
    if not isinstance(name, str) or not name:
        return None
    params: dict[str, Any] = {}
    for k in needs:
        v = raw.get(k)
        if v is None or isinstance(v, bool):
            return None
        params[k] = v
    return {"type": t, "kind": kind, "name": name, "params": params, "why": str(raw.get("why") or "")[:300]}


def resolve(out: dict | None, workload: str | None = None) -> dict | None:
    """The action an agent message asks for: legacy next_action='rollout_undo' or next_action='act' + `action`."""
    if not isinstance(out, dict):
        return None
    na = out.get("next_action")
    if na == "rollout_undo":
        return normalize({"type": "rollout_undo", "target": workload}, workload)
    if na == "act":
        return normalize(out.get("action"), workload)
    return None


def same(a: dict | None, b: dict | None) -> bool:
    return bool(a and b) and all(a[k] == b[k] for k in ("type", "kind", "name", "params"))


def group_action(msgs: dict, primary: dict | None, verified: bool, features: dict, workload: str | None,
                 allowlist: list[str]) -> dict:
    """Decision for a NORMAL/COMPLEX group: act only when verified and the proposal passes cheap pre-checks."""
    if not verified:
        return {"propose": False, "why": "not independently verified"}
    cand = resolve((msgs or {}).get("recovery_planner"), workload)
    if cand is None:
        for role in ("coordinator", "k8s_analyst", "cause_reasoner"):
            cand = resolve((msgs or {}).get(role), workload)
            if cand:
                break
    cand = cand or resolve(primary, workload)
    gv = (msgs or {}).get("group_verifier")
    if cand and gv:
        v = resolve(gv, workload)
        if v is not None and not same(cand, v):
            return {"propose": False, "why": "verifier proposed a different action than the proposer"}
        if v is None and gv.get("next_action") == "handoff":
            return {"propose": False, "why": "verifier did not support the proposed action"}
    if cand is None:
        return {"propose": False, "why": "agents proposed no Kubernetes action (handoff / not fixable in Kubernetes)"}
    return precheck(cand, features, workload, allowlist)


def precheck(cand: dict, features: dict, workload: str | None, allowlist: list[str]) -> dict:
    if cand["type"] not in allowlist:
        return {"propose": False, "why": f"action {cand['type']} is not allowlisted"}
    if cand["type"] == "rollout_undo" and not features.get("has_previous_revision"):
        return {"propose": False, "why": "no previous revision with a different template to roll back to"}
    if cand["kind"] == "Deployment" and workload and cand["name"] != workload:
        return {"propose": False, "why": f"action targets {cand['name']}, not the incident workload {workload}"}
    return {"propose": True, "why": cand.get("why") or "verified cause fixable in Kubernetes", **cand}
