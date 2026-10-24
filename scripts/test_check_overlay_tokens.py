"""KAN-106 regression: check-overlay.py token-automount allowlist.

rollback-controller is a legitimate API client (TokenReview, KAN-103) and must be allowed to
automount its ServiceAccount token; any workload not on the explicit allowlist must still be
rejected when it automounts. Uses the real portable-example render (kubectl kustomize) and
mutates only the automount field of one workload per case.
"""
import ast
import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "deploy/overlays/portable-example"
CHECK = ROOT / "scripts/check-overlay.py"
EXPECTED_API_CLIENTS = {"liveprobe-api", "rollback-controller", "prometheus", "alloy", "kube-state-metrics"}


@pytest.fixture(scope="module")
def docs():
    out = subprocess.run(["kubectl", "kustomize", str(OVERLAY)], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def workloads(docs):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] in ("Deployment", "StatefulSet")}


def run_check(docs, tmp_path):
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(yaml.safe_dump_all(docs))
    return subprocess.run([sys.executable, str(CHECK), str(OVERLAY), str(rendered)], capture_output=True, text=True)


def test_allowlist_is_exactly_the_approved_api_clients():
    src = CHECK.read_text()
    line = next(l for l in src.splitlines() if l.startswith("api_clients ="))
    assert ast.literal_eval(line.split("=", 1)[1].strip()) == EXPECTED_API_CLIENTS  # literal set, no broadening


def test_rollback_controller_automount_permitted(docs, tmp_path):
    rc = workloads(docs)["rollback-controller"]
    assert rc["spec"]["template"]["spec"].get("automountServiceAccountToken", True) is not False
    r = run_check(docs, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "rollback-controller" not in r.stderr


def test_rollback_controller_without_token_rejected(docs, tmp_path):
    mutated = copy.deepcopy(docs)
    workloads(mutated)["rollback-controller"]["spec"]["template"]["spec"]["automountServiceAccountToken"] = False
    r = run_check(mutated, tmp_path)
    assert r.returncode == 1
    assert "rollback-controller: automountServiceAccountToken=False" in r.stderr


def test_unapproved_workload_automount_rejected(docs, tmp_path):
    names = sorted(set(workloads(docs)) - EXPECTED_API_CLIENTS)
    assert names, "render must contain non-API-client workloads"
    for name in names:
        mutated = copy.deepcopy(docs)
        workloads(mutated)[name]["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        r = run_check(mutated, tmp_path)
        assert r.returncode == 1, name
        assert f"{name}: automountServiceAccountToken=True" in r.stderr


def test_unapproved_workload_default_automount_rejected(docs, tmp_path):
    name = sorted(set(workloads(docs)) - EXPECTED_API_CLIENTS)[0]
    mutated = copy.deepcopy(docs)
    workloads(mutated)[name]["spec"]["template"]["spec"].pop("automountServiceAccountToken", None)
    r = run_check(mutated, tmp_path)
    assert r.returncode == 1
    assert f"{name}: automountServiceAccountToken=True" in r.stderr
