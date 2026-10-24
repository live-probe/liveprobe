"""Self-test for scan-secrets.py classifier (synthetic, non-credential strings)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("scan", Path(__file__).with_name("scan-secrets.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

cases = {
    "oauth_access_token_expiry=5": "identifier_fragment",
    "access_token=" + "x" * 24: "token_value",
    "sk-" + "a" * 30: "token_value",
    "access_token: <redacted>": "key_only",
    "disk-usage task-1": "identifier_fragment",
}
for text, expected in cases.items():
    got = {k for k, v in scan.classify(text).items() if v}
    assert got == {expected}, (expected, got)
print("scan-secrets classifier self-test: OK", len(cases), "cases")
