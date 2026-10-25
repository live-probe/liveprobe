"""Router: deterministic rules first, optional LLM classifier second (can only raise the level)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import AppConfig
from .evidence import Bundle
from .k8s import parse_ts
from .remediation import remediation_hint  # public deterministic fallback

LEVEL_ORDER = {"SIMPLE": 0, "NORMAL": 1, "COMPLEX": 2, "HIGH_RISK": 3}
WORKFLOW = {"SIMPLE": "simple_group", "NORMAL": "normal_group", "COMPLEX": "complex_group", "HIGH_RISK": "high_risk_group"}
PULL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}
CRASH_REASONS = {"CrashLoopBackOff"}


@dataclass
class Decision:
    level: str
    workflow: str
    features: dict[str, Any]
    rationale: str
    confidence: float | None
    decided_by: str
    llm: dict | None = None
    policy: dict | None = None


def extract_features(b: Bundle, recent_minutes: int = 15, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    reasons = sorted({c["waiting_reason"] for p in b.pods for c in p["containers"] if c.get("waiting_reason")})
    event_reasons = sorted({str(e.get("reason")) for e in b.events if e.get("reason")})
    evidence_sources = sorted({i.source for i in b.items if not i.error and i.payload is not None})
    dep = b.deployment or {}
    svc = b.service
    created = parse_ts(dep.get("recent_rollout_at") or dep.get("current_revision_created"))
    age_min = (now - created).total_seconds() / 60 if created else None
    revs = dep.get("revisions") or []
    cur = dep.get("current_revision")
    cur_images = dep.get("images") or []
    prev_diff = [r for r in revs if r["revision"] != cur and r["images"] != cur_images]
    selector_mismatch = bool(
        svc and svc.get("selector") and not svc.get("matching_pods") and svc.get("pods_labeled_app_eq_service"))
    no_endpoints = bool(svc is not None and svc.get("ready_endpoints", 0) == 0)
    callers_failing = [c["name"] for c in b.callers if (c.get("http_5xx_rate_per_s") or 0) > 0]
    caller_errors_in_logs = [c["name"] for c in b.callers if any(l.startswith(f"[{c['name']}]") for l in b.logs)]
    ready_pods = [p["name"] for p in b.pods if all(c.get("ready") for c in p["containers"]) and p["containers"]]
    return {
        "waiting_reasons": reasons,
        "image_pull_failing": bool(set(reasons) & PULL_REASONS),
        "crashloop": bool(set(reasons) & CRASH_REASONS),
        # Error logs can outlive a transient startup 5xx; count current
        # metrics-based caller failures for the SIMPLE blast-radius rule.
        "affected_workload_count": 1 + len(set(callers_failing)),
        "recent_rollout": age_min is not None and age_min <= recent_minutes,
        "current_revision_age_min": round(age_min, 1) if age_min is not None else None,
        "current_revision": cur,
        "has_previous_revision": bool(prev_diff),
        "service_selector_matches_zero_pods": selector_mismatch,
        "service_has_no_endpoints": no_endpoints,
        # Zero endpoints because the workload's own pods are not ready (crash, OOM, pull failure, restarting) is a workload
        # problem, not a routing change. A routing fault is: selector matches nothing although labelled pods exist.
        "endpoints_empty_because_workload_down": bool(
            no_endpoints and not selector_mismatch and (not ready_pods)
            and (set(reasons) & (CRASH_REASONS | PULL_REASONS | {"OOMKilled", "ContainerCreating", "CreateContainerConfigError"})
                 or any(c.get("last_terminated_reason") == "OOMKilled" or (c.get("restart_count") or 0) > 0
                        for p in b.pods for c in p["containers"]))),
        "ready_pods": len(ready_pods),
        "dependent_callers_failing": sorted(set(callers_failing) | set(caller_errors_in_logs)),
        "evidence_errors": sorted({i.source for i in b.items if i.error}),
        "evidence_sources": evidence_sources,
        "evidence_source_count": len(evidence_sources),
        "event_reasons": event_reasons,
        "network_drop_signal": "NetworkDrop" in event_reasons,
        "oom_killed": "OOMKilled" in reasons or "OOMKilling" in event_reasons or "OOMKilled" in event_reasons,
        "latency_signal": bool({"HighLatency", "SlowResponse", "PoolExhausted", "PoolHold"} & set(event_reasons))
            or any("latency" in str(k).lower() or "pool" in str(k).lower() for k in b.metrics),
        "protected_resource": any(i.target and any(
                                      f"/{kind}/" in i.target and
                                      not (kind == "Node" and i.source == "node" and i.supporting)
                                      for kind in ("Node", "Secret", "Namespace"))
                                  for i in b.items),
    }


def rules_decide(f: dict[str, Any]) -> tuple[str, str]:
    if f.get("network_drop_signal") or "NetworkDrop" in f.get("signal_reasons", []):
        return "HIGH_RISK", ("NetworkDrop: observed dropped/error traffic on a workload path; "
                             "operator handoff, no automatic action (non-overridable).")
    routing_fault = f["service_has_no_endpoints"] and f["dependent_callers_failing"] and not f.get("endpoints_empty_because_workload_down")
    if f["protected_resource"] or f["service_selector_matches_zero_pods"] or routing_fault:
        why = []
        if f["protected_resource"]:
            why.append("protected resource implicated")
        if f["service_selector_matches_zero_pods"]:
            why.append("Service selector matches 0 pods while pods for the workload exist")
        if routing_fault:
            why.append("Service has 0 ready endpoints while its pods are not the cause")
        if f["dependent_callers_failing"]:
            why.append(f"dependent callers failing: {', '.join(f['dependent_callers_failing'])}")
        return "HIGH_RISK", "; ".join(why) + " -> traffic-routing change with cross-service blast radius (non-overridable)."
    if (f["image_pull_failing"] or f["crashloop"]) and f["recent_rollout"] and (
        f["affected_workload_count"] == 1 or (f["ready_pods"] > 0 and not f["service_has_no_endpoints"])
    ):
        return "SIMPLE", (f"Deployment with {', '.join(f['waiting_reasons'])} after a rollout "
                          f"{f['current_revision_age_min']} min ago (revision {f['current_revision']}).")
    if f["oom_killed"] or f["latency_signal"]:
        return "NORMAL", "bounded OOM/latency signal; requires multi-source analysis without automatic action."
    return "COMPLEX", "no deterministic SIMPLE/NORMAL/HIGH_RISK pattern matched."


ROUTER_PROMPT = """Classify the incident severity for automated remediation routing.
Levels: SIMPLE (single workload, clear recent change), NORMAL (bounded OOM/latency),
COMPLEX (unclear/multi-cause), HIGH_RISK (traffic routing / protected resource / irreversible risk).
Rule engine features:
{features}
Evidence summary:
{evidence}
Rule engine proposal: {rule_level} ({rule_why})
"""


def score_models(level: str, cfg: AppConfig) -> dict[str, Any]:
    """Hard safety gate, then relative quality/cost/throughput; never guess missing prices."""
    p = cfg.router
    anthropic = cfg.llm.provider in ("claude_cli", "anthropic_api")
    candidates = []
    for model in sorted(p.model_quality):
        if model.startswith("claude") != anthropic:
            continue
        price = cfg.llm.prices.get(model)
        quality, throughput = p.model_quality[model], p.model_throughput.get(model)
        if not price or throughput is None or "input" not in price or "output" not in price:
            candidates.append({"model": model, "eligible": False, "reason": "unknown price or throughput"})
            continue
        if quality < p.min_quality[level]:
            candidates.append({"model": model, "eligible": False, "reason": "below quality floor"})
            continue
        # Comparable estimate: 1000 input + 500 output tokens; not an actual bill.
        cost = (price["input"] * 1000 + price["output"] * 500) / 1e6
        candidates.append({"model": model, "eligible": True, "quality": quality,
                           "throughput": throughput, "estimated_cost_usd": round(cost, 6)})
    eligible = [c for c in candidates if c["eligible"]]
    if not eligible:
        raise ValueError("no eligible priced model for level")
    max_cost = max(c["estimated_cost_usd"] for c in eligible)
    min_cost = min(c["estimated_cost_usd"] for c in eligible)
    # v2: the cost term is on a log scale between the cheapest and the dearest eligible model. On a linear scale one
    # very expensive candidate (gpt-6-astra is 5x gpt-6.1-sol) made every other model look free, so a simple incident
    # picked the mid tier over the low tier. For the previous OpenAI lineup every v1 selection is unchanged; for the
    # Claude map SIMPLE moves sonnet -> haiku (the low tier, as the plan intends for simple incidents).
    span = math.log(max_cost / min_cost) if min_cost > 0 and max_cost > min_cost else 0.0
    for c in eligible:
        cost_term = 1 - math.log(c["estimated_cost_usd"] / min_cost) / span if span else 1.0
        c["score"] = round(p.quality_weight * c["quality"] +
                           p.cost_weight * cost_term +
                           p.throughput_weight * c["throughput"], 6)
    selected = sorted(eligible, key=lambda c: (-c["score"], c["model"]))[0]
    return {"version": p.version, "weights": {"quality": p.quality_weight, "cost": p.cost_weight,
            "throughput": p.throughput_weight}, "price_version": cfg.llm.prices_version,
            "candidate_scores": candidates, "selected_model": selected["model"],
            "selected_reason": "highest eligible weighted score; lexical tie-break",
            "workflow": WORKFLOW[level], "price_basis_tokens": {"input": 1000, "output": 500}}


def _handoff(f: dict, reason: str, cfg: AppConfig) -> Decision:
    return Decision("HIGH_RISK", WORKFLOW["HIGH_RISK"], f, reason, None, "router_failure",
                    policy={"version": cfg.router.version, "handoff_only": True, "selected_reason": reason})


def decide(b: Bundle, recent_minutes: int, llm_classify: Callable[[str], dict] | None = None,
           cfg: AppConfig | None = None, signal_reasons: list[str] | None = None) -> Decision:
    cfg = cfg or AppConfig()
    try:
        f = extract_features(b, recent_minutes)
        f["signal_reasons"] = sorted(set(signal_reasons or []))
        f["network_drop_signal"] = f.get("network_drop_signal", False) or "NetworkDrop" in f["signal_reasons"]
        f["oom_killed"] |= any("oom" in s.lower() for s in f["signal_reasons"])
        f["latency_signal"] |= any("latency" in s.lower() or "slow" in s.lower() or "pool" in s.lower()
                                   for s in f["signal_reasons"])
        level, why = rules_decide(f)
    except Exception as e:  # malformed evidence must not become an automatic action
        return _handoff({"evidence_error": type(e).__name__}, "feature extraction failed; operator handoff", cfg)
    if b.items and (set(f["evidence_errors"]) & {"deployment", "pod_status", "service", "endpoints"}):
        return _handoff(f, "critical evidence unavailable; operator handoff", cfg)
    if b.items and (b.deployment is None or not b.pods):
        return _handoff(f, "deployment/pod evidence missing; operator handoff", cfg)
    if not b.items and b.deployment is None and not b.pods and b.service is None:
        return _handoff(f, "no routing evidence; operator handoff", cfg)
    out = None
    if llm_classify is not None:
        from .evidence import compact_summary
        prompt = ROUTER_PROMPT.format(features=json.dumps(f, default=str),
                                      evidence=json.dumps(compact_summary(b), default=str)[:6000],
                                      rule_level=level, rule_why=why)
        try:
            out = llm_classify(prompt)
            if not isinstance(out, dict) or out.get("level") not in LEVEL_ORDER or not isinstance(out.get("rationale"), str) \
                    or not out["rationale"].strip() or isinstance(out.get("confidence"), bool) or not isinstance(out.get("confidence"), (int, float)) \
                    or not 0 <= out["confidence"] <= 1:
                raise ValueError("invalid classifier result")
        except Exception as e:  # noqa: BLE001
            return _handoff(f, f"LLM classifier unavailable ({type(e).__name__}); operator handoff", cfg)
        if out["confidence"] < cfg.min_confidence:
            return _handoff(f, "LLM classifier confidence below threshold; operator handoff", cfg)
        if level == "HIGH_RISK":
            why += (f" LLM agrees: {out['rationale'][:300]}" if out["level"] == level else
                    f" LLM suggested {out['level']}; rules win for HIGH_RISK.")
        elif out["level"] == "HIGH_RISK":
            # Policy C: only the deterministic rules may declare HIGH_RISK (protected resources, routing faults). The LLM may
            # still raise up to COMPLEX for deeper analysis, but it cannot take away automatic handling by itself.
            if level != "COMPLEX":
                why += f" LLM raised level {level}->COMPLEX (HIGH_RISK is reserved for the rules): {out['rationale'][:300]}"
                level = "COMPLEX"
            else:
                why += f" LLM suggested HIGH_RISK; rules keep COMPLEX: {out['rationale'][:300]}"
        elif LEVEL_ORDER[out["level"]] > LEVEL_ORDER[level]:
            why += f" LLM raised level {level}->{out['level']}: {out['rationale'][:300]}"
            level = out["level"]
        elif LEVEL_ORDER[out["level"]] < LEVEL_ORDER[level]:
            why += f" LLM suggested lower level {out['level']}; conservative rule level kept."
        else:
            why += f" LLM agrees: {out['rationale'][:300]}"
    try:
        policy = score_models(level, cfg)
    except (ValueError, KeyError, TypeError) as e:
        return _handoff(f, f"model policy unavailable ({type(e).__name__}); operator handoff", cfg)
    return Decision(level, WORKFLOW[level], f, why, out["confidence"] if out else 1.0,
                    "rules+llm" if out else "rules", llm=out, policy=policy)

