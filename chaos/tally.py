#!/usr/bin/env python3
"""KAN-48 evidence-only tally; never emits case labels, tokens or raw response bodies."""
import argparse
from collections import Counter
import json
from pathlib import Path

from validate import ROOT, validate_catalog


def latest(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        cid = record.get("case", "")
        if cid.startswith("F") and cid[2:3] == "-":
            result[cid] = record
    return result


def tally(target_path, runner_path):
    errors, counts = validate_catalog()
    if errors:
        raise ValueError("catalog validation failed")
    target = latest(target_path)
    runner = latest(runner_path)
    statuses = {}
    for path in sorted(ROOT.glob("F*.yaml")):
        case = json.loads(path.read_text(encoding="utf-8"))
        cid = case["id"]
        owner = "runner" if case["injection"]["mode"] in ("bad_image", "selector_mismatch") else "target"
        record = (runner if owner == "runner" else target).get(cid)
        verified = (record is not None and record.get("status") == "PASS" and
                    (record.get("baseline_restored") is True if owner == "runner"
                     else record.get("post_health_ok") is True))
        statuses[cid] = {"owner": owner, "verified": verified}
    return {"catalog": dict(counts), "unique_cases": len(statuses),
            "verified_by_owner": dict(Counter(v["owner"] for v in statuses.values() if v["verified"])),
            "unverified_ids": [cid for cid, value in statuses.items() if not value["verified"]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-results", type=Path, required=True)
    parser.add_argument("--runner-results", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(tally(args.target_results, args.runner_results), sort_keys=True))
