#!/usr/bin/env python3
"""Regression checks for source inclusion in the split GitHub repositories."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SourceIgnoreTests(unittest.TestCase):
    def test_java_package_named_target_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
            source = "services/order/src/main/java/io/liveprobe/target/order/OrderController.java"
            result = subprocess.run(
                ["git", "check-ignore", source], cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, "Java package source must be publishable")

    def test_maven_outputs_stay_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
            result = subprocess.run(
                ["git", "check-ignore", "services/order/target/app.jar"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)

    def test_target_deployment_manifests_are_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text())
            result = subprocess.run(
                ["git", "check-ignore", "deploy/base/target/target.yaml"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
