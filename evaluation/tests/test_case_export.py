"""Offline tests: no DB, cluster, credentials, network or model calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/export_case_results.py"


def exporter():
    assert SCRIPT.is_file(), "case-results exporter must exist"
    spec = importlib.util.spec_from_file_location("case_export", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_store(tmp_path, records):
    manifest = {"mode": "real", "arms": {"adaptive": "arm-hash"},
                "catalog": {"cases": {"F1-01": {"public_sha256": "case-hash"}}}}
    manifest["manifest_id"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "store"
    path.mkdir()
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = [{"manifest_id": manifest["manifest_id"], **record} for record in records]
    (path / "progress.jsonl").write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    return path


def start(attempt=1):
    return {"kind": "start", "case_id": "F1-01", "arm_id": "adaptive", "attempt": attempt,
            "inputs": {"arm_sha256": "arm-hash", "public_sha256": "case-hash"}}


def done(attempt=1, **changes):
    return {"kind": "done", "case_id": "F1-01", "arm_id": "adaptive", "attempt": attempt,
            "status": "completed", "baseline": {"ok": True, "stable_s": 30}, "cost_usd": 0.123456,
            "output": {"schema": "kan59-run-output/v1", "case": "F1-01", "arm": "adaptive",
                       "selected_workflow": "NORMAL", "cost_basis": "estimated", "cost_usd": 0.123456,
                       "model_calls": [{"role": "k8s_analyst", "model": "gpt-6-sol", "effort": "high"}],
                       "telemetry": {"wall_s": 12.3, "status_reason": "verifier supported"},
                       "evidence_claims": [], "root_cause_candidates": [],
                       "outcome": {"kind": "handoff", "verified": True}, "policy_violations": []}, **changes}


def test_export_preserves_history_without_inventing_absent_fields(tmp_path):
    path = make_store(tmp_path, [start(), done()])
    module = exporter()
    rows, audit = module.collect_store(tmp_path, "A", "A", Path("store"))
    assert len(rows) == audit["accepted"] == 1
    row = rows[0]
    assert row["models"] == done()["output"]["model_calls"]
    assert row["cost_usd"] == 0.123456
    assert row["level"] == row["routing_reasons"] == "미수집"
    assert row["workflow"] == "NORMAL"
    assert row["status_reason"] == "verifier supported"
    assert row["evidence_claims"] == []  # collected-empty is not missing
    assert row["source"] == "store/progress.jsonl:L2"
    raw_line = (path / "progress.jsonl").read_bytes().splitlines()[1]
    assert row["line_sha256"] == hashlib.sha256(raw_line).hexdigest()


def test_dedup_counts_failed_unverified_duplicate_and_open_attempts(tmp_path):
    records = [start(), done(status="failed"), start(2), done(2, baseline={"ok": False}),
               start(3), done(3), start(4), done(4), start(5)]
    make_store(tmp_path, records)
    rows, audit = exporter().collect_store(tmp_path, "C", "C", Path("store"))
    assert len(rows) == 1 and rows[0]["attempt"] == 3
    assert {key: audit[key] for key in ["starts", "done", "completed", "failed", "unverified", "duplicates", "open"]} == {
        "starts": 5, "done": 4, "completed": 3, "failed": 1, "unverified": 1, "duplicates": 1, "open": 1}
    assert audit["excluded"] == [
        {"line": 2, "case": "F1-01", "arm": "adaptive", "attempt": 1, "reason": "failed"},
        {"line": 4, "case": "F1-01", "arm": "adaptive", "attempt": 2, "reason": "baseline_not_verified"},
        {"line": 8, "case": "F1-01", "arm": "adaptive", "attempt": 4, "reason": "duplicate_completed"}]


def test_rejects_corruption_and_cross_manifest_records(tmp_path):
    import pytest
    path = make_store(tmp_path, [start(), done()])
    progress = path / "progress.jsonl"
    original = progress.read_text()
    progress.write_text(original + "{torn\n")
    with pytest.raises(ValueError):
        exporter().collect_store(tmp_path, "A", "A", Path("store"))
    rows = [json.loads(line) for line in original.splitlines()]
    rows[1]["manifest_id"] = "another-manifest"
    progress.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="manifest mismatch"):
        exporter().collect_store(tmp_path, "A", "A", Path("store"))


def test_rejects_invalid_attempt_or_output_binding(tmp_path):
    import pytest
    invalid = done()
    invalid["output"]["case"] = "another-case"
    path = make_store(tmp_path, [start(), invalid])
    with pytest.raises(ValueError, match="output"):
        exporter().collect_store(tmp_path, "A", "A", Path("store"))
    (path / "progress.jsonl").write_text(json.dumps({"manifest_id": json.loads((path / "manifest.json").read_text())["manifest_id"], **done()}))
    with pytest.raises(ValueError, match="start"):
        exporter().collect_store(tmp_path, "A", "A", Path("store"))


def test_render_is_deterministic_escapes_markdown_and_roundtrips_csv(tmp_path):
    import csv
    import io
    record = done()
    record["output"]["telemetry"]["status_reason"] = "one | two\n<three>"
    make_store(tmp_path, [start(), record])
    module = exporter()
    rows, audit = module.collect_store(tmp_path, "A", "A", Path("store"))
    md, text = module.render(rows, [audit], [])
    assert (md, text) == module.render(rows, [audit], [])
    assert "one &#124; two<br>&lt;three&gt;" in md
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 1
    assert json.loads(parsed[0]["models"]) == record["output"]["model_calls"]
    assert parsed[0]["status_reason"] == "one | two\n<three>"
    assert parsed[0]["level"] == "미수집"
    assert "라우팅 이유" in md and "현재 단가" in md


def test_actual_stores_have_exactly_354_rows_with_separate_p13_rerun():
    import pytest
    module = exporter()
    if not all((ROOT / source[2] / "progress.jsonl").is_file() for source in module.SOURCES):
        pytest.skip("local historical stores are not included in a clean checkout")
    rows, audits, excluded = module.collect_all(ROOT)
    assert len(rows) == 354
    assert [audit["accepted"] for audit in audits] == [64, 176, 88, 24, 2]
    assert len({(row["cohort"], row["case"], row["arm"]) for row in rows}) == 354
    assert len({(row["case"], row["arm"]) for row in rows if row["cohort"].startswith("P13")}) == 24
    assert sum(audit["accepted"] for audit in excluded) == 13
    assert all(row["level"] == row["routing_reasons"] == "미수집" for row in rows)
    assert all(row["restoration"]["ok"] is True for row in rows)
    sources = {}
    for row in rows:
        name, number = row["source"].rsplit(":L", 1)
        if name not in sources:
            sources[name] = (ROOT / name).read_bytes().splitlines()
        raw = sources[name][int(number) - 1]
        record = json.loads(raw)
        output = record["output"]
        assert row["line_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["cost_usd"] == output["cost_usd"] == record["cost_usd"]
        assert row["models"] == output["model_calls"]
        assert row["candidates"] == output["root_cause_candidates"]
        assert row["evidence_claims"] == output["evidence_claims"]
        assert row["outcome"] == output["outcome"]
        assert row["elapsed_s"] == output["telemetry"]["wall_s"]


def test_cli_missing_source_fails_without_creating_output(tmp_path):
    import subprocess
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "INVALID:" in result.stderr
    assert not (tmp_path / "docs").exists()


def test_cli_check_detects_stale_output_without_overwriting(tmp_path):
    import pytest
    module = exporter()
    # Exercise CLI orchestration with the real dataset, not fabricated result records.
    if not (ROOT / module.SOURCES[0][2]).is_dir():
        pytest.skip("historical stores unavailable")
    rows, audits, excluded = module.collect_all(ROOT)
    md, text = module.render(rows, audits, excluded)
    target = tmp_path / "docs/plans/revamp"
    target.mkdir(parents=True)
    (target / "P14-case-table.md").write_text(md)
    (target / "P14-case-table.csv").write_text(text)
    assert module.write_outputs(tmp_path, md, text, check=True) == 0
    (target / "P14-case-table.csv").write_text("stale")
    assert module.write_outputs(tmp_path, md, text, check=True) == 1
    assert (target / "P14-case-table.csv").read_text() == "stale"
