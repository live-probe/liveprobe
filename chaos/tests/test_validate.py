import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate import ROOT, check, validate_catalog  # noqa: E402


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.good = json.loads((ROOT / "F1-01.yaml").read_text())

    def test_catalog(self):
        errors, counts = validate_catalog()
        self.assertEqual(errors, [])
        self.assertEqual(dict(counts), {"image_crash": 15, "oom": 15, "latency_pool": 15, "endpoint_loss": 15})

    def test_missing_field(self):
        del self.good["ground_truth"]
        self.assertTrue(check(self.good))

    def test_unsafe_target_and_payload(self):
        self.good["target"] = "kube-system"
        self.good["injection"]["parameters"] = {"command": "kubectl delete node"}
        errors = check(self.good)
        self.assertTrue(any("target" in e for e in errors))
        self.assertTrue(any("parameters" in e for e in errors))

    def test_unbounded_parameter(self):
        self.good["injection"] = {"mode": "controlled_crash", "parameters": {"duration_seconds": 3600}}
        self.assertTrue(any("bounded" in e for e in check(self.good)))

    def test_duplicate_and_wrong_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for src in ROOT.glob("*.yaml"):
                (root / src.name).write_bytes(src.read_bytes())
            case = json.loads((root / "F1-01.yaml").read_text())
            (root / "F1-02.yaml").write_text(json.dumps(case))
            errors, _ = validate_catalog(root)
            self.assertTrue(any("duplicate id" in e for e in errors))
            (root / "F4-15.yaml").unlink()
            errors, _ = validate_catalog(root)
            self.assertTrue(any("expected 15" in e for e in errors))

    def test_label_change_requires_new_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for src in list(ROOT.glob("*.yaml")) + [ROOT / "LABELS.json"]:
                (root / src.name).write_bytes(src.read_bytes())
            case = json.loads((root / "F2-11.yaml").read_text())
            case["reference_workflow"] = "moderate"
            (root / "F2-11.yaml").write_text(json.dumps(case))
            errors, _ = validate_catalog(root)
            self.assertTrue(any("F2-11 labels changed" in e for e in errors))
            (root / "LABELS.json").unlink()
            errors, _ = validate_catalog(root)
            self.assertTrue(any("missing approved label manifest" in e for e in errors))

    def test_manifest_is_approved_v1(self):
        manifest = json.loads((ROOT / "LABELS.json").read_text())
        self.assertEqual(manifest["status"], "approved")
        self.assertGreaterEqual(manifest["label_version"], 1)
        self.assertEqual(len(manifest["cases"]), 60)

    def test_catalog_not_in_any_image_build_context(self):
        # Ground truth must never be copied into LiveProbe/dashboard/service images or manifests.
        repo = ROOT.parents[1]
        for ctx in ("liveprobe", "dashboard", "services/order", "services/inventory"):
            self.assertFalse(str(ROOT.resolve()).startswith(str((repo / ctx).resolve())), ctx)
        for path in list((repo / "deploy").rglob("*.yaml")) + list((repo / "deploy").rglob("*.yml")):
            text = path.read_text(errors="ignore")
            self.assertNotIn("ground_truth", text, str(path))
            self.assertNotIn("chaos/cases", text, str(path))


if __name__ == "__main__":
    unittest.main()
