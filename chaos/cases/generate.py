"""Regenerate draft YAML 1.2 (JSON-subset) cases from explicit bounded variants.

These are proposed labels, NOT verified incident outcomes. Requires human review.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAMILIES = ("image_crash", "oom", "latency_pool", "endpoint_loss")
VARIANTS = {
    "image_crash": [
        ("bad_image", "image_pull", "nonexistent image tag", "verify image reference; restore snapshot"),
        ("controlled_crash", "crash_loop", "test-only bounded process termination", "inspect restart reason; restore snapshot"),
    ],
    "oom": [("controlled_oom", "oom_killed", "test-only bounded memory pressure", "inspect OOM events and limits; reset controlled fault")],
    "latency_pool": [
        ("controlled_latency", "slow_response", "test-only bounded inventory/order latency", "inspect request latency and upstream span; reset controlled fault"),
        ("controlled_pool_hold", "pool_exhaustion", "test-only bounded database connection hold", "inspect pool metrics and DB wait; reset controlled fault"),
    ],
    "endpoint_loss": [
        ("selector_mismatch", "no_endpoints", "Service selector does not match workload labels", "inspect Service selector and Endpoints; restore snapshot"),
        ("controlled_endpoint_off", "upstream_5xx", "test-only bounded endpoint unavailability", "inspect EndpointSlices and upstream errors; reset controlled fault"),
    ],
}
DECOYS = ("unrelated pod restart", "short transient load spike", "stale deployment event", "unrelated HTTP 404", "historical warning event")


def make_case(family, number):
    mode, signal, cause, response = VARIANTS[family][(number - 1) % len(VARIANTS[family])]
    target = "inventory" if (number - 1) // 2 % 2 == 0 else "order"
    if mode == "bad_image":
        params = {"tag": f"liveprobe/{target}:does-not-exist-{number:02d}"}
    elif mode == "selector_mismatch":
        params = {"selector_value": f"{target}-broken-{number:02d}"}
    elif mode == "controlled_latency":
        params = {"delay_ms": 100 + (number - 1) * 100}
    elif mode == "controlled_pool_hold":
        params = {"connections": 1 + (number - 1) % 8}
    else:
        params = {"duration_seconds": 5 + number}
    high_risk = family == "endpoint_loss" or (family == "oom" and number >= 11)
    workflow = "high_risk_handoff" if high_risk else ("simple" if family == "image_crash" and number <= 5 else "complex" if number >= 11 else "moderate")
    return {
        "version": 1,
        "id": f"F{FAMILIES.index(family)+1}-{number:02d}",
        "family": family,
        "target": target,
        "injection": {"mode": mode, "parameters": params},
        "expected_signal": signal,
        "recovery": "restore_snapshot_and_verify",
        "baseline_checks": ["deployments_ready", "services_endpoints_present", "order_check_200"],
        "reference_workflow": workflow,
        "ground_truth": {
            "cause": f"{target}: {cause} ({mode}, {params})",
            "expected_response": "operator handoff; " + response if high_risk else response,
            "decoy_signal": DECOYS[(number - 1) % len(DECOYS)],
            "evidence_sufficiency": "insufficient" if number % 3 == 0 else "sufficient",
        },
    }


if __name__ == "__main__":
    for family in FAMILIES:
        for number in range(1, 16):
            case = make_case(family, number)
            (ROOT / (case["id"] + ".yaml")).write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote 60 draft cases; NOT injection-verified or label-approved")
