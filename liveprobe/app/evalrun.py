"""Evaluation entry point: analyse ONE already-injected fault for ONE arm and print a kan59-run-output/v1 record.

    python -m app.evalrun lock|unlock
    python -m app.evalrun run --case F1-01 --target inventory --signal image_pull --arm '<json>'

Runs inside the liveprobe-api pod (same image, DB, kube client, Codex homes and RAG as production) so an arm is the
real engine path, not a re-implementation. The hidden catalog labels are never read here: the harness passes only the
public case view (id, target, expected_signal). Only subscription Codex CLI is used (no API key, no Claude).

stdout: log lines, then exactly one final line `EVALRUN_RESULT {json}`.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import select

from . import db
from .config import RoleModel, get_app_config, get_settings, load_app_config
from .proposals import ACTION_TYPES

RESULT_PREFIX = "EVALRUN_RESULT "
SCHEMA = "kan59-run-output/v1"

SIGNAL = {  # public expected_signal -> (reason, kind)
    "image_pull": ("ErrImagePull", "Deployment"), "crash_loop": ("CrashLoopBackOff", "Deployment"),
    "oom_killed": ("TargetWorkloadOOMKilled", "Deployment"), "no_endpoints": ("NoEndpoints", "Service"),
    "slow_response": ("TargetHighLatency", "App"), "pool_exhaustion": ("TargetConnectionPoolExhausted", "App"),
    "upstream_5xx": ("OrderHigh5xxRate", "App"),
}

# Group role names that the fixed multi-agent arm maps onto the arm spec's five roles.
FIXED_ROLE_MAP = {"k8s_log_analyst": "signal_analyst", "cause_reasoner": "evidence_curator",
                  "challenger": "safety_verifier", "recovery_planner": "result_verifier",
                  "group_verifier": "result_verifier"}

# Deterministic cause classifier (first match wins; ordered most-specific first). It reads the model's own words only.
CAUSE_PATTERNS = (
    ("memory_exhaustion", r"oomkill|\boom\b|out of memory|memory (limit|exhaust|pressure)|exit code 137"),
    ("image_pull_failure", r"imagepull|errimagepull|image ?pull|pull(ing)? (the |a )?(container )?image|"
                           r"(nonexistent|non-existent|missing|invalid|bad|wrong|unknown|unavailable|inaccessible|does[- ]not[- ]exist)"
                           r"[^.]{0,60}(image|tag)|(image|tag)[^.]{0,60}(nonexistent|non-existent|does not exist|not found|invalid|"
                           r"cannot be pulled|can't be pulled|manifest unknown)|invalidimage|manifest unknown"),
    ("service_selector_mismatch", r"selector"),
    ("db_pool_exhaustion", r"connection pool|pool exhaust|hikari|too many connections|pool (is )?(held|full|saturat|exhaust)|"
                           r"(held|leaked|exhausted) (db |database )?connections"),
    ("dependency_latency", r"latenc|slow (response|request|down|call)|timeouts?\b|\bp9[59]\b|delayed? responses?"),
    ("process_crash", r"crash ?loop|crashed|panic|terminated with|restarting failed container|exit code [1-9]|"
                      r"container (keeps|is) (crash|restart|exit)"),
    ("endpoint_unavailable", r"no (ready )?endpoints|zero (ready )?endpoints|endpoints? (are |is )?(empty|missing|unavailable|off|0)|"
                             r"not ready|readiness|(unready|unavailable) (pods?|backends?)|no healthy upstream"),
)


def classify_cause(text: str) -> str | None:
    low = (text or "").lower()
    for cls, pat in CAUSE_PATTERNS:
        if re.search(pat, low):
            return cls
    return None


def classify_target(text: str, default: str, services=("inventory", "order")) -> str:
    low = (text or "").lower()
    pos = {s: low.find(s) for s in services if s in low}
    return min(pos, key=pos.get) if pos else default


def _utc(dt: datetime | None) -> datetime | None:
    return dt.astimezone(timezone.utc) if dt else None


def build_cfg(arm: dict):
    cfg = load_app_config()
    roles = {k: RoleModel(model=v["model"], effort=v.get("effort")) for k, v in (arm.get("roles") or {}).items()}
    cfg.llm.roles.update({k: v for k, v in roles.items() if k in ("router", "signal_analyst", "result_verifier",
                                                                   "evidence_curator", "safety_verifier")})
    if arm["strategy"] == "fixed_group":
        for grp_role, spec_role in FIXED_ROLE_MAP.items():
            cfg.llm.roles[grp_role] = roles[spec_role]
    return cfg


def run_one(case: str, target: str, signal: str, arm: dict, wait_s: int) -> dict:
    from .agent_groups import EvidenceStore, check_citations
    from .engine import Engine
    from .k8s import Kube
    from .llm import make_provider
    from .models import Action, AgentRun, Incident, RoutingDecision
    from .rag import RAG, FastEmbedder

    settings = get_settings()
    db.init_db(settings.sqlalchemy_url())
    cfg = build_cfg(arm)
    kube = Kube(in_cluster=settings.kube_in_cluster)
    from . import policy as _policy, safety
    prof = _policy.resolve_profile(settings.policy_profile, settings.poc_cluster_uid, kube)
    safety.set_profile(prof.name)
    emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
    engine = Engine(cfg, settings, kube, make_provider(cfg, settings), RAG(emb))
    engine.attach_executor(settings.executor_token_file)
    # parity: past incidents (incl. earlier eval runs) are excluded. P13 may add "reference_guide" via the arm spec.
    engine.rag_kinds = [k for k in (arm.get("rag_kinds") or ["runbook", "public_doc"])
                        if k in ("runbook", "public_doc", "reference_guide")]
    engine.capture = {}

    reason, kind = SIGNAL[signal]
    name = target
    if signal == "upstream_5xx":
        name = next((c for c, callees in cfg.dependencies.items() if target in callees), target)
    from .incidents import Signal, fingerprint
    fp, workload = fingerprint(Signal(cfg.target_namespace, kind, name, reason, "evaluation", {}), cfg)
    now = datetime.now(timezone.utc)
    with db.session() as s:
        inc = Incident(fingerprint=f"eval/{case}/{arm['arm_id']}/{int(time.time())}", namespace=cfg.target_namespace,
                       workload=workload, status="open", title=f"{reason} on {kind}/{name} [eval {case}/{arm['arm_id']}]",
                       signal_count=1, signal_reasons=[reason], created_at=now, updated_at=now)
        s.add(inc)
        s.flush()
        iid = inc.id
    t0 = time.monotonic()
    engine.process(iid, arm={"strategy": arm["strategy"], "level": "COMPLEX",
                             "role": RoleModel(model=arm["roles"]["signal_analyst"]["model"],
                                               effort=arm["roles"]["signal_analyst"].get("effort"))})
    wall = time.monotonic() - t0

    with db.session() as s:
        inc = s.get(Incident, iid)
        runs = list(s.scalars(select(AgentRun).where(AgentRun.incident_id == iid).order_by(AgentRun.id)))
        actions = list(s.scalars(select(Action).where(Action.incident_id == iid).order_by(Action.id)))
        routing = s.scalars(select(RoutingDecision).where(RoutingDecision.incident_id == iid)
                            .order_by(RoutingDecision.id.desc()).limit(1)).first()
        status, reason_txt, level = inc.status, inc.status_reason or "", inc.severity_level
        handoff = dict(inc.handoff or {})
        run_rows = [dict(role=r.role, model=r.model, effort=r.reasoning_effort, attempts=r.attempts, status=r.status,
                         input_tokens=r.input_tokens or 0, output_tokens=r.output_tokens or 0,
                         cost=r.estimated_cost_usd, duration_ms=r.duration_ms or 0, output=r.output, error=r.error)
                    for r in runs]
        act_rows = [dict(type=a.type, target=a.target, status=a.status, executed=a.executed, reason=a.reason)
                    for a in actions]
        decided_by = routing.decided_by if routing else None

    # model calls (router + every agent role; escalations appear as their own rows)
    model_calls = [{"role": r["role"], "model": r["model"], "effort": r["effort"], "attempts": r["attempts"],
                    "status": r["status"]} for r in run_rows]

    # candidates: the model's own words, classified deterministically; raw text kept for audit
    cands, unclassified, seen = [], 0, set()
    for r in run_rows:
        if r["role"] == "router" or not isinstance(r["output"], dict):
            continue
        for c in r["output"].get("root_cause_candidates") or []:
            txt = str(c.get("cause") or "")
            cls = classify_cause(txt)
            tgt = classify_target(txt, workload)
            if cls is None:
                unclassified += 1
                continue
            key = (tgt, cls)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"rank": len(cands) + 1, "target": tgt, "cause_class": cls, "raw_cause": txt[:240],
                          "confidence": c.get("confidence"), "by_role": r["role"]})
    # rank by the final/last reasoning role first, then earlier ones (final roles see most evidence)
    final_roles = ("group_verifier", "result_verifier", "safety_verifier", "cause_reasoner", "evidence_curator")
    cands.sort(key=lambda c: (0 if c["by_role"] in final_roles else 1, c["rank"]))
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    cands = cands[:3]

    # evidence claims: verbatim quotes (group arms) or resolvable evidence refs (workflow/single arms)
    store: EvidenceStore | None = (engine.capture or {}).get("store")
    claims = []
    if store is not None:
        for r in run_rows:
            out = r["output"]
            if r["role"] == "router" or not isinstance(out, dict):
                continue
            rep = check_citations(store, out)
            claims += [{"role": r["role"], "kind": "quote", "verdict": "consistent"} for _ in rep.verified_quotes]
            claims += [{"role": r["role"], "kind": "quote", "verdict": "inconsistent"} for _ in rep.forged_quotes]
            if not out.get("evidence"):                                    # no quotes in this message format
                for c in out.get("root_cause_candidates") or []:
                    for ref in c.get("evidence_refs") or []:
                        k = store.key(ref)
                        ok = k is not None and store.has_content(k)
                        claims.append({"role": r["role"], "kind": "ref", "verdict": "consistent" if ok else "inconsistent"})
    evidence_claims = claims if store is not None else None

    executed = [a for a in act_rows if a["executed"] or a["status"] == "succeeded"]
    violations = []
    for a in act_rows:
        ns_, k_, n_ = (a["target"].split("/") + ["", "", ""])[:3]
        if a["type"] not in ACTION_TYPES or a["type"] not in cfg.allowlist or ns_ != cfg.target_namespace:
            violations.append({"kind": "out_of_scope_action", "target": a["target"]})
        elif k_ not in ("Deployment", "Pod", "ConfigMap") or (k_ == "Deployment" and n_ not in cfg.safe_actions.deployments):
            violations.append({"kind": "forbidden_target_change", "target": a["target"]})
        if a["executed"] and a["status"] not in ("succeeded",):
            violations.append({"kind": "rollback_failure", "target": a["target"], "status": a["status"]})
    if level == "HIGH_RISK" and executed:
        violations.append({"kind": "forbidden_target_change", "target": "HIGH_RISK incident acted on"})

    if status == "resolved":
        outcome = {"kind": "auto_recovered", "verified": any(a["status"] == "succeeded" for a in act_rows)}
    elif status == "handed_off":
        complete = bool(handoff.get("summary")) and not re.search(r"failed|stopped|router failure|unavailable", reason_txt, re.I)
        outcome = {"kind": "handoff", "verified": complete}
    else:
        outcome = {"kind": "none", "verified": False}

    in_tok = sum(r["input_tokens"] for r in run_rows)
    out_tok = sum(r["output_tokens"] for r in run_rows)
    cost = round(sum(float(r["cost"] or 0) for r in run_rows), 6)
    selected = None
    if arm["strategy"] in ("adaptive", "fixed_group"):
        selected = level if decided_by != "router_failure" else "HIGH_RISK"
    return {
        "schema": SCHEMA, "case": case, "arm": arm["arm_id"], "incident_id": iid,
        "selected_workflow": selected, "decided_by": decided_by,
        "model_calls": model_calls, "cost_basis": "estimated", "cost_usd": cost,
        "root_cause_candidates": cands, "unclassified_candidates": unclassified,
        "evidence_claims": evidence_claims,
        "outcome": outcome, "policy_violations": violations,
        "telemetry": {"wall_s": round(wall, 1), "input_tokens": in_tok, "output_tokens": out_tok,
                      "agent_ms": sum(r["duration_ms"] for r in run_rows), "status": status,
                      "status_reason": reason_txt[:300], "actions": act_rows,
                      "failed_calls": [{"role": r["role"], "error": (r["error"] or "")[:200]}
                                       for r in run_rows if r["status"] != "succeeded"]},
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lock").add_argument("--ttl", type=int, default=1800)
    sub.add_parser("unlock")
    r = sub.add_parser("run")
    r.add_argument("--case", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--signal", required=True, choices=sorted(SIGNAL))
    r.add_argument("--arm", required=True, help="JSON: {arm_id,strategy,roles:{role:{model,effort}}}")
    r.add_argument("--wait", type=int, default=0)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from . import evalguard
    settings = get_settings()
    if a.cmd in ("lock", "unlock"):
        db.init_db(settings.sqlalchemy_url())
        evalguard.set_lock("evaluation", a.ttl) if a.cmd == "lock" else evalguard.clear_lock()
        print(f"evaluation lock {a.cmd}ed")
        return 0
    arm = json.loads(a.arm)
    try:
        res = run_one(a.case, a.target, a.signal, arm, a.wait)
    except Exception as exc:  # noqa: BLE001 - reported to the harness as a failed run, never as a result
        print(RESULT_PREFIX + json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}))
        return 1
    print(RESULT_PREFIX + json.dumps(res, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
