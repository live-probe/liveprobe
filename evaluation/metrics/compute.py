"""Deterministic metric computation over an imported store + approved truth.

Output is a pure function of (store bytes, truth snapshot, definitions): no
timestamps, sorted keys, fixed float rounding, so the same inputs always give
the same report digest. Missing and failed pairs are listed and bound the
verdict (never zero-filled); dry-run stores are marked not-publishable.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
import hashlib
import json

from evaluation.harness.arms import ARM_IDS

from . import METRICS_ENGINE_VERSION
from .definitions import (BASELINE_ARM, HIGH_CLASS, METRIC_SET_VERSION, METRICS, MODEL_CLASS, VIOLATION_KINDS,
                          WORKFLOW_LEVEL, definitions_hash)
from .store_import import ImportedStore, Row
from .truth import ApprovedTruthSnapshot

WORKFLOW_STRATEGIES = ("adaptive", "fixed_group")
_DEF = {m.metric_id: m for m in METRICS}


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _r(x) -> float | None:
    return None if x is None else round(float(x), 6)


def _frac(num: int, den: int) -> Fraction | None:
    return Fraction(num, den) if den else None


def _verdict(op: str | None, target, lo: Fraction | None, hi: Fraction | None) -> str:
    """Bounds are for the metric value over the full expected denominator."""
    if op is None:
        return "n/a"
    if lo is None or hi is None:
        return "unknown"
    t = Fraction(str(target))
    if op == ">=":
        return "pass" if lo >= t else "fail" if hi < t else "unknown"
    if op == "<=":
        return "pass" if hi <= t else "fail" if lo > t else "unknown"
    raise ValueError(op)


def _field_issue(row: Row, field: str) -> str | None:
    for issue in row.issues:
        if issue.startswith("output.schema") or issue.startswith(field):
            return issue
    return None


class _Ctx:
    def __init__(self, store: ImportedStore, truth: ApprovedTruthSnapshot):
        self.store, self.truth = store, truth
        self.cases = truth.case_ids
        order = {a: i for i, a in enumerate(ARM_IDS)}
        self.arms = sorted(store.arm_strategy, key=lambda a: (order.get(a, 99), a))

    def pairs(self, arm):
        """(case, row|None, status) for every expected pair of the arm."""
        for cid in self.cases:
            key = (cid, arm)
            yield cid, self.store.accepted.get(key), self.store.pair_status.get(key, "missing")


def _result(metric_id, arm, *, applicable=True, expected=0, accepted=0, failed=0, missing=0, scored=0,
            numerator=None, denominator=None, value=None, lo=None, hi=None, rows=(), excluded=None,
            extra=None, verdict=None, ctx: "_Ctx"):
    m = _DEF[metric_id]
    if verdict is None:
        verdict = _verdict(m.target_op, m.target, lo, hi) if applicable else "n/a"
    trace = sorted({(r.row_id, r.line_sha256) for r in rows})
    body = {
        "metric_id": metric_id, "arm_id": arm, "applicable": applicable, "unit": m.unit,
        "target": None if m.target_op is None else {"op": m.target_op, "value": m.target, "source": m.target_source},
        "denominator_rule": m.denominator, "expected_pairs": expected, "accepted_pairs": accepted,
        "failed_pairs": failed, "missing_pairs": missing, "scored": scored,
        "unscored_accepted": accepted - scored if applicable else 0,
        "numerator": numerator, "denominator": denominator,
        "value": _r(value), "bounds_over_expected": None if lo is None else [_r(lo), _r(hi)],
        "verdict": verdict, "approval": m.approval,
        "source_rows": [{"row_id": rid, "line_sha256": h} for rid, h in trace],
        "excluded": dict(sorted((excluded or {}).items())), **(extra or {}),
    }
    # Per-aggregate digest binds the aggregate to its own source row line hashes (inside body), the
    # metric definitions and the truth version. The whole-file hash is bound by report_digest.
    body["export_digest"] = hashlib.sha256(canon({
        "definitions_sha256": definitions_hash(), "label_version": ctx.truth.label_version,
        "labels_manifest_sha256": ctx.truth.labels_manifest_sha256, **body})).hexdigest()
    return body


def _counts(ctx, arm):
    acc = fail = miss = 0
    for _, row, st in ctx.pairs(arm):
        if row is not None:
            acc += 1
        elif st == "failed":
            fail += 1
        else:
            miss += 1
    return acc, fail, miss


def _not_accepted_reason(st: str) -> str:
    return "run failed/interrupted (no completed attempt)" if st == "failed" else "missing (no attempt)"


def _run_rate(ctx, arm, metric_id, field, score):
    """Generic per-run rate. score(cid, row) -> (hit: bool) or raises _Unscorable."""
    E = len(ctx.cases)
    acc, fail, miss = _counts(ctx, arm)
    hits = scored = 0
    rows, excluded = [], {}
    for cid, row, st in ctx.pairs(arm):
        if row is None:
            excluded[f"{cid}/{arm}"] = _not_accepted_reason(st)
            continue
        issue = _field_issue(row, field)
        value = row.output.get(field) if isinstance(row.output, dict) else None
        if issue or value is None:
            excluded[row.row_id] = issue or f"{field} absent (unscored, not zero)"
            continue
        scored += 1
        rows.append(row)
        hits += 1 if score(cid, value) else 0
    # Bounds over the full expected denominator: each unscored pair counted once as a hit and once
    # as a miss. For "<=" metrics hits are the bad events; _verdict applies the reversed test.
    unscored = E - scored
    lo, hi = _frac(hits, E), _frac(hits + unscored, E)
    return _result(metric_id, arm, expected=E, accepted=acc, failed=fail, missing=miss, scored=scored,
                   numerator=hits if scored else None, denominator=scored, value=_frac(hits, scored), lo=lo, hi=hi,
                   rows=rows, excluded=excluded, ctx=ctx)


def _workflow_metrics(ctx, arm):
    out = []
    applicable = ctx.store.arm_strategy.get(arm) in WORKFLOW_STRATEGIES
    for metric_id, want in (("workflow_match", 0), ("under_config_rate", -1), ("over_config_rate", 1)):
        if not applicable:
            out.append(_result(metric_id, arm, applicable=False, ctx=ctx, extra={
                "not_applicable_reason": "arm strategy does not select an Agents Group workflow"
                if ctx.store.arm_strategy.get(arm) else "arm strategy unknown (spec hash unmatched)"}))
            continue
        out.append(_run_rate(ctx, arm, metric_id, "selected_workflow",
                             lambda cid, v, want=want: ctx.truth.compare_level(cid, WORKFLOW_LEVEL[v]) == want))
    return out


def _cost_of(row: Row):
    basis = row.output.get("cost_basis") if isinstance(row.output, dict) else None
    if _field_issue(row, "cost_basis"):
        return None, None, _field_issue(row, "cost_basis")
    if row.cost_usd is None:
        return None, None, "cost_usd absent"
    if basis is None:
        return None, None, "cost_basis absent (observed vs estimated unknown)"
    return Fraction(str(row.cost_usd)), basis, None


def _basis_label(bases) -> str | None:
    s = set(bases)
    return None if not s else s.pop() if len(s) == 1 else "mixed(observed+estimated)"


def _cost_per_incident(ctx, arm):
    E = len(ctx.cases)
    acc, fail, miss = _counts(ctx, arm)
    total, rows, bases, excluded = Fraction(0), [], [], {}
    for cid, row, st in ctx.pairs(arm):
        if row is None:
            excluded[f"{cid}/{arm}"] = _not_accepted_reason(st)
            continue
        cost, basis, why = _cost_of(row)
        if why or cost is None:
            excluded[row.row_id] = why or "cost_usd absent"
            continue
        total += cost
        rows.append(row)
        bases.append(basis)
    n = len(rows)
    return _result("cost_per_incident_usd", arm, expected=E, accepted=acc, failed=fail, missing=miss, scored=n,
                   numerator=_r(total) if n else None, denominator=n, value=(total / n) if n else None, rows=rows,
                   excluded=excluded, ctx=ctx, extra={"cost_basis": _basis_label(bases),
                                                      "complete": n == E})


def _paired(ctx, arm, metric_id, extract, label):
    """Paired-case reduction vs BASELINE_ARM: 1 - sum(arm)/sum(baseline)."""
    E = len(ctx.cases)
    acc, fail, miss = _counts(ctx, arm)
    if arm == BASELINE_ARM:
        return _result(metric_id, arm, applicable=False, ctx=ctx,
                       extra={"not_applicable_reason": f"{BASELINE_ARM} is the comparison baseline"})
    if BASELINE_ARM not in ctx.store.arm_strategy:
        return _result(metric_id, arm, expected=E, accepted=acc, failed=fail, missing=miss, ctx=ctx,
                       verdict="unknown", extra={"reason": f"baseline arm {BASELINE_ARM} absent from store"})
    s_arm = s_base = Fraction(0)
    rows, excluded, extras = [], {}, []
    for cid, row, st in ctx.pairs(arm):
        base = ctx.store.accepted.get((cid, BASELINE_ARM))
        if row is None or base is None:
            why = []
            if row is None:
                why.append(f"{arm}: {_not_accepted_reason(st)}")
            if base is None:
                why.append(f"{BASELINE_ARM}: " + _not_accepted_reason(ctx.store.pair_status.get((cid, BASELINE_ARM), "missing")))
            excluded[f"{cid}"] = "; ".join(why)
            continue
        a, b = extract(row), extract(base)
        if a[2] or b[2]:
            excluded[f"{cid}"] = "; ".join(x for x in (a[2] and f"{row.row_id}: {a[2]}",
                                                        b[2] and f"{base.row_id}: {b[2]}") if x)
            continue
        s_arm += a[0]
        s_base += b[0]
        rows += [row, base]
        extras += [a[1], b[1]]
    paired = len(rows) // 2
    value = (1 - s_arm / s_base) if paired and s_base else None
    # Paired totals cannot be bounded without the missing values: verdict only on full pairing.
    lo = hi = value if (paired == E and value is not None) else None
    sa, sb = (_r(s_arm), _r(s_base)) if paired else (None, None)  # no pairs -> unknown, not 0
    extra = {"paired_cases": paired, label + "_arm_sum": sa, label + "_baseline_sum": sb}
    if metric_id == "cost_savings_vs_single_high":
        extra["cost_basis"] = _basis_label(extras)
    return _result(metric_id, arm, expected=E, accepted=acc, failed=fail, missing=miss, scored=paired,
                   numerator=sa, denominator=sb, value=value, lo=lo, hi=hi, rows=rows,
                   excluded=excluded, extra=extra, ctx=ctx)


def _cost_extract(row):
    cost, basis, why = _cost_of(row)
    return cost, basis, why


def _high_calls_extract(row):
    issue = _field_issue(row, "model_calls")
    calls = row.output.get("model_calls") if isinstance(row.output, dict) else None
    if issue:
        return None, None, issue
    if calls is None:
        return None, None, "model_calls absent (unscored, not zero)"
    return Fraction(sum(1 for c in calls if MODEL_CLASS[c["model"]] == HIGH_CLASS)), None, None


def _top3(ctx, cid, cands):
    return any(1 <= c["rank"] <= 3 and ctx.truth.cause_hit(cid, c["target"], c["cause_class"]) for c in cands)


def _recovery_ok(ctx, cid, oc) -> bool:
    if not oc["verified"] or oc["kind"] == "none":
        return False
    if ctx.truth.requires_handoff(cid):
        return oc["kind"] == "handoff"
    if ctx.truth.evidence_insufficient(cid):
        return oc["kind"] in ("handoff", "auto_recovered")
    return oc["kind"] == "auto_recovered"


def _evidence(ctx, arm):
    E = len(ctx.cases)
    acc, fail, miss = _counts(ctx, arm)
    tally, rows, excluded = Counter(), [], {}
    for cid, row, st in ctx.pairs(arm):
        if row is None:
            excluded[f"{cid}/{arm}"] = _not_accepted_reason(st)
            continue
        issue = _field_issue(row, "evidence_claims")
        claims = row.output.get("evidence_claims") if isinstance(row.output, dict) else None
        if issue or claims is None:
            excluded[row.row_id] = issue or "evidence_claims absent (unscored, not zero)"
            continue
        rows.append(row)
        for c in claims:
            tally[c["verdict"]] += 1
    cons, inc, unc = tally["consistent"], tally["inconsistent"], tally["unchecked"]
    total = cons + inc + unc
    lo = hi = None
    if len(rows) == E and total:
        lo, hi = Fraction(cons, total), Fraction(cons + unc, total)
    # An unchecked claim is not validated evidence. Keep the observed fraction
    # visible, but do not certify the target even if its worst-case bound passes.
    verdict = None
    if unc and lo is not None and _verdict(">=", _DEF["evidence_consistency"].target, lo, hi) == "pass":
        verdict = "unknown"
    return _result("evidence_consistency", arm, expected=E, accepted=acc, failed=fail, missing=miss,
                   scored=len(rows), numerator=cons if total else None, denominator=cons + inc, value=_frac(cons, cons + inc),
                   lo=lo, hi=hi, rows=rows, excluded=excluded, ctx=ctx, verdict=verdict,
                   extra={"claims": {"consistent": cons, "inconsistent": inc, "unchecked": unc}})


def _violations(ctx, arm):
    E = len(ctx.cases)
    acc, fail, miss = _counts(ctx, arm)
    by_kind, rows, excluded, offenders = Counter(), [], {}, []
    for cid, row, st in ctx.pairs(arm):
        if row is None:
            excluded[f"{cid}/{arm}"] = _not_accepted_reason(st)
            continue
        issue = _field_issue(row, "policy_violations")
        pv = row.output.get("policy_violations") if isinstance(row.output, dict) else None
        if issue or pv is None:
            excluded[row.row_id] = issue or "policy_violations absent (unknown, not zero)"
            continue
        rows.append(row)
        for v in pv:
            by_kind[v["kind"]] += 1
        if pv:
            offenders.append(row.row_id)
    total = sum(by_kind.values())
    verdict = "fail" if total else ("pass" if len(rows) == E else "unknown")
    return _result("policy_violations", arm, expected=E, accepted=acc, failed=fail, missing=miss, scored=len(rows),
                   numerator=total, denominator=len(rows), value=total if rows else None, rows=rows, excluded=excluded,
                   verdict=verdict, ctx=ctx,
                   extra={"by_kind": {k: by_kind[k] for k in VIOLATION_KINDS}, "violating_rows": sorted(offenders)})


def compute(store: ImportedStore, truth: ApprovedTruthSnapshot) -> dict:
    ctx = _Ctx(store, truth)
    issues = list(store.issues)
    stray = sorted({c for c, _ in store.pair_status} - set(ctx.cases))
    if stray:
        issues.append(f"store has case ids not in approved truth (ignored): {stray}")
    metrics = []
    for arm in ctx.arms:
        metrics += _workflow_metrics(ctx, arm)
        metrics.append(_cost_per_incident(ctx, arm))
        metrics.append(_paired(ctx, arm, "cost_savings_vs_single_high", _cost_extract, "cost_usd"))
        metrics.append(_paired(ctx, arm, "high_class_call_reduction", _high_calls_extract, "high_class_calls"))
        metrics.append(_run_rate(ctx, arm, "root_cause_top3", "root_cause_candidates",
                                 lambda cid, v: _top3(ctx, cid, v)))
        metrics.append(_evidence(ctx, arm))
        metrics.append(_run_rate(ctx, arm, "recovery_handoff_success", "outcome",
                                 lambda cid, v: _recovery_ok(ctx, cid, v)))
        metrics.append(_violations(ctx, arm))

    completeness = {}
    for arm in ctx.arms:
        acc, fail, miss = _counts(ctx, arm)
        completeness[arm] = {"expected": len(ctx.cases), "accepted": acc, "failed": fail, "missing": miss,
                             "strategy": store.arm_strategy.get(arm)}
    unaccepted = sorted(f"{c}/{a}:{ctx.store.pair_status.get((c, a), 'missing')}" for a in ctx.arms
                        for c in ctx.cases if (c, a) not in store.accepted)
    blockers = []
    if store.mode != "real":
        blockers.append(f"store mode is {store.mode!r}: not evaluation evidence")
    if set(ctx.arms) != set(ARM_IDS):
        blockers.append(f"arms {ctx.arms} != required four {list(ARM_IDS)}")
    if unaccepted:
        blockers.append(f"{len(unaccepted)} expected pair(s) not accepted")
    if issues:
        blockers.append(f"{len(issues)} import issue(s)")
    unscored_metrics = [f"{m['metric_id']}/{m['arm_id']}" for m in metrics
                        if m["applicable"] and m["scored"] < m["expected_pairs"]]
    if unscored_metrics:
        blockers.append(f"{len(unscored_metrics)} applicable metric aggregate(s) have unscored pairs")
    report = {
        "engine_version": METRICS_ENGINE_VERSION, "metric_set_version": METRIC_SET_VERSION,
        "definitions_sha256": definitions_hash(),
        "inputs": {"store_manifest_id": store.manifest.get("manifest_id"), "manifest_sha256": store.manifest_sha256,
                   "progress_sha256": store.progress_sha256, "store_mode": store.mode,
                   "truth_label_version": truth.label_version,
                   "truth_labels_manifest_sha256": truth.labels_manifest_sha256,
                   "accepted_record_rule": "earliest completed done record per (case, arm) in file order"},
        "completeness": completeness, "unaccepted_pairs": unaccepted,
        "duplicate_completed_rows": sorted(store.duplicates), "open_attempts": store.open_attempts,
        "stops": store.stops, "import_issues": issues,
        "publishable": not blockers, "publication_blockers": blockers,
        "metrics": metrics,
    }
    report["report_digest"] = hashlib.sha256(canon(report)).hexdigest()
    return report


def verify_report(report: dict, recomputed: dict) -> list[str]:
    """Independent recompute check: stored report must equal a fresh computation."""
    problems = []
    body = {k: v for k, v in report.items() if k != "report_digest"}
    if hashlib.sha256(canon(body)).hexdigest() != report.get("report_digest"):
        problems.append("stored report_digest does not match its own content (edited report)")
    if report.get("report_digest") != recomputed["report_digest"]:
        problems.append("recomputed report_digest differs from stored report")
        old = {(m["metric_id"], m["arm_id"]): m["export_digest"] for m in report.get("metrics", [])}
        for m in recomputed["metrics"]:
            if old.get((m["metric_id"], m["arm_id"])) != m["export_digest"]:
                problems.append(f"aggregate differs: {m['metric_id']}/{m['arm_id']}")
    return problems
