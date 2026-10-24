"""Policy profiles and the operator-review ledger (P7).

Two profiles (docs/plans/liveprobe-revamp-plan.md 5.6):

* ``assessment``    - the 240-pair comparison and any run whose results are scored. HIGH_RISK never executes:
                      it is handed to the operator, exactly as 과제계획서.md and the approved answer key expect.
* ``poc_autonomy``  - live demonstration / new-case work on the dedicated PoC cluster. HIGH_RISK follows the
                      automation mode instead of being forced to a handoff.

``poc_autonomy`` is only honoured when the cluster it runs against is the one that was pinned for it
(kube-system namespace UID). On any other cluster the profile silently falls back to ``assessment`` and says why,
so a copied configuration can never widen what the product may do somewhere else.

The ledger records, for every HIGH_RISK situation and every action that policy held back or ran under
``poc_autonomy``, WHY a human review was judged necessary and what happened. It is data for the operator to derive
review criteria from later; it never changes what the product does.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from . import db
from .redact import redact, redact_obj

PROFILES = ("assessment", "poc_autonomy")


@dataclass(frozen=True)
class Profile:
    name: str                 # the profile actually in force
    requested: str
    reason: str | None = None  # why a requested poc_autonomy was downgraded


def resolve_profile(requested: str, pinned_uid: str | None, kube: Any) -> Profile:
    """Profile in force. Fails closed to `assessment` on any doubt."""
    if requested not in PROFILES:
        return Profile("assessment", requested, f"unknown profile {requested!r}")
    if requested == "assessment":
        return Profile("assessment", requested)
    if not pinned_uid:
        return Profile("assessment", requested, "poc_autonomy needs a pinned cluster identity (LIVEPROBE_POC_CLUSTER_UID)")
    try:
        ns = kube.get_namespace_uid("kube-system")
    except Exception as e:  # noqa: BLE001 - cannot prove where we are -> do not widen
        return Profile("assessment", requested, f"cluster identity unreadable ({type(e).__name__})")
    if not ns or ns != pinned_uid:
        return Profile("assessment", requested, "this is not the pinned PoC cluster")
    return Profile("poc_autonomy", requested)


def high_risk_policy(level: str, mode: str, profile: str) -> str | None:
    """Override for HIGH_RISK: None = keep the default (always handoff)."""
    if level == "HIGH_RISK" and profile == "poc_autonomy":
        # Even here HIGH_RISK is never automatic: an operator approves it (or it stays a suggestion).
        return "suggest_only" if mode == "suggest_only" else "approval"
    return None


# ------------------------------------------------------------------ ledger
REASON_CODES = ("high_risk_level", "low_confidence", "agents_disagree", "citation_failed", "router_failure",
                "action_held_by_policy", "action_ran_under_poc_autonomy", "operator_stopped", "action_failed")


def record(incident_id: int, reason_code: str, *, profile: str, level: str | None, workload: str | None,
           proposed: dict | None = None, rationale: str = "", impact: str = "", outcome: str | None = None,
           rollback: str | None = None, operator_involved: bool = False, detail: dict | None = None) -> int | None:
    """Append one ledger entry. Never raises into the pipeline (the ledger must not break an incident)."""
    from .models import ReviewLedger
    try:
        with db.session() as s:
            row = ReviewLedger(incident_id=incident_id, reason_code=reason_code if reason_code in REASON_CODES else "high_risk_level",
                               profile=profile, level=level, workload=workload,
                               proposed=redact_obj(proposed or {}), rationale=redact(rationale)[:1500], impact=redact(impact)[:600],
                               outcome=outcome, rollback=rollback, operator_involved=operator_involved, detail=redact_obj(detail or {}))
            s.add(row)
            s.flush()
            return row.id
    except Exception:  # noqa: BLE001
        return None


FIELDS = ("id", "created_at", "incident_id", "reason_code", "profile", "level", "workload", "proposed", "rationale",
          "impact", "outcome", "rollback", "operator_involved")


def _row(r) -> dict:
    return {"id": r.id, "created_at": r.created_at.isoformat().replace("+00:00", "Z"), "incident_id": r.incident_id,
            "reason_code": r.reason_code, "profile": r.profile, "level": r.level, "workload": r.workload,
            "proposed": r.proposed, "rationale": r.rationale, "impact": r.impact, "outcome": r.outcome,
            "rollback": r.rollback, "operator_involved": r.operator_involved}


def list_entries(limit: int = 200, offset: int = 0, reason: str | None = None, incident_id: int | None = None) -> dict:
    from .models import ReviewLedger
    from sqlalchemy import func
    q = select(ReviewLedger)
    cq = select(func.count()).select_from(ReviewLedger)
    for cond in ([ReviewLedger.reason_code == reason] if reason else []) + ([ReviewLedger.incident_id == incident_id] if incident_id else []):
        q, cq = q.where(cond), cq.where(cond)
    with db.session() as s:
        total = s.scalar(cq) or 0
        rows = s.scalars(q.order_by(ReviewLedger.id.desc()).limit(max(1, min(limit, 1000))).offset(max(0, offset))).all()
        by_reason = dict(s.execute(select(ReviewLedger.reason_code, func.count()).group_by(ReviewLedger.reason_code)).all())
        return {"total": total, "by_reason": by_reason, "items": [_row(r) for r in rows]}


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def to_csv(items: list[dict]) -> str:
    """CSV for spreadsheets. Cells that start with a formula character are neutralised (CSV injection)."""
    import json
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(FIELDS)
    for it in items:
        cells = []
        for k in FIELDS:
            v = it.get(k)
            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else ("" if v is None else str(v))
            cells.append("'" + v if v.startswith(_FORMULA) else v)
        w.writerow(cells)
    return out.getvalue()
