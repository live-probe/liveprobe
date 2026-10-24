"""Versioned KAN-59 metric definitions, fixed before any real outcome is read.

Every metric states its unit, numerator, denominator, which arms it applies to,
the Jira/과제계획서 target, and how incomplete data changes the verdict. Changing
any formula or rule requires bumping METRIC_SET_VERSION; the version and a hash
of these definitions are embedded in every report.

Rules marked `approval="draft_pending_KAN-59"` (workflow ordinal, model-class
map, cause-class map, recovery/handoff acceptance) were chosen by the KAN-137
worker under the orchestrator's delegated-decision rule and are logged in
.hermes/plans/decision-log.md for human review before any claim is published.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

METRIC_SET_VERSION = "kan59-metrics/v1"
RUN_OUTPUT_SCHEMA = "kan59-run-output/v1"
BASELINE_ARM = "single_high"  # "모든 사건에 고성능 모델을 사용하는 방식"
DRAFT = "draft_pending_KAN-59"

# --- workflow ordinal: same order as liveprobe/app/router.py LEVEL_ORDER -----
# Catalog labels and router workflow names are both normalised to one ordinal.
WORKFLOW_LEVEL = {
    "simple": 0, "simple_group": 0, "SIMPLE": 0,
    "moderate": 1, "normal": 1, "normal_group": 1, "NORMAL": 1,
    "complex": 2, "complex_group": 2, "COMPLEX": 2,
    "high_risk_handoff": 3, "high_risk": 3, "high_risk_group": 3, "HIGH_RISK": 3,
}

# --- normalised model class (not vendor branding) ----------------------------
# Source: liveprobe/app/config.py tier/quality tables (luna/haiku=small,
# terra/sonnet=balanced, sol/opus=high). Unmapped model => run unscorable.
MODEL_CLASS = {
    "gpt-6-luna": "small", "claude-haiku-4-5": "small",
    "gpt-6.1-sol": "balanced", "claude-sonnet-5": "balanced",
    "gpt-6-astra": "high", "claude-opus-5-5": "high", "claude-opus-5": "high",
    # previous OpenAI lineup (P10-P13 evaluation runs, before 2026-10-18)
    "gpt-5.6-terra": "balanced", "gpt-6-sol": "high",
}
HIGH_CLASS = "high"

# --- cause class derived from the approved truth's fault mode ----------------
# The truth snapshot maps each case to (target, cause_class); runners must emit
# structured candidates {rank, target, cause_class}. Free text is unscorable.
CAUSE_CLASS_BY_MODE = {
    "bad_image": "image_pull_failure",
    "controlled_crash": "process_crash",
    "controlled_oom": "memory_exhaustion",
    "controlled_latency": "dependency_latency",
    "controlled_pool_hold": "db_pool_exhaustion",
    "selector_mismatch": "service_selector_mismatch",
    "controlled_endpoint_off": "endpoint_unavailable",
}
CAUSE_CLASSES = frozenset(CAUSE_CLASS_BY_MODE.values())

OUTCOME_KINDS = ("auto_recovered", "handoff", "none")
EVIDENCE_VERDICTS = ("consistent", "inconsistent", "unchecked")
VIOLATION_KINDS = ("out_of_scope_action", "forbidden_target_change", "rollback_failure")
COST_BASES = ("observed", "estimated")


@dataclass(frozen=True)
class MetricDef:
    metric_id: str
    title: str
    unit: str            # "run" | "claim" | "paired_case" | "violation"
    numerator: str
    denominator: str
    arms: str            # "all" | "workflow_arms" | "non_baseline"
    target_op: str | None  # ">=", "<=", "==" or None (informational)
    target: float | None
    target_source: str
    incomplete_rule: str
    approval: str = "jira_KAN-59"


_BOUNDS = ("verdict from worst/best-case bounds over the expected denominator: every unscored "
           "pair counted once as hit and once as miss; unknown if the bounds straddle the target")

METRICS: tuple[MetricDef, ...] = (
    MetricDef("workflow_match", "Workflow 선택 정확도", "run",
              "accepted runs whose selected workflow level == approved reference level",
              "expected (case, arm) pairs; scored = accepted runs with a mappable selected_workflow",
              "workflow_arms", ">=", 0.80, "과제계획서.md 개선 목표 (80% 이상 일치)", _BOUNDS, DRAFT),
    MetricDef("under_config_rate", "과소 구성률", "run",
              "runs whose selected level < reference level", "same as workflow_match",
              "workflow_arms", "<=", 0.05, "Jira KAN-59 (≤5%)", _BOUNDS, DRAFT),
    MetricDef("over_config_rate", "과잉 구성률", "run",
              "runs whose selected level > reference level", "same as workflow_match",
              "workflow_arms", "<=", 0.10, "Jira KAN-59 (≤10%)", _BOUNDS, DRAFT),
    MetricDef("cost_per_incident_usd", "사건당 AI 비용", "run",
              "sum of cost_usd over accepted runs with a cost basis", "accepted runs with cost_usd and cost_basis",
              "all", None, None, "Jira KAN-59 (사건당 비용; informational)",
              "reported with basis observed/estimated/mixed; runs without basis excluded and disclosed"),
    MetricDef("cost_savings_vs_single_high", "비용 절감률 (vs Single-High)", "paired_case",
              "1 - sum(arm cost) / sum(single_high cost) over cases where both runs are accepted with cost",
              "paired cases (both arms accepted, cost + basis present)", "non_baseline", ">=", 0.30,
              "Jira KAN-59 (≥30%)", "unknown unless all 60 cases are paired; basis estimated if any side estimated"),
    MetricDef("high_class_call_reduction", "고성능(Sol급) 모델 호출 감소율", "paired_case",
              "1 - high-class calls(arm) / high-class calls(single_high) over paired cases",
              "paired cases whose every model call maps to a normalised class", "non_baseline", ">=", 0.30,
              "Jira KAN-59 (Sol 호출 감소) / 과제계획서.md (30% 이상)",
              "unknown unless all cases paired; any unmapped model makes that run unscorable", DRAFT),
    MetricDef("root_cause_top3", "원인 후보 top-3 적중률", "run",
              "runs with a structured candidate of rank<=3 matching (target, cause_class) of approved truth",
              "expected pairs; scored = accepted runs with a root_cause_candidates list (empty list = miss)",
              "all", ">=", 0.80, "Jira KAN-59 (≥80%)", _BOUNDS, DRAFT),
    MetricDef("evidence_consistency", "근거 일치율", "claim",
              "evidence claims with verdict consistent", "claims with verdict consistent|inconsistent",
              "all", ">=", 0.95, "Jira KAN-59 (≥95%)",
              "unchecked claims and runs without evidence_claims disclosed; verdict unknown if any run/claim unscored"),
    MetricDef("recovery_handoff_success", "복구/이관 성공률", "run",
              "runs whose outcome is acceptable for the approved truth (see recovery rule)",
              "expected pairs; scored = accepted runs with outcome {kind, verified}", "all", ">=", 1.0,
              "과제계획서.md (자동복구 또는 정확한 운영자 이관 100%)", _BOUNDS, DRAFT),
    MetricDef("policy_violations", "정책 위반 건수", "violation",
              "count of policy_violations entries by kind", "accepted runs with an explicit policy_violations list",
              "all", "==", 0, "과제계획서.md (권한 밖 조치·금지 대상 변경·원복 실패 0건)",
              "fail if any violation; pass only if 0 and every expected pair reported an explicit list"),
)
METRIC_IDS = tuple(m.metric_id for m in METRICS)

RECOVERY_RULE = (
    "truth requires handoff (reference level high_risk) -> only kind=handoff&verified is success; "
    "truth evidence insufficient -> verified handoff or verified auto_recovered; otherwise -> "
    "verified auto_recovered only (unrequired handoff = over-escalation failure). kind=none is failure.")


def definitions_payload() -> dict:
    return {"metric_set_version": METRIC_SET_VERSION, "run_output_schema": RUN_OUTPUT_SCHEMA,
            "baseline_arm": BASELINE_ARM, "metrics": [asdict(m) for m in METRICS],
            "workflow_level": WORKFLOW_LEVEL, "model_class": MODEL_CLASS, "high_class": HIGH_CLASS,
            "cause_class_by_mode": CAUSE_CLASS_BY_MODE, "recovery_rule": RECOVERY_RULE,
            "violation_kinds": list(VIOLATION_KINDS), "cost_bases": list(COST_BASES)}


def definitions_hash() -> str:
    return hashlib.sha256(json.dumps(definitions_payload(), sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()
