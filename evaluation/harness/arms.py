"""The four comparison arms, frozen before any result exists.

Model/effort choices mirror the role vocabulary of 과제계획서.md (Luna/Terra/Sol)
and liveprobe/app/config.py. Approved 2026-10-05 under the user's delegation (decision D3,
docs/plans/revamp/P10-evaluation-protocol.md); the three models were each called once on the
subscription before approval. `max_run_cost_usd` is a generous reservation only: cost is recorded
(estimated from token usage), never used as a spending limit (D4).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math

APPROVAL = "approved_2026-10-05_user_delegated"   # D3 in docs/plans/revamp/P10-evaluation-protocol.md
ROLES = ("router", "signal_analyst", "result_verifier", "evidence_curator", "safety_verifier")


@dataclass(frozen=True)
class RoleModel:
    model: str
    effort: str | None


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    label: str
    # "single": one model does the whole analysis; "fixed_group": the same Agents
    # Group for every case; "adaptive": the LiveProbe router picks the group.
    strategy: str
    fixed_workflow: str | None
    roles: dict[str, RoleModel] = field(default_factory=dict)
    # Worst-case USD a single run of this arm may spend; the budget reserves it
    # before the run starts so the ceiling can never be crossed mid-run.
    max_run_cost_usd: float = 0.0
    prompt_version: str = "v1"
    approval: str = APPROVAL
    # RAG source kinds the run may retrieve. None = engine default (runbook + public_doc). P13 only.
    rag_kinds: tuple[str, ...] | None = None

    def spec_hash(self) -> str:
        d = asdict(self)
        if d.get("rag_kinds") is None:       # keep the hashes of the four original arms unchanged
            d.pop("rag_kinds", None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def _all(model: str, effort: str | None) -> dict[str, RoleModel]:
    return {role: RoleModel(model, effort) for role in ROLES}


ARMS: tuple[ArmSpec, ...] = (
    ArmSpec("single_small", "Single-small", "single", None,
            _all("gpt-6-luna", "low"), max_run_cost_usd=0.50),
    ArmSpec("single_high", "Single-High", "single", None,
            _all("gpt-6-sol", "xhigh"), max_run_cost_usd=3.00),
    ArmSpec("fixed_multi_agent", "Fixed Multi-Agent", "fixed_group", "complex",
            {"router": RoleModel("gpt-6-luna", "low"),
             "signal_analyst": RoleModel("gpt-5.6-terra", "medium"),
             "result_verifier": RoleModel("gpt-5.6-terra", "medium"),
             "evidence_curator": RoleModel("gpt-5.6-terra", "medium"),
             "safety_verifier": RoleModel("gpt-6-sol", "high")}, max_run_cost_usd=3.00),
    ArmSpec("adaptive", "Adaptive", "adaptive", None,
            {"router": RoleModel("gpt-6-luna", "low"),
             "signal_analyst": RoleModel("gpt-6-luna", "low"),
             "result_verifier": RoleModel("gpt-5.6-terra", "low"),
             "evidence_curator": RoleModel("gpt-5.6-terra", "medium"),
             "safety_verifier": RoleModel("gpt-6-sol", "xhigh")}, max_run_cost_usd=3.00),
)

ARM_IDS = tuple(a.arm_id for a in ARMS)

# P13 (docs/plans/revamp/P13-new-cases.md): same adaptive roles, with and without the judgement guide in RAG.
_ADAPTIVE = next(a for a in ARMS if a.arm_id == "adaptive")
P13_APPROVAL = "approved_2026-10-11_user_delegated_P13"
P13_ARMS: tuple[ArmSpec, ...] = (
    ArmSpec("adaptive_noguide", "Adaptive (no guide)", "adaptive", None, dict(_ADAPTIVE.roles),
            max_run_cost_usd=3.00, approval=P13_APPROVAL, rag_kinds=("runbook", "public_doc")),
    ArmSpec("adaptive_guide", "Adaptive + guide", "adaptive", None, dict(_ADAPTIVE.roles),
            max_run_cost_usd=3.00, approval=P13_APPROVAL, rag_kinds=("runbook", "public_doc", "reference_guide")),
)
P13_ARM_IDS = tuple(a.arm_id for a in P13_ARMS)


def validate_arms(arms=ARMS) -> list[str]:
    errors = []
    ids = [a.arm_id for a in arms]
    if sorted(ids) not in (sorted(ARM_IDS), sorted(P13_ARM_IDS)) or len(set(ids)) != len(ids):
        errors.append(f"expected exactly the four arms {ARM_IDS} (or the P13 pair {P13_ARM_IDS}), got {ids}")
    for a in arms:
        if set(a.roles) != set(ROLES):
            errors.append(f"{a.arm_id}: roles must equal {ROLES}")
        if not (isinstance(a.max_run_cost_usd, (int, float))
                and math.isfinite(a.max_run_cost_usd) and a.max_run_cost_usd > 0):
            errors.append(f"{a.arm_id}: max_run_cost_usd must be finite and > 0")
        if a.strategy == "single" and len({(r.model, r.effort) for r in a.roles.values()}) != 1:
            errors.append(f"{a.arm_id}: single arm must use one model/effort")
        if (a.strategy == "fixed_group") != (a.fixed_workflow is not None):
            errors.append(f"{a.arm_id}: fixed_workflow only for fixed_group")
    return errors
