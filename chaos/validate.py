"""Offline, dependency-free validator for the operator-only case catalog.

Case files use JSON syntax, which is a YAML 1.2 subset. This tool never calls kubectl.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent / "cases"
FAMILIES = {"image_crash", "oom", "latency_pool", "endpoint_loss"}
MODES = {
    "image_crash": {"bad_image", "controlled_crash"},
    "oom": {"controlled_oom"},
    "latency_pool": {"controlled_latency", "controlled_pool_hold"},
    "endpoint_loss": {"selector_mismatch", "controlled_endpoint_off"},
}
SIGNALS = {"image_pull", "crash_loop", "oom_killed", "slow_response", "pool_exhaustion", "no_endpoints", "upstream_5xx"}
WORKFLOWS = {"simple", "moderate", "complex", "high_risk_handoff"}
FIELDS = {"version", "id", "family", "target", "injection", "expected_signal", "recovery", "baseline_checks", "reference_workflow", "ground_truth"}
TRUTH = {"cause", "expected_response", "decoy_signal", "evidence_sufficiency"}
PARAMETERS = {"tag", "duration_seconds", "delay_ms", "connections", "selector_value"}
BASELINE = ["deployments_ready", "services_endpoints_present", "order_check_200"]
ID = re.compile(r"^[FN][1-4]-[0-9]{2}$")       # F: approved 60-case catalog; N: P13 held-out new cases
TAG = re.compile(r"^liveprobe/(?:inventory|order):does-not-exist-[0-9]{2}$")
SELECTOR = re.compile(r"^(?:inventory|order)-broken-[0-9]{2}$")


def check(case):
    """Return human-readable validation errors; no arbitrary command or target accepted."""
    e = []
    if not isinstance(case, dict):
        return ["case must be an object"]
    if set(case) != FIELDS:
        e.append(f"fields must equal {sorted(FIELDS)}")
    family = case.get("family")
    cid = case.get("id")
    if not isinstance(cid, str) or not ID.fullmatch(cid):
        e.append("invalid id")
    if not isinstance(family, str) or family not in FAMILIES:
        e.append("invalid family")
    elif isinstance(cid, str) and ID.fullmatch(cid) and int(cid[1]) != list(MODES).index(family) + 1:
        e.append("id/family mismatch")
    if case.get("version") != 1:
        e.append("unsupported version")
    if case.get("target") not in ("inventory", "order"):
        e.append("unsafe target")
    injection = case.get("injection")
    if not isinstance(injection, dict) or set(injection) != {"mode", "parameters"}:
        e.append("invalid injection")
    else:
        mode, params = injection["mode"], injection["parameters"]
        if not isinstance(mode, str) or mode not in MODES.get(family if isinstance(family, str) else "", set()) or not isinstance(params, dict) or len(params) != 1 or not set(params) <= PARAMETERS:
            e.append("unsafe mode or parameters")
        else:
            param, value = next(iter(params.items()))
            mapping = {"bad_image": "tag", "controlled_crash": "duration_seconds", "controlled_oom": "duration_seconds", "controlled_latency": "delay_ms", "controlled_pool_hold": "connections", "selector_mismatch": "selector_value", "controlled_endpoint_off": "duration_seconds"}
            if param != mapping[mode]:
                e.append("parameter/mode mismatch")
            elif param == "tag" and (not isinstance(value, str) or not TAG.fullmatch(value)):
                e.append("unsafe image tag")
            elif param == "selector_value" and (not isinstance(value, str) or not SELECTOR.fullmatch(value)):
                e.append("unsafe selector")
            elif param in ("duration_seconds", "delay_ms", "connections") and (type(value) is not int or not 1 <= value <= {"duration_seconds": 30, "delay_ms": 1500, "connections": 8}[param]):
                e.append("parameter outside bounded range")
    if case.get("expected_signal") not in SIGNALS:
        e.append("invalid signal")
    if case.get("recovery") != "restore_snapshot_and_verify":
        e.append("invalid recovery")
    if case.get("baseline_checks") != BASELINE:
        e.append("invalid baseline checks")
    if case.get("reference_workflow") not in WORKFLOWS:
        e.append("invalid workflow")
    truth = case.get("ground_truth")
    if not isinstance(truth, dict) or set(truth) != TRUTH:
        e.append("invalid ground truth fields")
    else:
        for field in ("cause", "expected_response", "decoy_signal"):
            if not isinstance(truth[field], str) or not truth[field].strip():
                e.append(f"empty {field}")
        if truth["evidence_sufficiency"] not in ("sufficient", "insufficient"):
            e.append("invalid evidence sufficiency")
    return e


def validate_catalog(root=ROOT, per_family=15, total=60):
    errors, cases = [], []
    files = sorted(root.glob("*.yaml"))
    for path in files:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            errors.append(f"{path.name}: invalid JSON-subset YAML: {exc}")
            continue
        errors.extend(f"{path.name}: {msg}" for msg in check(case))
        if isinstance(case, dict):
            if path.stem != case.get("id"):
                errors.append(f"{path.name}: filename/id mismatch")
            cases.append(case)
    ids = [case.get("id") for case in cases]
    for cid, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate id: {cid}")
    counts = Counter(case.get("family") for case in cases)
    for family in FAMILIES:
        if counts[family] != per_family:
            errors.append(f"{family}: expected {per_family}, found {counts[family]}")
    if len(files) != total:
        errors.append(f"expected {total} files, found {len(files)}")
    prefix = "F" if root == ROOT else None
    if prefix and any(not str(c.get("id", "")).startswith(prefix) for c in cases):
        errors.append("the approved 60-case catalog must only contain F cases")
    errors.extend(check_labels(root, cases))
    return errors, counts


LABELS = "LABELS.json"


def label_digest(case):
    """Digest of the approved answer fields; any label edit changes it."""
    answer = {"reference_workflow": case.get("reference_workflow"), "ground_truth": case.get("ground_truth")}
    return hashlib.sha256(json.dumps(answer, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def check_labels(root, cases):
    """Label changes must be recorded by bumping LABELS.json (see CHANGELOG.md)."""
    path = root / LABELS
    if not path.exists():
        return [f"{LABELS}: missing approved label manifest"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{LABELS}: invalid JSON: {exc}"]
    errors = []
    if type(manifest.get("label_version")) is not int or manifest["label_version"] < 1:
        errors.append(f"{LABELS}: invalid label_version")
    if manifest.get("status") not in ("approved", "draft"):
        errors.append(f"{LABELS}: invalid status")
    recorded = manifest.get("cases") if isinstance(manifest.get("cases"), dict) else {}
    actual = {case.get("id"): label_digest(case) for case in cases}
    for cid in sorted(set(actual) | set(recorded)):
        if cid not in recorded:
            errors.append(f"{LABELS}: {cid} not recorded")
        elif cid not in actual:
            errors.append(f"{LABELS}: {cid} recorded but no case file")
        elif recorded[cid] != actual[cid]:
            errors.append(f"{LABELS}: {cid} labels changed without a new label_version")
    return errors


def write_labels(root, version, status, approved_on, approval_ref):
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("*.yaml"))]
    manifest = {
        "label_version": version,
        "status": status,
        "approved_on": approved_on,
        "approval_ref": approval_ref,
        "digest": "sha256 of canonical JSON {reference_workflow, ground_truth} (sort_keys, UTF-8)",
        "cases": {case["id"]: label_digest(case) for case in cases},
    }
    (root / LABELS).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT)
    parser.add_argument("--write-labels", type=int, metavar="VERSION", help="record current labels as VERSION (operator action; add a CHANGELOG.md entry)")
    parser.add_argument("--status", default="approved", choices=("approved", "draft"))
    parser.add_argument("--approved-on", default="")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args()
    if args.write_labels is not None:
        print(f"recorded {write_labels(args.cases, args.write_labels, args.status, args.approved_on, args.approval_ref)} cases as label_version {args.write_labels}")
    problems, counts = validate_catalog(args.cases)
    for problem in problems:
        print(problem)
    print("; ".join(f"{name}: {counts[name]}" for name in MODES))
    raise SystemExit(bool(problems))
