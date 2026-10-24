"""MVP-0 injection contract tests; kubectl is replaced, no cluster calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

INJECT = Path(__file__).resolve().parents[1] / "inject.sh"


class InjectTests(unittest.TestCase):
    def test_family1_uses_fresh_nonexistent_tag_each_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kubectl = root / "kubectl"
            kubectl.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                               "with open(os.environ['KUBECTL_LOG'], 'a') as f:\n"
                               "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n")
            kubectl.chmod(0o755)
            uuidgen = root / "uuidgen"
            uuidgen.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_UUID\"\n")
            uuidgen.chmod(0o755)
            log = root / "kubectl.jsonl"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", KUBECTL_LOG=str(log))
            for token in ("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"):
                run = subprocess.run(["bash", str(INJECT), "family1"], env=dict(env, FAKE_UUID=token),
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            for call, marker in zip(calls, ("a", "b")):
                self.assertEqual(call, ["--context", "rancher-desktop", "-n", "liveprobe-target",
                                        "set", "image", "deploy/inventory",
                                        f"inventory=liveprobe/inventory:does-not-exist-{marker * 32}"])
            self.assertNotEqual(calls[0][-1], calls[1][-1])


if __name__ == "__main__":
    unittest.main()
