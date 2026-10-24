"""Offline, read-only v1 catalog audit and evidence consistency checker.

No Kubernetes client, network calls, injection, or filesystem writes. An evidence
file is an untrusted operator-supplied record, never proof of cluster capability.
"""
import argparse
from collections import Counter
import json
import math
import statistics
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1] / "chaos" / "cases"
BASELINE = ("deployments_ready", "services_endpoints_present", "order_check_200")
MODES = {
    "bad_image": ("image_pull", "runner_snapshot", "tag"),
    "controlled_crash": ("crash_loop", "target_supervisor", "duration_seconds"),
    "controlled_oom": ("oom_killed", "target_supervisor", "duration_seconds"),
    "controlled_latency": ("slow_response", "inventory_injector" , "delay_ms"),
    "controlled_pool_hold": ("pool_exhaustion", "physical_db_contract", "connections"),
    "selector_mismatch": ("no_endpoints", "runner_snapshot", "selector_value"),
    "controlled_endpoint_off": ("upstream_5xx", "target_supervisor", "duration_seconds"),
}
FAMILY = {1: "image_crash", 2: "oom", 3: "latency_pool", 4: "endpoint_loss"}
REQUIRED = {
    "image_pull": ("image_pull_observed",),
    "crash_loop": ("crash_waiting", "restart_count"),
    "oom_killed": ("cgroup_oom_killed", "cgroup_memory_event"),
    "slow_response": ("latency_samples",),
    "pool_exhaustion": ("physical_pool_occupied", "db_wait_observed"),
    "no_endpoints": ("zero_ready_endpoints",),
    "upstream_5xx": ("zero_ready_endpoints", "upstream_http_5xx"),
}
# Proposal, not approved thresholds: require repeated observation and a matching
# before/after control sample. The 100 ms case is never inferred from one request.
MIN_SAMPLES = 10
ALLOWED_OBS = {"image_pull_observed", "crash_waiting", "restart_count", "cgroup_oom_killed", "cgroup_memory_event",
               "latency_samples", "physical_pool_occupied", "db_wait_observed",
               "zero_ready_endpoints", "upstream_http_5xx"}


def expected(cid):
    """Independent v1 contract: detect drift even when YAML remains schema-valid."""
    f, n = int(cid[1]), int(cid[3:])
    target = "inventory" if n % 4 in (1, 2) else "order"
    if f == 1:
        mode = "bad_image" if n % 2 else "controlled_crash"
        value = f"liveprobe/{target}:does-not-exist-{n:02}" if n % 2 else n + 5
    elif f == 2:
        mode = "controlled_oom"
        value = n + 5
    elif f == 3:
        mode = "controlled_latency" if n % 2 else "controlled_pool_hold"
        value = n * 100 if n % 2 else {2: 2, 4: 4, 6: 6, 8: 8, 10: 2, 12: 4, 14: 6}[n]
    else:
        mode = "selector_mismatch" if n % 2 else "controlled_endpoint_off"
        value = f"{target}-broken-{n:02}" if n % 2 else n + 5
    signal, owner, param = MODES[mode]
    if mode == "controlled_latency" and target == "order":
        owner = "order_target_contract"
    return {"id": cid, "family": FAMILY[f], "target": target, "mode": mode,
            "parameter": param, "value": value, "signal": signal, "owner": owner,
            "recovery": "restore_snapshot_and_verify", "baseline": list(BASELINE)}


def audit(root=ROOT):
    errors, records = [], []
    ids = [f"F{f}-{n:02}" for f in range(1, 5) for n in range(1, 16)]
    paths = sorted(root.glob("*.yaml"))
    if {p.stem for p in paths} != set(ids) or len(paths) != 60:
        errors.append("catalog IDs/count differ from immutable v1 60")
    try:
        manifest = json.loads((root / "LABELS.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "approved" or manifest.get("label_version") != 1 or set(manifest.get("cases", {})) != set(ids):
            errors.append("approved LABELS v1 IDs/status mismatch")
    except (OSError, ValueError, TypeError):
        errors.append("LABELS manifest unreadable")
    # Reuse existing validator for full schema and approved label digests.
    spec = importlib.util.spec_from_file_location("kan105_catalog_validator", ROOT.parent / "validate.py")
    if spec is None or spec.loader is None:
        return ["catalog validator unavailable"], records
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validation_errors, _ = validator.validate_catalog(root)
    if validation_errors:
        errors.append(f"catalog schema/label validation failed ({len(validation_errors)} errors)")
    for cid in ids:
        path = root / f"{cid}.yaml"
        if not path.is_file():
            continue
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
            contract = expected(cid)
            actual = {"id": case["id"], "family": case["family"], "target": case["target"],
                      "mode": case["injection"]["mode"],
                      "parameter": next(iter(case["injection"]["parameters"])),
                      "value": next(iter(case["injection"]["parameters"].values())),
                      "signal": case["expected_signal"], "recovery": case["recovery"],
                      "baseline": case["baseline_checks"]}
            if any(actual[k] != contract[k] for k in actual):
                errors.append(f"{cid}: immutable contract mismatch")
            records.append(contract)
        except (OSError, ValueError, TypeError, KeyError, StopIteration):
            errors.append(f"{cid}: unreadable case contract")
    return errors, records


def valid_restart_snapshots(item, window):
    """Require a baseline and two distinct increments of one opaque container."""
    rows = item["snapshots"]
    if not isinstance(rows, list) or len(rows) != item["sample_count"] or len(rows) < 3:
        return False
    previous_time, previous_count, identity = -1, None, None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"second", "count", "container_hash"}:
            return False
        t, count, container = row["second"], row["count"], row["container_hash"]
        if (type(t) not in (int, float) or not math.isfinite(t) or not previous_time < t <= window or
                type(count) is not int or count < 0 or
                not isinstance(container, str) or len(container) != 64 or
                any(ch not in "0123456789abcdef" for ch in container)):
            return False
        if identity is not None and (container != identity or previous_count is None or count < previous_count or count - previous_count > 1):
            return False
        previous_time, previous_count, identity = t, count, container
    return (rows[0]["second"] == item["first_second"] and rows[-1]["second"] == item["last_second"] and
            rows[0]["count"] == item["baseline_value"] and rows[-1]["count"] == item["value"] and
            rows[-1]["count"] - rows[0]["count"] >= 2)


def valid_latency_pairs(item, window):
    """Bound ten pre-fault/active measurements, not just an asserted median."""
    pairs = item["pairs"]
    if not isinstance(pairs, list) or len(pairs) != item["sample_count"] or len(pairs) < MIN_SAMPLES:
        return False
    baseline_times, active_times, baseline_ms, active_ms = [], [], [], []
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {"baseline_second", "active_second", "baseline_ms", "active_ms", "baseline_status", "active_status"}:
            return False
        bt, at = pair["baseline_second"], pair["active_second"]
        bm, am = pair["baseline_ms"], pair["active_ms"]
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (bt, at, bm, am)) or
                not -30 <= bt < 0 or not 0 <= at <= window or
                not 0 <= bm <= 100000 or not 0 <= am <= 100000 or
                type(pair["baseline_status"]) is not int or pair["baseline_status"] != 200 or
                type(pair["active_status"]) is not int or pair["active_status"] != 200):
            return False
        baseline_times.append(bt)
        active_times.append(at)
        baseline_ms.append(bm)
        active_ms.append(am)
    return (all(a < b for a, b in zip(baseline_times, baseline_times[1:])) and
            all(a < b for a, b in zip(active_times, active_times[1:])) and
            active_times[0] == item["first_second"] and active_times[-1] == item["last_second"] and
            statistics.median(baseline_ms) == item["baseline_value"] and
            statistics.median(active_ms) == item["value"])


def evaluate(contract, evidence):
    """Check supplied observations, never infer capability from catalog or fixtures."""
    if evidence is None:
        return "UNVERIFIED", ["no observed evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"case_id", "target", "window_seconds", "observations", "recovery"}:
        return "UNVERIFIED", ["invalid evidence envelope"]
    if evidence["case_id"] != contract["id"] or evidence["target"] != contract["target"]:
        return "UNVERIFIED", ["case/target mismatch"]
    window = evidence["window_seconds"]
    if type(window) is not int or not 1 <= window <= 30:
        return "UNVERIFIED", ["window must be bounded 1..30s"]
    if contract["parameter"] == "duration_seconds" and window != contract["value"]:
        return "UNVERIFIED", ["duration window does not match v1"]
    obs = evidence["observations"]
    if not isinstance(obs, dict) or set(obs) - ALLOWED_OBS:
        return "UNVERIFIED", ["invalid observation kinds"]
    missing = [kind for kind in REQUIRED[contract["signal"]] if kind not in obs]
    if missing:
        return "UNVERIFIED", ["missing observed signal: " + ", ".join(missing)]
    # Every individual signal is source-bound to the case and window. No raw logs,
    # bearer tokens, network targets, or arbitrary payloads enter this format.
    for kind in REQUIRED[contract["signal"]]:
        item = obs[kind]
        fields = {"source", "sample_count", "first_second", "last_second", "observed", "value", "baseline_value"}
        if kind == "restart_count":
            fields.add("snapshots")
        if kind == "latency_samples":
            fields.add("pairs")
        if not isinstance(item, dict) or set(item) != fields:
            return "UNVERIFIED", ["invalid observation shape"]
        if item["source"] not in ("kubernetes_status", "http_probe", "db_metrics", "cgroup_metrics"):
            return "UNVERIFIED", ["unknown observation source"]
        if type(item["sample_count"]) is not int or item["sample_count"] < 1:
            return "UNVERIFIED", ["invalid sample count"]
        first, last = item["first_second"], item["last_second"]
        if any(type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= window for t in (first, last)) or first > last:
            return "UNVERIFIED", ["observation outside active window"]
        if item["observed"] is not True:
            return "UNVERIFIED", ["required signal not observed"]
        if any(type(item[k]) not in (int, float) or not math.isfinite(item[k]) or not 0 <= item[k] <= 100000 for k in ("value", "baseline_value")):
            return "UNVERIFIED", ["invalid numeric measurement"]
    signal = contract["signal"]
    sources = {kind: obs[kind]["source"] for kind in REQUIRED[signal]}
    required_sources = {"image_pull_observed": "kubernetes_status", "crash_waiting": "kubernetes_status",
                        "restart_count": "kubernetes_status", "cgroup_oom_killed": "kubernetes_status",
                        "cgroup_memory_event": "cgroup_metrics",
                        "latency_samples": "http_probe", "physical_pool_occupied": "db_metrics",
                        "db_wait_observed": "db_metrics", "zero_ready_endpoints": "kubernetes_status",
                        "upstream_http_5xx": "http_probe"}
    if any(sources[k] != required_sources[k] for k in sources):
        return "UNVERIFIED", ["wrong source for required signal"]
    if signal == "image_pull" and obs["image_pull_observed"]["value"] != 1:
        return "UNVERIFIED", ["image pull waiting reason required"]
    if signal == "crash_loop" and (not valid_restart_snapshots(obs["restart_count"], window) or
                                   obs["crash_waiting"]["value"] != 1):
        return "UNVERIFIED", ["two same-container restart increments and CrashLoopBackOff waiting required"]
    if signal == "oom_killed" and (obs["cgroup_oom_killed"]["value"] != 137 or
                                   obs["cgroup_memory_event"]["value"] - obs["cgroup_memory_event"]["baseline_value"] < 1):
        return "UNVERIFIED", ["OOMKilled exit 137 and increasing cgroup oom_kill counter required"]
    if signal == "slow_response" and (not valid_latency_pairs(obs["latency_samples"], window) or
                                      obs["latency_samples"]["value"] - obs["latency_samples"]["baseline_value"] < contract["value"]):
        return "UNVERIFIED", ["at least 10 timed baseline/active HTTP pairs and median delay >= v1 delay required"]
    if signal == "pool_exhaustion" and (obs["physical_pool_occupied"]["value"] != contract["value"] or
                                        obs["physical_pool_occupied"]["baseline_value"] != contract["value"] or
                                        obs["db_wait_observed"]["value"] < 1 or
                                        obs["db_wait_observed"]["baseline_value"] != 0):
        return "UNVERIFIED", ["physical occupied = configured capacity = v1 connections, plus real DB wait required"]
    if signal in ("no_endpoints", "upstream_5xx") and obs["zero_ready_endpoints"]["value"] != 0:
        return "UNVERIFIED", ["ready EndpointSlice addresses must be zero"]
    if signal == "upstream_5xx":
        endpoint, http = obs["zero_ready_endpoints"], obs["upstream_http_5xx"]
        if not 500 <= http["value"] <= 599:
            return "UNVERIFIED", ["upstream HTTP status must be 5xx"]
        if (endpoint["sample_count"] < 2 or http["sample_count"] < 2 or
                max(endpoint["first_second"], http["first_second"]) >=
                min(endpoint["last_second"], http["last_second"])):
            return "UNVERIFIED", ["zero endpoints and upstream 5xx observation intervals must overlap"]
    recovery = evidence["recovery"]
    if not isinstance(recovery, dict) or set(recovery) != set(BASELINE) | {"snapshot_restored", "default_profile_404", "pool_released"} or any(v is not True for v in recovery.values()):
        return "UNVERIFIED", ["snapshot, baseline and isolation recovery not all observed"]
    return "EVIDENCE_CONSISTENT_NOT_CAPABILITY", ["untrusted offline record; independent isolated-cluster verification required"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, help="optional sanitized JSON object keyed by case ID (read only)")
    parser.add_argument("--summary", action="store_true", help="counts only; omit per-case mapping")
    args = parser.parse_args()
    errors, contracts = audit(args.cases)
    if errors:
        print(json.dumps({"status": "INVALID_CATALOG", "errors": errors}, ensure_ascii=False))
        return 2
    evidence = {}
    if args.evidence:
        try:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict) or set(evidence) - {c["id"] for c in contracts}:
                raise ValueError("unexpected evidence IDs")
        except (OSError, ValueError, UnicodeError):
            print(json.dumps({"status": "INVALID_EVIDENCE"}))
            return 2
    rows = [{**c, "required_observations": REQUIRED[c["signal"]],
             "verdict": evaluate(c, evidence.get(c["id"]))[0]} for c in contracts]
    output = {"status": "OFFLINE_AUDIT_ONLY", "count": len(rows),
              "by_signal": dict(sorted(Counter(r["signal"] for r in rows).items())),
              "by_verdict": dict(sorted(Counter(r["verdict"] for r in rows).items()))}
    if not args.summary:
        output["cases"] = rows
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
