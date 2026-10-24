"""KAN-59 synthetic-only regression cases; these are not recorded campaign results."""
from __future__ import annotations

from evaluation.tests.test_metrics import Base, M, baseline_runs


class IncompleteMetricsTests(Base):
    def test_no_scored_workflow_has_null_numerator(self):
        runs = [(cid, arm, status, cost,
                 {k: v for k, v in output.items() if k != "selected_workflow"}
                 if arm == "adaptive" else output)
                for cid, arm, status, cost, output in baseline_runs()]
        metric = M(self.report(runs), "workflow_match", "adaptive")
        self.assertEqual(metric["scored"], 0)
        self.assertIsNone(metric["value"])
        self.assertIsNone(metric["numerator"])
        self.assertEqual(metric["verdict"], "unknown")

    def test_unchecked_evidence_never_passes_threshold(self):
        runs = [(cid, arm, status, cost,
                 {**output, "evidence_claims": [{"claim_id": "verified", "verdict": "consistent"}] * 99
                  + [{"claim_id": "unverified", "verdict": "unchecked"}]}
                 if arm == "adaptive" and cid == "F1-01" else output)
                for cid, arm, status, cost, output in baseline_runs()]
        metric = M(self.report(runs), "evidence_consistency", "adaptive")
        self.assertEqual(metric["claims"]["unchecked"], 2)
        self.assertEqual(metric["verdict"], "unknown")

    def test_real_mode_without_scoring_schema_cannot_be_published(self):
        runs = [(cid, arm, status, cost, {}) for cid, arm, status, cost, _ in baseline_runs()]
        report = self.report(runs, mode="real")
        self.assertFalse(report["publishable"])
        self.assertTrue(any("unscored" in reason for reason in report["publication_blockers"]))

    def test_no_scored_evidence_has_null_numerator(self):
        runs = [(cid, arm, status, cost,
                 {k: v for k, v in output.items() if k != "evidence_claims"}
                 if arm == "adaptive" else output)
                for cid, arm, status, cost, output in baseline_runs()]
        metric = M(self.report(runs), "evidence_consistency", "adaptive")
        self.assertEqual(metric["scored"], 0)
        self.assertIsNone(metric["numerator"])
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["verdict"], "unknown")
