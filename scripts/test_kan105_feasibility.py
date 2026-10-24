"""Synthetic records test fail-closed behavior, not a cluster or capability."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("kan105", HERE / "kan105_feasibility.py")
assert spec is not None and spec.loader is not None
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

SOURCES = {"crash_waiting": "kubernetes_status", "restart_count": "kubernetes_status",
           "cgroup_oom_killed": "kubernetes_status", "cgroup_memory_event": "cgroup_metrics", "latency_samples": "http_probe",
           "physical_pool_occupied": "db_metrics", "db_wait_observed": "db_metrics",
           "zero_ready_endpoints": "kubernetes_status", "upstream_http_5xx": "http_probe",
           "image_pull_observed": "kubernetes_status"}


def fixture(cid):
    c = harness.expected(cid)
    duration = c["value"] if c["parameter"] == "duration_seconds" else 10
    obs = {}
    for kind in harness.REQUIRED[c["signal"]]:
        value = {"restart_count": 2, "cgroup_oom_killed": 137, "cgroup_memory_event": 1, "latency_samples": (c["value"] + 30) if isinstance(c["value"], int) else 1,
                 "physical_pool_occupied": c["value"], "db_wait_observed": 1,
                 "zero_ready_endpoints": 0, "upstream_http_5xx": 502}.get(kind, 1)
        obs[kind] = {"source": SOURCES[kind], "sample_count": 10 if kind == "latency_samples" else (3 if kind == "restart_count" else (2 if c["signal"] == "upstream_5xx" else 1)),
                     "first_second": 0.2, "last_second": duration - 0.1, "observed": True,
                     "value": value, "baseline_value": c["value"] if kind == "physical_pool_occupied" else (30 if kind == "latency_samples" else 0)}
        if kind == "restart_count":
            obs[kind]["snapshots"] = [
                {"second": second, "count": count, "container_hash": "a" * 64}
                for second, count in ((0.2, 0), (duration / 2, 1), (duration - 0.1, 2))]
        if kind == "latency_samples":
            obs[kind]["pairs"] = [
                {"baseline_second": -10 + i, "active_second": (duration - 0.1 if i == 9 else 0.2 + i * (duration - 0.3) / 9),
                 "baseline_ms": 30, "active_ms": value, "baseline_status": 200, "active_status": 200}
                for i in range(10)]
    return c, {"case_id": cid, "target": c["target"], "window_seconds": duration,
               "observations": obs,
               "recovery": {key: True for key in (*harness.BASELINE, "snapshot_restored", "default_profile_404", "pool_released")}}


class FeasibilityTests(unittest.TestCase):
    def test_exact_catalog_and_missing_observation(self):
        errors, contracts = harness.audit()
        self.assertEqual(errors, [])
        self.assertEqual(len(contracts), 60)
        self.assertEqual({c["id"] for c in contracts}, {f"F{f}-{n:02}" for f in range(1, 5) for n in range(1, 16)})
        self.assertEqual(sum(c["target"] == "inventory" for c in contracts), 32)
        for c in contracts:
            self.assertEqual(harness.evaluate(c, None)[0], "UNVERIFIED")
            _, record = fixture(c["id"])
            self.assertEqual(harness.evaluate(c, record)[0], "EVIDENCE_CONSISTENT_NOT_CAPABILITY")
            del record["observations"][harness.REQUIRED[c["signal"]][0]]
            self.assertEqual(harness.evaluate(c, record)[0], "UNVERIFIED")

    def test_independent_contract_catches_schema_valid_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for src in harness.ROOT.glob("*.yaml"):
                (root / src.name).write_bytes(src.read_bytes())
            (root / "LABELS.json").write_bytes((harness.ROOT / "LABELS.json").read_bytes())
            case = json.loads((root / "F3-02.yaml").read_text())
            case["injection"]["parameters"]["connections"] = 3
            (root / "F3-02.yaml").write_text(json.dumps(case))
            errors, _ = harness.audit(root)
            self.assertTrue(any("F3-02: immutable" in e for e in errors))

    def test_critical_false_positives(self):
        tests = [("F1-02", "restart_count", "value", 1), ("F2-01", "cgroup_oom_killed", "value", 1),
                 ("F2-01", "cgroup_memory_event", "value", 0),
                 ("F3-01", "latency_samples", "sample_count", 1), ("F3-01", "latency_samples", "value", 40),
                 ("F3-02", "physical_pool_occupied", "baseline_value", 5),
                 ("F3-06", "physical_pool_occupied", "value", 5),
                 ("F3-04", "db_wait_observed", "observed", False),
                 ("F4-02", "zero_ready_endpoints", "value", 1),
                 ("F4-04", "upstream_http_5xx", "value", 200)]
        for cid, kind, field, wrong in tests:
            with self.subTest(cid=cid, kind=kind, field=field):
                c, record = fixture(cid)
                record["observations"][kind][field] = wrong
                self.assertEqual(harness.evaluate(c, record)[0], "UNVERIFIED")

    def test_single_restart_snapshot_and_identity_drift(self):
        c, record = fixture("F1-02")
        for mutation in (
            lambda r: r["observations"]["restart_count"].update(
                snapshots=[{"second": 0.2, "count": 2, "container_hash": "a" * 64}],
                sample_count=1, first_second=0.2, last_second=0.2),
            lambda r: r["observations"]["restart_count"]["snapshots"][2].update(container_hash="b" * 64),
            lambda r: r["observations"]["restart_count"]["snapshots"][1].update(count=2),
        ):
            bad = copy.deepcopy(record)
            mutation(bad)
            self.assertEqual(harness.evaluate(c, bad)[0], "UNVERIFIED")

    def test_disjoint_endpoint_and_upstream_intervals(self):
        c, record = fixture("F4-02")
        record["observations"]["zero_ready_endpoints"].update(first_second=0.2, last_second=2)
        record["observations"]["upstream_http_5xx"].update(first_second=3, last_second=9.9)
        self.assertEqual(harness.evaluate(c, record)[0], "UNVERIFIED")

    def test_latency_requires_actual_timed_baseline_pairs(self):
        c, record = fixture("F3-01")
        for mutation in (
            lambda r: r["observations"]["latency_samples"].pop("pairs"),
            lambda r: r["observations"]["latency_samples"].update(first_second=0.2, last_second=0.2),
            lambda r: r["observations"]["latency_samples"]["pairs"][0].update(baseline_second=0.2),
            lambda r: r["observations"]["latency_samples"]["pairs"][0].update(active_status=503),
        ):
            bad = copy.deepcopy(record)
            mutation(bad)
            self.assertEqual(harness.evaluate(c, bad)[0], "UNVERIFIED")

    def test_bounds_source_recovery_and_sanitization(self):
        c, record = fixture("F1-02")
        for mutation in (lambda r: r.update(window_seconds=8),
                         lambda r: r["observations"]["crash_waiting"].update(last_second=8),
                         lambda r: r["observations"]["crash_waiting"].update(source="http_probe"),
                         lambda r: r["observations"]["crash_waiting"].update(token="secret"),
                         lambda r: r["recovery"].update(snapshot_restored=False),
                         lambda r: r.update(target="order")):
            bad = copy.deepcopy(record)
            mutation(bad)
            self.assertEqual(harness.evaluate(c, bad)[0], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
