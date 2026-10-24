"""KAN-137 metrics engine tests. Hand-checkable fixtures; no network, LLM, kubectl or Secret."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from evaluation.harness.arms import ARMS, ARM_IDS
from evaluation.harness.cases import CASES_DIR, Catalog, label_digest
from evaluation.harness.runners import FakeFaultWorker, FakeRunner
from evaluation.harness.scheduler import run_campaign
from evaluation.metrics.__main__ import main as cli
from evaluation.metrics.compute import compute, verify_report
from evaluation.metrics.definitions import METRIC_IDS, METRICS, RUN_OUTPUT_SCHEMA, definitions_hash
from evaluation.metrics.store_import import StoreInvalid, load_store
from evaluation.metrics.truth import ApprovedTruthSnapshot, TruthNotApproved

# (id, family, target, mode, reference_workflow, evidence_sufficiency)
CASES = [
    ("F1-01", "image_crash", "inventory", "bad_image", "simple", "sufficient"),
    ("F2-01", "oom", "inventory", "controlled_oom", "moderate", "insufficient"),
    ("F3-01", "latency_pool", "order", "controlled_latency", "complex", "sufficient"),
    ("F4-01", "endpoint_loss", "order", "selector_mismatch", "high_risk_handoff", "sufficient"),
]
SECRETS = [f"SYNTHETIC-SECRET-CAUSE-{i}" for i in range(1, 5)] + ["SYNTHETIC-DECOY-xyz", "SYNTHETIC-RESPONSE"]
CAUSE = {"bad_image": "image_pull_failure", "controlled_oom": "memory_exhaustion",
         "controlled_latency": "dependency_latency", "selector_mismatch": "service_selector_mismatch"}


def write_catalog(root: Path, status: str = "approved") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    digests = {}
    for i, (cid, fam, target, mode, ref, suff) in enumerate(CASES, 1):
        case = {"version": 1, "id": cid, "family": fam, "target": target,
                "injection": {"mode": mode, "parameters": {}}, "expected_signal": "x",
                "recovery": "restore_snapshot_and_verify", "baseline_checks": ["deployments_ready"],
                "reference_workflow": ref,
                "ground_truth": {"cause": f"SYNTHETIC-SECRET-CAUSE-{i}", "expected_response": "SYNTHETIC-RESPONSE",
                                 "decoy_signal": "SYNTHETIC-DECOY-xyz", "evidence_sufficiency": suff}}
        (root / f"{cid}.yaml").write_text(json.dumps(case), encoding="utf-8")
        digests[cid] = label_digest(case)
    (root / "LABELS.json").write_text(json.dumps({"label_version": 7, "status": status, "cases": digests}))
    return root


def out(workflow=None, cost_basis="observed", models=(), cands=(), claims=(), outcome=None, violations=()):
    o = {"schema": RUN_OUTPUT_SCHEMA, "cost_basis": cost_basis, "model_calls": [{"model": m} for m in models],
         "root_cause_candidates": [{"rank": r, "target": t, "cause_class": c} for r, t, c in cands],
         "evidence_claims": [{"claim_id": f"e{i}", "verdict": v} for i, v in enumerate(claims)],
         "outcome": outcome or {"kind": "auto_recovered", "verified": True},
         "policy_violations": [{"kind": k} for k in violations]}
    if workflow is not None:
        o["selected_workflow"] = workflow
    return o


def hit(cid):
    c = next(x for x in CASES if x[0] == cid)
    return (1, c[2], CAUSE[c[3]])


def baseline_runs():
    """Hand-checkable 4 cases x 4 arms. Expected values are asserted in MetricTests."""
    runs = []
    wf_adaptive = {"F1-01": "simple_group", "F2-01": "complex_group", "F3-01": "normal_group",
                   "F4-01": "high_risk_group"}
    cost_adaptive = {"F1-01": 0.10, "F2-01": 0.20, "F3-01": 0.30, "F4-01": 0.20}
    models_adaptive = {"F1-01": ["gpt-6-luna", "gpt-6-luna"], "F2-01": ["gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol"],
                       "F3-01": ["gpt-6-sol", "gpt-6-sol"], "F4-01": ["gpt-6-sol"]}
    cands_adaptive = {"F1-01": [hit("F1-01")],
                      "F2-01": [(1, "order", "process_crash"), (4, "inventory", "memory_exhaustion")],
                      "F3-01": [(2, "inventory", "dependency_latency")],
                      "F4-01": [(1, "order", "endpoint_unavailable"), (2, "inventory", "process_crash"),
                                (3, "order", "service_selector_mismatch")]}
    claims_adaptive = {"F1-01": ["consistent", "consistent"], "F2-01": ["consistent", "inconsistent"],
                       "F3-01": ["consistent", "unchecked"], "F4-01": ["consistent"]}
    outcome_adaptive = {"F1-01": {"kind": "auto_recovered", "verified": True},
                        "F2-01": {"kind": "handoff", "verified": True},
                        "F3-01": {"kind": "handoff", "verified": True},
                        "F4-01": {"kind": "handoff", "verified": True}}
    for cid, *_ in CASES:
        good_outcome = {"kind": "handoff" if cid == "F4-01" else "auto_recovered", "verified": True}
        runs.append((cid, "single_small", "completed", 0.02,
                     out(None, "estimated", ["gpt-6-luna"], [hit(cid)], ["consistent"], good_outcome)))
        runs.append((cid, "single_high", "completed", 0.40,
                     out(None, "observed", ["gpt-6-sol"] * 3, [hit(cid)], ["consistent"], good_outcome)))
        runs.append((cid, "fixed_multi_agent", "completed", 0.50,
                     out("complex_group", "observed", ["gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol"], [hit(cid)],
                         ["consistent"], good_outcome,
                         ["out_of_scope_action"] if cid == "F2-01" else ())))
        runs.append((cid, "adaptive", "completed", cost_adaptive[cid],
                     out(wf_adaptive[cid], "observed", models_adaptive[cid], cands_adaptive[cid],
                         claims_adaptive[cid], outcome_adaptive[cid])))
    return runs


def write_store(store: Path, runs, mode="real", open_starts=(), torn=False) -> Path:
    """Write a KAN-133-format store (manifest.json + progress.jsonl) from run tuples."""
    store.mkdir(parents=True, exist_ok=True)
    body = {"harness_version": "fixture", "mode": mode, "cost_ceiling_usd": 100, "runner": "fixture",
            "fault_worker": "fixture", "python": "x", "git_head": None, "files": {},
            "arms": {a.arm_id: a.spec_hash() for a in ARMS}, "catalog": {}, "extra": {}}
    mid = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    (store / "manifest.json").write_text(json.dumps({**body, "manifest_id": mid}, indent=2, sort_keys=True))
    attempts: dict = {}
    lines = []
    for cid, arm, status, cost, output in runs:
        a = attempts[(cid, arm)] = attempts.get((cid, arm), 0) + 1
        lines.append({"manifest_id": mid, "ts": 0, "kind": "start", "case_id": cid, "arm_id": arm, "attempt": a,
                      "reserve_usd": 1, "inputs": {}})
        lines.append({"manifest_id": mid, "ts": 0, "kind": "done", "case_id": cid, "arm_id": arm, "attempt": a,
                      "status": status, "cost_usd": cost, "output": output, "error": None, "baseline": {}})
    for cid, arm in open_starts:
        a = attempts[(cid, arm)] = attempts.get((cid, arm), 0) + 1
        lines.append({"manifest_id": mid, "ts": 0, "kind": "start", "case_id": cid, "arm_id": arm, "attempt": a,
                      "reserve_usd": 1, "inputs": {}})
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in lines)
    if torn:
        text += '{"kind": "do'
    (store / "progress.jsonl").write_text(text)
    return store


def M(report, metric, arm):
    return next(m for m in report["metrics"] if m["metric_id"] == metric and m["arm_id"] == arm)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cases = write_catalog(self.tmp / "cases")

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, runs, **kw):
        store = write_store(self.tmp / "store", runs, **kw)
        return compute(load_store(store), ApprovedTruthSnapshot(self.cases))


class DefinitionTests(unittest.TestCase):
    def test_versioned_ten_metrics_with_jira_targets(self):
        self.assertEqual(len(METRIC_IDS), 10)
        t = {m.metric_id: (m.target_op, m.target) for m in METRICS}
        self.assertEqual(t["under_config_rate"], ("<=", 0.05))
        self.assertEqual(t["over_config_rate"], ("<=", 0.10))
        self.assertEqual(t["cost_savings_vs_single_high"], (">=", 0.30))
        self.assertEqual(t["high_class_call_reduction"], (">=", 0.30))
        self.assertEqual(t["root_cause_top3"], (">=", 0.80))
        self.assertEqual(t["evidence_consistency"], (">=", 0.95))
        self.assertEqual(t["workflow_match"], (">=", 0.80))
        self.assertEqual(t["recovery_handoff_success"], (">=", 1.0))
        self.assertEqual(t["policy_violations"], ("==", 0))
        self.assertTrue(all(m.denominator for m in METRICS))
        self.assertEqual(definitions_hash(), definitions_hash())


class MetricTests(Base):
    def test_hand_checked_values(self):
        r = self.report(baseline_runs())
        # adaptive workflow: F1 match, F2 over, F3 under, F4 match
        self.assertEqual((M(r, "workflow_match", "adaptive")["numerator"], M(r, "workflow_match", "adaptive")["denominator"]), (2, 4))
        self.assertEqual(M(r, "under_config_rate", "adaptive")["value"], 0.25)
        self.assertEqual(M(r, "over_config_rate", "adaptive")["value"], 0.25)
        self.assertEqual(M(r, "workflow_match", "adaptive")["verdict"], "fail")
        self.assertEqual(M(r, "under_config_rate", "adaptive")["verdict"], "fail")
        # fixed (always complex): F1 over, F2 over, F3 match, F4 under
        self.assertEqual(M(r, "workflow_match", "fixed_multi_agent")["value"], 0.25)
        self.assertEqual(M(r, "over_config_rate", "fixed_multi_agent")["value"], 0.5)
        # single arms don't select a workflow -> n/a, not 0
        for arm in ("single_small", "single_high"):
            m = M(r, "workflow_match", arm)
            self.assertEqual((m["applicable"], m["value"], m["verdict"]), (False, None, "n/a"))
        # cost: adaptive 0.80 vs single_high 1.60 -> 50% savings
        s = M(r, "cost_savings_vs_single_high", "adaptive")
        self.assertEqual((s["value"], s["paired_cases"], s["verdict"], s["cost_basis"]), (0.5, 4, "pass", "observed"))
        self.assertEqual(M(r, "cost_savings_vs_single_high", "fixed_multi_agent")["value"], -0.25)
        self.assertEqual(M(r, "cost_savings_vs_single_high", "fixed_multi_agent")["verdict"], "fail")
        small = M(r, "cost_savings_vs_single_high", "single_small")
        self.assertEqual((small["value"], small["cost_basis"]), (0.95, "mixed(observed+estimated)"))
        self.assertEqual(M(r, "cost_per_incident_usd", "single_small")["cost_basis"], "estimated")
        self.assertEqual(M(r, "cost_per_incident_usd", "adaptive")["value"], 0.2)
        self.assertEqual(M(r, "cost_savings_vs_single_high", "single_high")["applicable"], False)
        # high-class calls: adaptive 0+1+2+1=4 vs 12 -> 0.666667
        h = M(r, "high_class_call_reduction", "adaptive")
        self.assertEqual((h["numerator"], h["denominator"], h["value"], h["verdict"]), (4.0, 12.0, 0.666667, "pass"))
        self.assertEqual(M(r, "high_class_call_reduction", "single_small")["value"], 1.0)
        # top-3: F1 hit, F2 rank4 miss, F3 wrong target miss, F4 rank3 hit
        t = M(r, "root_cause_top3", "adaptive")
        self.assertEqual((t["numerator"], t["denominator"], t["verdict"]), (2, 4, "fail"))
        self.assertEqual(M(r, "root_cause_top3", "single_high")["verdict"], "pass")
        # evidence: 5 consistent, 1 inconsistent, 1 unchecked
        e = M(r, "evidence_consistency", "adaptive")
        self.assertEqual((e["numerator"], e["denominator"], e["claims"]["unchecked"]), (5, 6, 1))
        self.assertEqual(e["bounds_over_expected"], [round(5 / 7, 6), round(6 / 7, 6)])
        self.assertEqual(e["verdict"], "fail")
        # recovery: F3 unrequired handoff is a failure; F2 insufficient evidence handoff ok
        rh = M(r, "recovery_handoff_success", "adaptive")
        self.assertEqual((rh["numerator"], rh["denominator"], rh["verdict"]), (3, 4, "fail"))
        self.assertEqual(M(r, "recovery_handoff_success", "single_high")["verdict"], "pass")
        # violations: never omitted
        v = M(r, "policy_violations", "fixed_multi_agent")
        self.assertEqual((v["value"], v["by_kind"]["out_of_scope_action"], v["verdict"]), (1, 1, "fail"))
        self.assertEqual(len(v["violating_rows"]), 1)
        self.assertEqual(M(r, "policy_violations", "adaptive")["verdict"], "pass")
        self.assertTrue(r["publishable"])
        self.assertEqual(len(r["metrics"]), 40)

    def test_every_aggregate_has_source_rows_and_digest(self):
        r = self.report(baseline_runs())
        store = load_store(self.tmp / "store")
        ids = {row.row_id: row.line_sha256 for row in store.rows}
        for m in r["metrics"]:
            self.assertRegex(m["export_digest"], r"^[0-9a-f]{64}$")
            for s in m["source_rows"]:
                self.assertEqual(ids[s["row_id"]], s["line_sha256"])
        self.assertEqual(len(M(r, "workflow_match", "adaptive")["source_rows"]), 4)
        self.assertEqual(len(M(r, "cost_savings_vs_single_high", "adaptive")["source_rows"]), 8)
        raw = (self.tmp / "store" / "progress.jsonl").read_bytes().split(b"\n")
        rid = M(r, "workflow_match", "adaptive")["source_rows"][0]
        line_no = int(rid["row_id"].split(":")[0][1:])
        self.assertEqual(hashlib.sha256(raw[line_no - 1]).hexdigest(), rid["line_sha256"])


class MissingFailedTests(Base):
    def test_missing_and_failed_bound_verdict_never_zero_filled(self):
        runs = [r for r in baseline_runs() if not (r[1] == "adaptive" and r[0] in ("F3-01", "F4-01"))]
        runs.append(("F3-01", "adaptive", "failed", 0.0, {}))
        r = self.report(runs)
        c = r["completeness"]["adaptive"]
        self.assertEqual((c["accepted"], c["failed"], c["missing"]), (2, 1, 1))
        w = M(r, "workflow_match", "adaptive")
        self.assertEqual((w["numerator"], w["denominator"], w["expected_pairs"]), (1, 2, 4))
        self.assertEqual(w["bounds_over_expected"], [0.25, 0.75])
        self.assertEqual(w["verdict"], "fail")
        u = M(r, "under_config_rate", "adaptive")
        self.assertEqual((u["value"], u["bounds_over_expected"], u["verdict"]), (0.0, [0.0, 0.5], "unknown"))
        self.assertIn("F3-01/adaptive", w["excluded"])
        self.assertIn("F4-01/adaptive", w["excluded"])
        s = M(r, "cost_savings_vs_single_high", "adaptive")
        self.assertEqual((s["paired_cases"], s["verdict"]), (2, "unknown"))
        self.assertEqual(M(r, "policy_violations", "adaptive")["verdict"], "unknown")
        self.assertFalse(r["publishable"])
        self.assertIn("F3-01/adaptive:failed", r["unaccepted_pairs"])
        self.assertIn("F4-01/adaptive:missing", r["unaccepted_pairs"])

    def test_absent_field_is_unscored_not_zero(self):
        runs = baseline_runs()
        runs = [(c, a, s, cost, {k: v for k, v in o.items() if not (a == "adaptive" and c == "F1-01"
                                                                 and k in ("policy_violations", "cost_basis"))})
                for c, a, s, cost, o in runs]
        r = self.report(runs)
        v = M(r, "policy_violations", "adaptive")
        self.assertEqual((v["scored"], v["verdict"]), (3, "unknown"))
        cpi = M(r, "cost_per_incident_usd", "adaptive")
        self.assertEqual((cpi["scored"], cpi["complete"]), (3, False))
        self.assertTrue(any("cost_basis absent" in x for x in cpi["excluded"].values()))
        self.assertEqual(M(r, "cost_savings_vs_single_high", "adaptive")["verdict"], "unknown")

    def test_unmapped_model_makes_run_unscorable(self):
        runs = baseline_runs()
        runs = [(c, a, s, cost, {**o, "model_calls": [{"model": "vendor-x-mega"}]} if (a, c) == ("adaptive", "F1-01")
                 else o) for c, a, s, cost, o in runs]
        r = self.report(runs)
        h = M(r, "high_class_call_reduction", "adaptive")
        self.assertEqual((h["paired_cases"], h["verdict"]), (3, "unknown"))
        self.assertTrue(any("unmapped model" in x for x in h["excluded"].values()))

    def test_duplicates_retry_open_attempt_and_torn_tail(self):
        runs = baseline_runs()
        runs.insert(0, ("F1-01", "adaptive", "failed", 0.0, {}))  # attempt 1 failed, attempt 2 completes
        runs.append(("F2-01", "adaptive", "completed", 9.99, out("simple_group")))  # duplicate completion
        r = self.report(runs, open_starts=[("F1-01", "single_small")], torn=True)
        self.assertEqual(r["completeness"]["adaptive"]["accepted"], 4)
        self.assertEqual(len(r["duplicate_completed_rows"]), 1)
        self.assertEqual(M(r, "cost_per_incident_usd", "adaptive")["value"], 0.2)  # duplicate not counted
        self.assertEqual(M(r, "over_config_rate", "adaptive")["value"], 0.25)       # earliest accepted
        self.assertEqual(len(r["open_attempts"]), 1)
        self.assertTrue(any("torn tail" in i for i in r["import_issues"]))
        self.assertFalse(r["publishable"])

    def test_dry_run_store_not_publishable(self):
        r = self.report(baseline_runs(), mode="dry_run")
        self.assertFalse(r["publishable"])
        self.assertTrue(any("dry_run" in b for b in r["publication_blockers"]))


class IntegrityTests(Base):
    def test_tampered_manifest_and_bad_records_rejected(self):
        store = write_store(self.tmp / "store", baseline_runs())
        m = json.loads((store / "manifest.json").read_text())
        m["mode"] = "real-edited"
        (store / "manifest.json").write_text(json.dumps(m))
        with self.assertRaises(StoreInvalid):
            load_store(store)
        store2 = write_store(self.tmp / "s2", baseline_runs())
        lines = (store2 / "progress.jsonl").read_text().splitlines()
        lines.insert(3, "not json")
        (store2 / "progress.jsonl").write_text("\n".join(lines) + "\n")
        with self.assertRaises(StoreInvalid):
            load_store(store2)

    def test_deterministic_and_recompute_detects_edit(self):
        store = write_store(self.tmp / "store", baseline_runs())
        truth = ApprovedTruthSnapshot(self.cases)
        before = (store / "progress.jsonl").read_bytes()
        a, b = compute(load_store(store), truth), compute(load_store(store), truth)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(verify_report(a, b), [])
        self.assertEqual((store / "progress.jsonl").read_bytes(), before)  # read-only
        # Edit one adaptive row's raw output: only adaptive aggregates change digest.
        text = (store / "progress.jsonl").read_text().replace('"normal_group"', '"complex_group"')
        (store / "progress.jsonl").write_text(text)
        c = compute(load_store(store), truth)
        problems = verify_report(a, c)
        self.assertIn("recomputed report_digest differs from stored report", problems)
        changed = {p.split(": ")[1] for p in problems if p.startswith("aggregate differs")}
        self.assertTrue(changed)
        self.assertTrue(all(x.endswith("/adaptive") for x in changed), changed)
        edited = dict(a, publishable=False)
        self.assertIn("stored report_digest does not match its own content (edited report)", verify_report(edited, a))


    def test_offline_fixture_copy_recomputes_and_detects_tampered_export(self):
        """Re-open a fixture in an independent directory; this is not a VDI bundle or live evidence."""
        original = write_store(self.tmp / "original" / "store", baseline_runs(), mode="dry_run")
        report = compute(load_store(original), ApprovedTruthSnapshot(self.cases))
        source = self.tmp / "offline-source"
        shutil.copytree(original, source / "store")
        shutil.copytree(self.cases, source / "cases")
        (source / "report.json").write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        with tempfile.TemporaryDirectory() as destination:
            offline = Path(destination) / "offline"
            shutil.copytree(source, offline)
            reopened = json.loads((offline / "report.json").read_text(encoding="utf-8"))
            recalculated = compute(load_store(offline / "store"), ApprovedTruthSnapshot(offline / "cases"))
            self.assertEqual(verify_report(reopened, recalculated), [])
            self.assertFalse(reopened["publishable"])
            self.assertEqual(reopened["inputs"]["progress_sha256"],
                             hashlib.sha256((offline / "store" / "progress.jsonl").read_bytes()).hexdigest())
            (offline / "store" / "progress.jsonl").write_bytes(
                (offline / "store" / "progress.jsonl").read_bytes().replace(b'"normal_group"', b'"complex_group"'))
            changed = compute(load_store(offline / "store"), ApprovedTruthSnapshot(offline / "cases"))
            self.assertIn("recomputed report_digest differs from stored report", verify_report(reopened, changed))

class TruthIsolationTests(Base):
    def test_truth_must_be_approved_and_pinned(self):
        draft = write_catalog(self.tmp / "draft", status="draft")
        with self.assertRaises(TruthNotApproved):
            ApprovedTruthSnapshot(draft)
        case = json.loads((self.cases / "F1-01.yaml").read_text())
        case["reference_workflow"] = "complex"  # silent relabel
        (self.cases / "F1-01.yaml").write_text(json.dumps(case))
        with self.assertRaises(TruthNotApproved):
            ApprovedTruthSnapshot(self.cases)

    def test_no_label_text_in_snapshot_or_report(self):
        truth = ApprovedTruthSnapshot(self.cases)
        self.assertFalse(hasattr(truth, "__dict__"))
        blob = repr(truth) + repr([getattr(truth, s) for s in truth.__slots__])
        r = self.report(baseline_runs())
        text = json.dumps(r, ensure_ascii=False)
        for s in SECRETS:
            self.assertNotIn(s, blob)
            self.assertNotIn(s, text)
        for key in ("ground_truth", "reference_workflow", "expected_response", "decoy_signal"):
            self.assertNotIn(key, text)

    def test_real_catalog_snapshot_loads_60(self):
        truth = ApprovedTruthSnapshot(CASES_DIR)
        self.assertEqual(len(truth.case_ids), 60)
        self.assertEqual(truth.label_version, 1)


class HarnessStoreTests(Base):
    def test_imports_real_kan133_dry_run_store(self):
        store = self.tmp / "h"
        out_ = run_campaign(store, Catalog(self.cases), FakeRunner(), FakeFaultWorker(), cost_ceiling_usd=100,
                            dry_run=True)
        self.assertFalse(out_.stopped)
        s = load_store(store)
        self.assertEqual(len(s.accepted), 16)
        self.assertEqual(set(s.arm_strategy), set(ARM_IDS))
        self.assertTrue(all(v is not None for v in s.arm_strategy.values()))
        r = compute(s, ApprovedTruthSnapshot(self.cases))
        self.assertFalse(r["publishable"])
        # Fake outputs carry no kan59 schema -> every applicable metric unscored, verdict unknown.
        for m in r["metrics"]:
            if m["applicable"]:
                self.assertEqual(m["scored"], 0, m["metric_id"])
                self.assertIsNone(m["value"], (m["metric_id"], m["arm_id"]))  # never zero-filled
                want = "n/a" if m["target"] is None else "unknown"
                self.assertEqual(m["verdict"], want, (m["metric_id"], m["arm_id"]))


class CliTests(Base):
    def run_cli(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli(list(argv))
        return rc, o.getvalue(), e.getvalue()

    def test_compute_verify_and_guards(self):
        store = write_store(self.tmp / "store", baseline_runs())
        rep = self.tmp / "report.json"
        rc, _, err = self.run_cli("compute", "--store", str(store), "--cases-dir", str(self.cases), "--out", str(rep),
                                  "--summary", "--require-publishable")
        self.assertEqual(rc, 0, err)
        rc2, o2, _ = self.run_cli("compute", "--store", str(store), "--cases-dir", str(self.cases))
        self.assertEqual(o2, rep.read_text())  # byte-identical recompute
        rc, o, _ = self.run_cli("verify", "--store", str(store), "--cases-dir", str(self.cases), "--report", str(rep))
        self.assertEqual((rc, json.loads(o)["ok"]), (0, True))
        rc, _, err = self.run_cli("compute", "--store", str(store), "--cases-dir", str(self.cases),
                                  "--out", str(store / "r.json"))
        self.assertEqual(rc, 2)
        self.assertFalse((store / "r.json").exists())
        rc, _, _ = self.run_cli("compute", "--store", str(self.tmp / "nope"), "--cases-dir", str(self.cases))
        self.assertEqual(rc, 2)
        dry = write_store(self.tmp / "dry", baseline_runs(), mode="dry_run")
        rc, _, _ = self.run_cli("compute", "--store", str(dry), "--cases-dir", str(self.cases), "--require-publishable")
        self.assertEqual(rc, 3)
        rc, o, _ = self.run_cli("definitions")
        self.assertEqual((rc, len(json.loads(o)["metrics"])), (0, 10))


if __name__ == "__main__":
    unittest.main()
