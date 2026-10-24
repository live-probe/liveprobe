"""KAN-133 harness skeleton tests. No network, LLM, kubectl or Secret access."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evaluation.harness import arms as arms_mod
from evaluation.harness.arms import ARMS, ARM_IDS, validate_arms
from evaluation.harness.cases import CASES_DIR, Catalog, HiddenLabelLeak, label_digest
from evaluation.harness.manifest import ManifestMismatch
from evaluation.harness.runners import FakeFaultWorker, FakeRunner, InferenceRunner
from evaluation.harness.scheduler import NamespaceLease, NotAuthorized, Stop, run_campaign
from evaluation.harness.store import ProgressStore
from evaluation.harness.__main__ import main

FULL = sum(a.max_run_cost_usd for a in ARMS) * 60  # worst-case reservation for 240 runs


def synthetic_catalog(root: Path, n: int = 2) -> Path:
    """Tiny catalog with invented labels so tests never depend on real answers."""
    cases = {}
    for i in range(1, n + 1):
        cid = f"F1-{i:02d}"
        case = {"version": 1, "id": cid, "family": "image_crash", "target": "inventory",
                "injection": {"mode": "bad_image", "parameters": {"tag": f"liveprobe/inventory:does-not-exist-{i:02d}"}},
                "expected_signal": "image_pull", "recovery": "restore_snapshot_and_verify",
                "baseline_checks": ["deployments_ready", "services_endpoints_present", "order_check_200"],
                "reference_workflow": "simple",
                "ground_truth": {"cause": f"SYNTHETIC-SECRET-CAUSE-{i}", "expected_response": f"SYNTHETIC-RESPONSE-{i}",
                                 "decoy_signal": "SYNTHETIC-DECOY-xyz", "evidence_sufficiency": "sufficient"}}
        (root / f"{cid}.yaml").write_text(json.dumps(case), encoding="utf-8")
        cases[cid] = label_digest(case)
    (root / "LABELS.json").write_text(json.dumps({"label_version": 1, "status": "approved", "cases": cases}))
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"

    def tearDown(self):
        self._tmp.cleanup()

    def camp(self, catalog, runner=None, worker=None, ceiling=FULL, **kw):
        return run_campaign(self.store, catalog, runner or FakeRunner(), worker or FakeFaultWorker(),
                            cost_ceiling_usd=ceiling, dry_run=True, **kw)


class ArmTests(unittest.TestCase):
    def test_four_frozen_arms(self):
        self.assertEqual(ARM_IDS, ("single_small", "single_high", "fixed_multi_agent", "adaptive"))
        self.assertEqual(validate_arms(), [])
        self.assertEqual(len({a.spec_hash() for a in ARMS}), 4)
        self.assertTrue(all(a.approval == "approved_2026-10-05_user_delegated" for a in ARMS))

    def test_single_arm_must_be_one_model(self):
        bad = arms_mod.ArmSpec("single_small", "x", "single", None,
                               {**ARMS[0].roles, "router": arms_mod.RoleModel("gpt-6-sol", "high")}, 0.1)
        self.assertTrue(validate_arms((bad,) + ARMS[1:]))


class RealCatalogTests(Base):
    def test_catalog_60_pinned_and_public_view_clean(self):
        cat = Catalog(CASES_DIR)
        self.assertEqual(len(cat.case_ids), 60)
        self.assertEqual(cat.verify_labels_pinned(), [])
        for cid in cat.case_ids:
            cat.assert_no_hidden(cat.public[cid])  # must not raise
            self.assertNotIn("ground_truth", cat.public[cid])
            self.assertNotIn("reference_workflow", cat.public[cid])
            self.assertNotIn("injection", cat.public[cid])

    def test_fake_campaign_restores_every_family_pair_and_resume_never_reinjects_accepted(self):
        """Contract only: 240 fake pairs are not 240 live injections or model calls."""
        cat = Catalog(CASES_DIR)
        first_worker = FakeFaultWorker()
        first = self.camp(cat, worker=first_worker, max_pairs=1)
        self.assertEqual(first.tally["accepted_pairs"], 1)
        runner, worker = FakeRunner(), FakeFaultWorker()
        resumed = self.camp(cat, runner=runner, worker=worker)
        self.assertEqual(resumed.tally["accepted_pairs"], 240)
        self.assertEqual(len(runner.calls), 239)
        self.assertNotIn((cat.case_ids[0], ARM_IDS[0]), runner.calls)
        self.assertEqual(len(set(runner.calls)), 239)
        from collections import Counter
        counts = Counter(cid.split("-")[0] for cid, _ in runner.calls)
        self.assertEqual(dict(counts), {"F1": 59, "F2": 60, "F3": 60, "F4": 60})
        events = worker.events
        self.assertEqual((events[0][0], events[-1][0]), ("acquire", "release"))
        self.assertEqual(len(events), 2 + 2 * 239)
        for injected, restored in zip(events[1:-1:2], events[2:-1:2]):
            self.assertEqual((injected[0], restored[0], injected[1]), ("inject", "restore", restored[1]))
        for payload in runner.seen_payloads:
            cat.assert_no_hidden(payload)
            self.assertEqual(set(payload), set(("version", "id", "family", "target", "expected_signal", "baseline_checks")))

    def test_full_dry_run_240_and_runner_saw_only_public(self):
        cat = Catalog(CASES_DIR)
        runner = FakeRunner()
        out = self.camp(cat, runner)
        self.assertFalse(out.stopped, out.reason)
        self.assertEqual(out.tally["accepted_pairs"], 240)
        self.assertEqual(out.tally["per_arm"], {a: 60 for a in ARM_IDS})
        self.assertEqual(len(set(runner.calls)), 240)
        for payload in runner.seen_payloads:
            cat.assert_no_hidden(payload)
        manifest = json.loads((self.store / "manifest.json").read_text())
        self.assertEqual(len(manifest["catalog"]["cases"]), 60)
        self.assertEqual(manifest["mode"], "dry_run")
        self.assertEqual(set(manifest["arms"]), set(ARM_IDS))
        self.assertTrue(all(manifest["files"][f] for f in manifest["files"]))


class GuardTests(Base):
    def test_guard_rejects_hidden_keys_and_values(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        with self.assertRaises(HiddenLabelLeak):
            cat.assert_no_hidden({"id": "F1-01", "nested": [{"ground_truth": {}}]})
        with self.assertRaises(HiddenLabelLeak) as ctx:
            cat.assert_no_hidden({"prompt": "hint: SYNTHETIC-SECRET-CAUSE-1 here"})
        self.assertNotIn("SYNTHETIC-SECRET-CAUSE-1", str(ctx.exception))

    def test_leaking_public_view_stops_campaign_before_inference(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        cat.public["F1-01"]["expected_signal"] = "SYNTHETIC-DECOY-xyz"  # simulate a leaky view
        runner = FakeRunner()
        out = self.camp(cat, runner)
        self.assertTrue(out.stopped)
        self.assertIn("hidden label", out.reason)
        self.assertEqual(runner.calls, [])

    def test_relabel_without_version_is_preflight_stop(self):
        root = synthetic_catalog(self.tmp)
        case = json.loads((root / "F1-01.yaml").read_text())
        case["ground_truth"]["cause"] = "SYNTHETIC-CHANGED-CAUSE"
        (root / "F1-01.yaml").write_text(json.dumps(case))
        with self.assertRaises(Stop):
            self.camp(Catalog(root))


class BudgetTests(Base):
    def test_ceiling_is_mandatory(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        for bad in (0, -1, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.camp(cat, ceiling=bad)

    def test_invalid_arm_reservations_rejected_before_injection(self):
        from dataclasses import replace
        cat = Catalog(synthetic_catalog(self.tmp))
        for value in (float("nan"), float("inf"), -0.01):
            with self.subTest(value=value):
                bad_arms = (replace(ARMS[0], max_run_cost_usd=value),) + ARMS[1:]
                runner, worker = FakeRunner(), FakeFaultWorker()
                with self.assertRaises(Stop):
                    self.camp(cat, runner=runner, worker=worker, arms=bad_arms)
                self.assertEqual(runner.calls, [])
                self.assertEqual(worker.events, [])

    def test_nonfinite_ceiling_is_rejected_before_store_creation(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        for ceiling in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ceiling=ceiling), self.assertRaises(ValueError):
                self.camp(cat, ceiling=ceiling)
            self.assertFalse(self.store.exists())

    def test_invalid_reported_cost_fails_closed_after_restoration(self):
        class InvalidCost(FakeRunner):
            def __init__(self, cost):
                super().__init__()
                self.cost = cost

            def run(self, arm, public_case):
                result = super().run(arm, public_case)
                result.cost_usd = self.cost
                return result

        cat = Catalog(synthetic_catalog(self.tmp))
        for index, cost in enumerate((float("nan"), float("inf"), -0.01)):
            with self.subTest(cost=cost):
                self.store = self.tmp / f"invalid-{index}"
                runner, worker = InvalidCost(cost), FakeFaultWorker()
                out = self.camp(cat, runner=runner, worker=worker)
                self.assertTrue(out.stopped)
                self.assertEqual(out.tally["accepted_pairs"], 0)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(worker.events[-2:], [("restore", "F1-01"), ("release", "")])
                self.assertTrue((self.store / "baseline-quarantine.json").exists())
                with self.assertRaises(Stop):
                    self.camp(cat)

    def test_budget_below_first_reservation_never_injects_or_calls_runner(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        runner, worker = FakeRunner(), FakeFaultWorker()
        out = self.camp(cat, runner=runner, worker=worker, ceiling=ARMS[0].max_run_cost_usd / 2)
        self.assertTrue(out.stopped)
        self.assertEqual(out.tally["accepted_pairs"], 0)
        self.assertEqual(runner.calls, [])
        self.assertEqual(worker.events, [("acquire", ""), ("release", "")])

    def test_stops_before_exceeding_ceiling(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        # ceiling = reservations of the first two arms exactly: the third reservation would cross it.
        ceiling = ARMS[0].max_run_cost_usd + ARMS[1].max_run_cost_usd
        out = self.camp(cat, FakeRunner(cost_fraction=1.0), ceiling=ceiling)
        self.assertTrue(out.stopped)
        self.assertEqual(out.tally["accepted_pairs"], 2)
        self.assertLessEqual(out.tally["spent_usd"], ceiling)
        stop = ProgressStore(self.store / "progress.jsonl", self._mid()).records[-1]
        self.assertEqual((stop["kind"], stop["reason"], stop["next_pair"]),
                         ("stop", "cost_ceiling", ["F1-01", "fixed_multi_agent"]))

    def test_overspend_beyond_reservation_stops(self):
        class Greedy(FakeRunner):
            def run(self, arm, case):
                r = super().run(arm, case)
                r.cost_usd = arm.max_run_cost_usd * 2
                return r
        out = self.camp(Catalog(synthetic_catalog(self.tmp)), Greedy())
        self.assertTrue(out.stopped)
        self.assertIn("reservation", out.reason)
        self.assertEqual(out.tally["attempts"], 1)

    def _mid(self):
        return json.loads((self.store / "manifest.json").read_text())["manifest_id"]


class ResumeTests(Base):
    def test_interrupt_and_resume_skips_completed(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        first = self.camp(cat, max_pairs=3)
        self.assertEqual(first.tally["accepted_pairs"], 3)
        runner = FakeRunner()
        second = self.camp(cat, runner)
        self.assertFalse(second.stopped)
        self.assertEqual(second.tally["accepted_pairs"], 8)
        self.assertEqual(len(runner.calls), 5)
        self.assertNotIn(("F1-01", "single_small"), runner.calls)

    def test_crash_mid_run_marked_interrupted_and_rerun(self):
        cat = Catalog(synthetic_catalog(self.tmp))

        class Crash(FakeRunner):
            def run(self, arm, case):
                if arm.arm_id == "single_high":
                    raise KeyboardInterrupt  # not an Exception: simulates a hard kill
                return super().run(arm, case)
        worker = FakeFaultWorker()
        with self.assertRaises(KeyboardInterrupt):
            self.camp(cat, Crash(), worker)
        self.assertEqual(worker.events[-2:], [("restore", "F1-01"), ("release", "")])  # baseline still restored
        self.assertFalse((self.store / "fault-namespace.lease").exists())
        out = self.camp(cat)
        self.assertEqual(out.tally["accepted_pairs"], 8)
        recs = [json.loads(l) for l in (self.store / "progress.jsonl").read_text().splitlines()]
        interrupted = [r for r in recs if r["kind"] == "done" and r["status"] == "interrupted"]
        self.assertEqual([(r["case_id"], r["arm_id"]) for r in interrupted], [("F1-01", "single_high")])
        self.assertEqual(interrupted[0]["cost_usd"], ARMS[1].max_run_cost_usd)  # unknown spend charged at reserve

    def test_torn_tail_quarantines_without_overwriting_evidence(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        self.camp(cat, max_pairs=2)
        with (self.store / "progress.jsonl").open("a") as fh:
            fh.write('{"kind": "start", "trunc')
        original = (self.store / "progress.jsonl").read_bytes()
        runner = FakeRunner()
        with self.assertRaises(Stop):
            self.camp(cat, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual((self.store / "progress.jsonl").read_bytes(), original)
        self.assertTrue((self.store / "baseline-quarantine.json").exists())
        with self.assertRaises(Stop):
            self.camp(cat)

    def test_manifest_change_refuses_resume(self):
        root = synthetic_catalog(self.tmp)
        self.camp(Catalog(root), max_pairs=1)
        case = json.loads((root / "F1-02.yaml").read_text())
        case["target"] = "order"
        (root / "F1-02.yaml").write_text(json.dumps(case))
        with self.assertRaises(ManifestMismatch):
            self.camp(Catalog(root))

    def test_duplicate_completion_counts_once(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        self.camp(cat)
        mid = json.loads((self.store / "manifest.json").read_text())["manifest_id"]
        st = ProgressStore(self.store / "progress.jsonl", mid)
        s = st.start("F1-01", "adaptive", 0.8, {})
        st.done(s, "completed", 0.1)
        self.assertEqual(st.tally()["accepted_pairs"], 8)
        self.assertEqual(st.attempts("F1-01", "adaptive"), 2)


class SchedulerSafetyTests(Base):
    def test_serial_inject_restore_per_pair(self):
        worker = FakeFaultWorker()
        self.camp(Catalog(synthetic_catalog(self.tmp)), worker=worker)
        body = worker.events[1:-1]
        self.assertEqual(worker.events[0][0], "acquire")
        self.assertEqual(worker.events[-1][0], "release")
        self.assertEqual(len(body), 16)
        for i in range(0, 16, 2):
            self.assertEqual((body[i][0], body[i + 1][0]), ("inject", "restore"))
            self.assertEqual(body[i][1], body[i + 1][1])

    def test_baseline_failure_quarantines(self):
        runner = FakeRunner()
        cat = Catalog(synthetic_catalog(self.tmp))
        out = self.camp(cat, runner, FakeFaultWorker({"F1-01"}))
        self.assertTrue(out.stopped)
        self.assertIn("quarantined", out.reason)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue((self.store / "baseline-quarantine.json").exists())
        with self.assertRaises(Stop):
            self.camp(cat)

    def test_restore_exception_quarantines_and_records_failure(self):
        class BrokenRestore(FakeFaultWorker):
            def restore_baseline(self, public_case):
                raise RuntimeError("cannot confirm baseline")
        cat = Catalog(synthetic_catalog(self.tmp))
        out = self.camp(cat, worker=BrokenRestore())
        self.assertTrue(out.stopped)
        self.assertEqual(out.tally["accepted_pairs"], 0)
        self.assertTrue((self.store / "baseline-quarantine.json").exists())
        with self.assertRaises(Stop):
            self.camp(cat)

    def test_unclean_process_death_quarantines_before_new_injection(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        self.camp(cat, max_pairs=1)
        mid = json.loads((self.store / "manifest.json").read_text())["manifest_id"]
        ProgressStore(self.store / "progress.jsonl", mid).start("F1-01", "single_high", 0.6, {})
        runner = FakeRunner()
        with self.assertRaises(Stop):
            self.camp(cat, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertTrue((self.store / "baseline-quarantine.json").exists())

    def test_run_failure_stops_and_is_retried_on_resume(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        out = self.camp(cat, FakeRunner(fail_on={("F1-01", "fixed_multi_agent")}))
        self.assertTrue(out.stopped)
        self.assertEqual(out.tally["accepted_pairs"], 2)
        out2 = self.camp(cat)
        self.assertEqual(out2.tally["accepted_pairs"], 8)

    def test_second_worker_lease_refused(self):
        self.store.mkdir()
        with NamespaceLease(self.store / "fault-namespace.lease", "other"):
            with self.assertRaises(Stop):
                self.camp(Catalog(synthetic_catalog(self.tmp)))

    def test_real_execution_refused(self):
        cat = Catalog(synthetic_catalog(self.tmp))

        class Real(InferenceRunner):
            runner_id = "real"
        with self.assertRaises(NotAuthorized):
            run_campaign(self.store, cat, Real(), FakeFaultWorker(), cost_ceiling_usd=10, dry_run=True)
        with self.assertRaises(NotAuthorized):
            run_campaign(self.store, cat, FakeRunner(), FakeFaultWorker(), cost_ceiling_usd=10, dry_run=False)

    def test_dry_run_is_reproducible(self):
        cat = Catalog(synthetic_catalog(self.tmp))
        outs = []
        for name in ("a", "b"):
            self.store = self.tmp / name
            self.camp(cat)
            recs = [json.loads(l) for l in (self.store / "progress.jsonl").read_text().splitlines()]
            outs.append([(r["case_id"], r["arm_id"], r.get("output"), r.get("inputs")) for r in recs])
        self.assertEqual(outs[0], outs[1])


class CliTests(Base):
    def test_cli_requires_dry_run(self):
        self.assertEqual(main(["--store", str(self.store), "--cost-ceiling-usd", "5"]), 2)
        self.assertFalse(self.store.exists())

    def test_cli_dry_run_and_ceiling_stop(self):
        root = synthetic_catalog(self.tmp)
        self.assertEqual(main(["--dry-run", "--store", str(self.store), "--cost-ceiling-usd", "100",
                               "--cases-dir", str(root)]), 0)
        self.assertEqual(main(["--dry-run", "--store", str(self.tmp / "s2"), "--cost-ceiling-usd", "0.5",
                               "--cases-dir", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()


class RealExecutionAuthorizationTests(unittest.TestCase):
    """Real execution must be impossible without the approved protocol and impossible with fakes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.cat = Catalog(synthetic_catalog(self.tmp))

    class _Real(InferenceRunner):
        runner_id, is_fake = "real", False

    class _RealWorker(FakeFaultWorker):
        worker_id, is_fake = "real-worker", False

    def _go(self, runner, worker, **kw):
        return run_campaign(self.tmp / "s", self.cat, runner, worker, cost_ceiling_usd=100, dry_run=False, **kw)

    def test_real_without_authorization_refused(self):
        with self.assertRaises(NotAuthorized):
            self._go(self._Real(), self._RealWorker())

    def test_real_with_wrong_hash_refused(self):
        with self.assertRaises(NotAuthorized) as cm:
            self._go(self._Real(), self._RealWorker(), authorization={"approved_by": "x", "protocol_sha256": "0" * 64})
        self.assertIn("sha256", str(cm.exception))

    def test_real_with_fake_side_refused_even_if_authorized(self):
        import hashlib
        from evaluation.harness.cases import REPO
        from evaluation.harness.scheduler import PROTOCOL_FILE
        auth = {"approved_by": "x", "protocol_sha256": hashlib.sha256((REPO / PROTOCOL_FILE).read_bytes()).hexdigest()}
        with self.assertRaises(NotAuthorized):
            self._go(FakeRunner(), self._RealWorker(), authorization=auth)
        with self.assertRaises(NotAuthorized):
            self._go(self._Real(), FakeFaultWorker(), authorization=auth)

    def test_authorization_problem_none_for_current_protocol(self):
        import hashlib
        from evaluation.harness.cases import REPO
        from evaluation.harness.scheduler import PROTOCOL_FILE, authorization_problem
        auth = {"approved_by": "x", "protocol_sha256": hashlib.sha256((REPO / PROTOCOL_FILE).read_bytes()).hexdigest()}
        self.assertIsNone(authorization_problem(auth))


# ------------------------------------------------------------------ policy C: harness restores what the agent changed
def _worker_with_state(before, now, calls):
    from evaluation.harness import real

    class C:
        def obj(self, kind, name):
            s = now[name]
            ann = {"kubectl.kubernetes.io/restartedAt": s["restartedAt"]} if s["restartedAt"] else {}
            return {"spec": {"replicas": s["replicas"], "template": {"metadata": {"annotations": ann}, "spec": {"containers": [
                {"name": name, "resources": {"limits": {"memory": s["memory"]}}}]}}}}

        def kubectl(self, *a, **k):
            calls.append(a)
            return ""
    w = real.K8sFaultWorker.__new__(real.K8sFaultWorker)
    w.c, w.spec_before = C(), before
    return w


_S = {"inventory": {"memory": "512Mi", "replicas": 1, "restartedAt": None},
      "order": {"memory": "512Mi", "replicas": 1, "restartedAt": None}}


def test_restore_agent_spec_is_noop_when_unchanged():
    calls = []
    w = _worker_with_state(_S, _S, calls)
    w._restore_agent_spec()
    assert [c for c in calls if c[0] != "rollout"] == []


def test_restore_agent_spec_reverts_memory_replicas_and_restart_annotation():
    now = {"inventory": {"memory": "768Mi", "replicas": 2, "restartedAt": "2026-10-07T00:00:00Z"}, "order": dict(_S["order"])}
    calls = []
    _worker_with_state(_S, now, calls)._restore_agent_spec()
    writes = [c for c in calls if c[0] != "rollout"]
    assert ("set", "resources", "deployment/inventory", "-c", "inventory", "--limits=memory=512Mi") in writes
    assert ("scale", "deployment/inventory", "--replicas=1") in writes
    assert any(c[0] == "patch" and "restartedAt" in c[-1] for c in writes)
    assert not [c for c in writes if "order" in " ".join(c)]            # untouched workloads are not written


def test_hold_stops_rearming_after_the_workload_changes(monkeypatch):
    """Policy C: after the agent restarts/deletes a pod or patches the Deployment, the harness must not re-inject."""
    from evaluation.harness import real
    monkeypatch.setattr(real, "MAX_REARM_S", 30)
    state = {"gen": 5, "uids": ("a",), "mode": "off"}
    posts = []

    class C:
        def obj(self, kind, name):
            return {"metadata": {"generation": state["gen"]}}

        def pod_status(self, t):
            return [("p", u, {}) for u in state["uids"]]
    w = real.K8sFaultWorker.__new__(real.K8sFaultWorker)
    w.c = C()
    w._cx = lambda t, m, path, body=None: (posts.append(path) or (200, {})) if m == "POST" else (200, {"mode": state["mode"]})
    import time as _t
    w._start_hold("inventory", {"mode": "pool_exhaust"})
    _t.sleep(4.5)
    assert posts, "fault expired with the workload unchanged -> re-armed"
    state["uids"] = ("b",)          # agent deleted the pod / restarted the rollout
    n = len(posts)
    _t.sleep(4.5)
    w._stop_hold()
    assert len(posts) == n and w.rearm_stopped_by_change is True


def test_p13_arms_differ_only_in_rag_and_keep_original_hashes():
    import json
    from pathlib import Path
    from evaluation.harness.arms import ARMS, P13_ARMS, validate_arms
    assert validate_arms() == [] and validate_arms(P13_ARMS) == []
    a, b = P13_ARMS
    assert a.roles == b.roles == next(x for x in ARMS if x.arm_id == "adaptive").roles
    assert "reference_guide" not in a.rag_kinds and "reference_guide" in b.rag_kinds
    # the four original arms keep their pinned hashes (stores from 10-05..10-11 still bind)
    store = Path(__file__).resolve().parents[2] / ".e2e-tmp" / "revamp-P12" / "store-policyC" / "manifest.json"
    if store.exists():
        pinned = json.loads(store.read_text())["arms"]
        assert {x.arm_id: x.spec_hash() for x in ARMS} == pinned


def test_p13_runner_passes_rag_kinds():
    from evaluation.harness.arms import ARMS, P13_ARMS
    from evaluation.harness import real
    sent = {}

    class R(real.CodexInClusterRunner):
        def __init__(self):
            self.timeout_s = 10

        def lock(self, *_):
            pass

        def _exec(self, *argv, timeout=None):
            sent["argv"] = argv
            raise real.subprocess.TimeoutExpired("x", 1)
    case = {"id": "N2-01", "target": "order", "expected_signal": "oom_killed"}
    R().run(P13_ARMS[1], case)
    assert json.loads(sent["argv"][sent["argv"].index("--arm") + 1])["rag_kinds"][-1] == "reference_guide"
    R().run(ARMS[0], case)
    assert "rag_kinds" not in json.loads(sent["argv"][sent["argv"].index("--arm") + 1])
