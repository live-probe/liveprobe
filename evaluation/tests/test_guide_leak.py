import json
from pathlib import Path

from evaluation.guide_leak_check import main, problems

CASES = Path(__file__).resolve().parents[2] / "chaos" / "cases"


def _write_catalog(d: Path, cid: str, cause: str):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.yaml").write_text(json.dumps({
        "version": 1, "id": cid, "family": "oom", "target": "inventory", "expected_signal": "oom_killed",
        "baseline_checks": [], "reference_workflow": "moderate",
        "ground_truth": {"cause": cause, "expected_response": "an operator response string long enough",
                         "decoy_signal": "a decoy signal that is long", "evidence_sufficiency": "sufficient"}}))


def test_clean_guide_passes_against_real_catalog():
    guide = Path(__file__).resolve().parents[2] / "docs" / "reference" / "liveprobe-judgement-guide.md"
    assert problems(guide.read_text("utf-8"), [CASES], []) == []


def test_truth_string_and_key_and_heldout_id_are_caught(tmp_path):
    new = tmp_path / "new"
    _write_catalog(new, "N2-01", "a brand new hidden cause for the new case")
    leaked = "intro\nA Brand  new hidden cause for the new case\nexpected_response: x\nsee N2-01\n"
    found = problems(leaked, [], [new])
    assert any("truth string" in f for f in found)            # whitespace/case-insensitive match
    assert any("hidden answer key" in f for f in found)
    assert any("N2-01" in f for f in found)
    assert all("brand new hidden cause" not in f.lower() for f in found)   # value never echoed


def test_cli_exit_code_reflects_result(tmp_path):
    new = tmp_path / "new"
    _write_catalog(new, "N1-01", "another hidden cause that must not leak")
    bad = tmp_path / "g.md"
    bad.write_text("another hidden cause that must not leak")
    good = tmp_path / "ok.md"
    good.write_text("nothing to see")
    assert main([str(bad), "--held-out", str(new)]) == 1
    assert main([str(good), "--held-out", str(new)]) == 0
