#!/usr/bin/env python3
"""Count exact private Claude token occurrences in live logs, DB and API response.

Prints counts only. Never emits token content or raw source text.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location("update_claude_secret", Path(__file__).with_name("update-claude-secret.py"))
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def check(args: list[str], token: bytes) -> tuple[int, int]:
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"source query failed ({result.returncode})")
    return 1, result.stdout.count(token)


def main() -> None:
    if len(sys.argv) != 3:
        raise ValueError("usage: scan-claude-token.py TOKEN_ENV_FILE API_RESPONSE_FILE")
    token = module.parse_assignment(Path(sys.argv[1]).read_bytes())
    base = ["kubectl", "--context", "rancher-desktop"]
    counts = {"logs": [0, 0], "db": [0, 0], "api": [1, 0]}
    for ns in ("liveprobe-system", "liveprobe-observability", "liveprobe-target"):
        pods = subprocess.run(base + ["-n", ns, "get", "pods", "-o", "jsonpath={.items[*].metadata.name}"], capture_output=True, check=True).stdout.split()
        for pod in pods:
            for previous in (False, True):
                args = base + ["-n", ns, "logs", pod.decode(), "--all-containers=true"]
                if previous:
                    args.append("--previous")
                result = subprocess.run(args, capture_output=True, check=False)
                if result.returncode == 0:
                    counts["logs"][0] += 1
                    counts["logs"][1] += result.stdout.count(token)
    db = base + ["-n", "liveprobe-system", "exec", "postgres-0", "--", "sh", "-c"]
    tables = subprocess.run(db + ['psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select table_name from information_schema.tables where table_schema=\'public\'"'], capture_output=True, check=True).stdout.split()
    for table in tables:
        if not table.replace(b"_", b"").isalnum():
            raise ValueError("unexpected DB table identifier")
        n, hits = check(db + [f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select t::text from {table.decode()} t"'], token)
        counts["db"][0] += n
        counts["db"][1] += hits
    counts["api"][1] = Path(sys.argv[2]).read_bytes().count(token)
    for name, (sources, hits) in counts.items():
        print(f"{name}: sources={sources} exact_token_hits={hits}")
    if any(hits for _, hits in counts.values()):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError):
        print("scan incomplete; source query failed (no credential output)", file=sys.stderr)
        sys.exit(2)
