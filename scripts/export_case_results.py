#!/usr/bin/env python3
"""Read-only, stdlib-only export of historical case results; never calls the application."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MISSING = "미수집"


def collect_store(root: Path, cohort: str, policy: str, store: Path):
    """Accept the first baseline-verified completion for each pair within ONE store."""
    directory = root / store
    manifest_bytes = (directory / "manifest.json").read_bytes()
    progress_bytes = (directory / "progress.jsonl").read_bytes()
    manifest = json.loads(manifest_bytes)
    body = {k: v for k, v in manifest.items() if k != "manifest_id"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if manifest.get("manifest_id") != digest or manifest.get("mode") != "real":
        raise ValueError(f"{store}: invalid manifest or non-real store")
    accepted, starts, closed = {}, {}, set()
    counts = {key: 0 for key in ("starts", "done", "completed", "failed", "interrupted", "unverified", "duplicates", "stops")}
    excluded = []
    for line, raw in enumerate(progress_bytes.splitlines(), 1):
        if not raw.strip():
            continue
        record = json.loads(raw)
        if record.get("manifest_id") != digest:
            raise ValueError(f"{store}:L{line}: manifest mismatch")
        kind = record.get("kind")
        if kind == "stop":
            counts["stops"] += 1
            continue
        if kind not in ("start", "done"):
            raise ValueError(f"{store}:L{line}: unknown kind")
        key = record["case_id"], record["arm_id"]
        attempt_key = (*key, record["attempt"])
        if key[0] not in manifest["catalog"]["cases"] or key[1] not in manifest["arms"]:
            raise ValueError(f"{store}:L{line}: pair not in manifest")
        if kind == "start":
            if attempt_key in starts:
                raise ValueError(f"{store}:L{line}: duplicate start")
            inputs = record.get("inputs") or {}
            if (inputs.get("arm_sha256") != manifest["arms"][key[1]] or
                    inputs.get("public_sha256") != manifest["catalog"]["cases"][key[0]]["public_sha256"]):
                raise ValueError(f"{store}:L{line}: input hashes mismatch")
            starts[attempt_key] = line
            counts["starts"] += 1
            continue
        if attempt_key not in starts or attempt_key in closed:
            raise ValueError(f"{store}:L{line}: done without unique open start")
        closed.add(attempt_key)
        counts["done"] += 1
        status = record.get("status")
        if status not in ("completed", "failed", "interrupted"):
            raise ValueError(f"{store}:L{line}: unknown status")
        counts[status] += 1
        reason = None
        if status != "completed":
            reason = status
        elif (record.get("baseline") or {}).get("ok") is not True:
            counts["unverified"] += 1
            reason = "baseline_not_verified"
        elif key in accepted:
            counts["duplicates"] += 1
            reason = "duplicate_completed"
        if reason:
            excluded.append({"line": line, "case": key[0], "arm": key[1],
                             "attempt": record["attempt"], "reason": reason})
            continue
        output = record.get("output") or {}
        if (output.get("schema") != "kan59-run-output/v1" or
                (output.get("case"), output.get("arm")) != key):
            raise ValueError(f"{store}:L{line}: output binding/schema mismatch")
        if output.get("cost_usd") is not None and output["cost_usd"] != record.get("cost_usd"):
            raise ValueError(f"{store}:L{line}: output cost differs from done cost")
        telemetry = output.get("telemetry") or {}
        accepted[key] = {
            "cohort": cohort, "policy": policy, "case": key[0], "arm": key[1],
            "attempt": record["attempt"], "incident_id": output.get("incident_id", MISSING),
            "level": output.get("level", MISSING),
            "workflow": output.get("selected_workflow", MISSING),
            "decided_by": output.get("decided_by", MISSING),
            "routing_reasons": output.get("routing_reasons", MISSING),
            "models": output.get("model_calls", MISSING),
            "cost_usd": output.get("cost_usd", MISSING),
            "cost_basis": output.get("cost_basis", MISSING),
            "elapsed_s": telemetry.get("wall_s", MISSING),
            "status_reason": telemetry.get("status_reason", MISSING),
            "candidates": output.get("root_cause_candidates", MISSING),
            "evidence_claims": output.get("evidence_claims", MISSING),
            "outcome": output.get("outcome", MISSING),
            "actions": telemetry.get("actions", MISSING),
            "policy_violations": output.get("policy_violations", MISSING),
            "restoration": record.get("baseline", MISSING),
            "source": f"{store.as_posix()}/progress.jsonl:L{line}",
            "line_sha256": hashlib.sha256(raw).hexdigest(),
            "manifest_id": digest,
        }
    rows = [accepted[key] for key in sorted(accepted)]
    audit = {"cohort": cohort, "policy": policy, "store": store.as_posix(),
             "accepted": len(rows), **counts, "open": len(starts.keys() - closed), "excluded": excluded,
             "cases": len({row["case"] for row in rows}),
             "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
             "progress_sha256": hashlib.sha256(progress_bytes).hexdigest()}
    return rows, audit


# Explicit report cohorts, not a glob of all experiments. Policy labels are from P12/P13 reports,
# not inferred from mutable application defaults. P13 reruns are never substituted for original rows.
SOURCES = (
    ("A", "A", ".e2e-tmp/revamp-P10/store-pilot", 64),
    ("B", "B", ".e2e-tmp/revamp-P11b/store-chaos", 176),
    ("C", "C", ".e2e-tmp/revamp-P12/store-policyC", 88),
    ("P13-main", "C / P13 신규 라벨", ".e2e-tmp/revamp-P12/store-p13", 24),
    ("P13-rerun", "C / P13 수정 후", ".e2e-tmp/revamp-P12/store-p13-n103", 2),
)


def collect_all(root: Path):
    """Validate declared totals and inventory excluded smoke/trial stores without merging them."""
    rows, audits, excluded = [], [], []
    included = {source[2] for source in SOURCES}
    for cohort, policy, path, expected in SOURCES:
        batch, audit = collect_store(root, cohort, policy, Path(path))
        if len(batch) != expected or audit["open"] or audit["unverified"]:
            raise ValueError(f"{path}: expected {expected} rows, got {len(batch)}; open/unverified must be zero")
        arms = {row["arm"] for row in batch}
        expected_arms = ({"adaptive_guide", "adaptive_noguide"} if cohort.startswith("P13") else
                         {"single_small", "single_high", "fixed_multi_agent", "adaptive"})
        if arms != expected_arms or len({row["case"] for row in batch}) * len(arms) != expected:
            raise ValueError(f"{path}: incomplete case × arm matrix")
        rows.extend(batch)
        audits.append(audit)
    original = [row for row in rows if row["cohort"] in ("A", "B")]
    if len({(row["case"], row["arm"]) for row in original}) != 240:
        raise ValueError("A/B must cover 60 cases × 4 arms without overlap")
    for parent in sorted({str(Path(source[2]).parent) for source in SOURCES}):
        for progress in sorted((root / parent).glob("store-*/progress.jsonl")):
            store = progress.parent.relative_to(root)
            if store.as_posix() in included:
                continue
            _, audit = collect_store(root, "제외: smoke/trial", "보고서 비교 대상 아님", store)
            excluded.append(audit)
    return rows, audits, excluded


def render(rows, audits, excluded):
    """Render deterministic UTF-8 Markdown and lossless structured-field CSV."""
    import csv
    import html
    import io
    from collections import Counter

    def cell(value):
        if value is None:
            text = "없음(null)"
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        else:
            text = str(value)
        return html.escape(text, quote=False).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")

    def table(headers, values):
        return ["| " + " | ".join(map(cell, headers)) + " |", "|" + "---|" * len(headers)] + [
            "| " + " | ".join(map(cell, value)) + " |" for value in values]

    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                         if isinstance(value, (dict, list)) else "없음(null)" if value is None else value
                         for key, value in row.items()})
    lines = ["# P14 E1 — 시험 사례별 결과표", "",
             "생성: `python3 scripts/export_case_results.py` · 검증: `python3 scripts/export_case_results.py --check`", "",
             f"본 표는 **{len(rows)}개 실행 결과 행**이다. 정책 A·B·C와 P13 본 실행·수정 후 재실행을 아래에서 분리한다.",
             "행수는 서로 다른 (case, arm)의 수가 아니다. P13 N1-03 재실행 2행도 별도 보존한다. 정책별 지표를 합산하지 않는다.",
             "A+B는 60사례 × 4방식, C는 그중 22사례 재실행, P13은 새 12사례 × 2방식 + 수정 후 1사례 × 2방식이다.",
             "정책 A: SIMPLE만 자동·롤백만. 정책 B: COMPLEX까지 자동·롤백만. 정책 C: HIGH_RISK 이관·허용 조치 5종. P13은 정책 C지만 신규 사례/라벨과 수정 전후 환경이 달라 따로 낸다.", "",
             "## 원본과 채택 규칙", "",
             "- 원본: 각 저장소의 `manifest.json` 및 `progress.jsonl`만 읽는다. DB/API/클러스터/LLM 호출은 없다.",
             "- manifest 본문 해시·record manifest·start/done 대응·입력 해시·output 사례/방식·비용 일치를 검증한다.",
             "- 같은 **저장소 안** (case, arm)에서 기준선 `baseline.ok=true`인 최초 completed만 채택한다. 실패·중복·미원복은 제외 내역에 남긴다.",
             "- CSV는 JSON 목록/객체를 보존한다(호출 순서·역할·모델·effort·재시도·상태, 원인 후보 원문, 근거 판정, 조치, 원복). Markdown은 읽기용 요약이다.",
             "- 출처는 실제 done 줄 번호와 SHA-256으로 연결한다. CSV에 `line_sha256`, `manifest_id`를 포함한다.",
             "- 과거 모델명과 기록된 `cost_usd`/`cost_basis`는 불변이다. **현재 단가로 다시 계산하지 않는다.** estimated는 구독 CLI 토큰 환산 추정치이지 청구액이 아니다.", ""]
    lines += table(["구간", "정책", "사례", "start", "done", "completed", "채택 행", "실패/중단", "중복", "미원복", "미종결"], [
        [a["cohort"], a["policy"], a["cases"], a["starts"], a["done"], a["completed"], a["accepted"],
         a["failed"] + a["interrupted"], a["duplicates"], a["unverified"], a["open"]] for a in audits])
    lines += ["", "### 원본 지문", ""]
    for audit in audits + excluded:
        lines += [f"- `{audit['store']}`", f"  - manifest SHA-256: `{audit['manifest_sha256']}`",
                  f"  - progress SHA-256: `{audit['progress_sha256']}`"]
    lines += ["", "## 필드 의미와 누락", "",
              "- `level`(별도 사건 수준)과 라우팅 선택 이유(rationale)는 이 progress 스키마에 **미수집**이다. Workflow나 결과 사유로 추정해 채우지 않는다.",
              "- `workflow`는 `selected_workflow` 원문이다. 단일 방식의 null은 `없음(null)`이며 별도 수준을 뜻하지 않는다.",
              "- `elapsed_s`는 `telemetry.wall_s`(엔진 분석 실행 벽시계 시간)다. 장애 주입·사건 수신 이전 대기·시험 후 기준선 복구까지의 전체 시험 시간이 아니다.",
              "- `status_reason`은 결과 상태 사유이며 **라우팅 이유가 아니다**. 판단 주체는 `decided_by` 원문이다.",
              "- `evidence_claims`는 quote/ref의 일치 판정만 담는다. 인용 문장·근거 ID·원본 로그 내용은 **미수집**이며, 아래 판정 건수를 실제 근거 본문처럼 해석하지 않는다.",
              "- `[]`는 수집된 빈 목록(없음), `미수집`은 키 부재, null은 원본 null이다. 누락 비용/시간을 0으로 채우지 않는다.",
              "- `outcome.verified`는 실행기가 기록한 검증 표시다. 정답 라벨 대비 성공률이 아니다. `restoration`은 AI 복구와 별개인 시험 후 기준선 원복 증명이다.",
              "- `prices_version`·호출별 토큰/비용·라우터 후보 점수·근거 본문·전체 라우팅 판단은 progress에 없으므로 DB를 보지 않고 보충하지 않는다.", ""]
    fields = ["level", "workflow", "routing_reasons", "decided_by", "models", "cost_usd", "cost_basis",
              "elapsed_s", "status_reason", "candidates", "evidence_claims", "outcome", "actions", "restoration"]
    lines += table(["필드", "키 부재(미수집)", "원본 null", "수집된 빈 목록"], [
        [key, sum(row[key] == MISSING for row in rows), sum(row[key] is None for row in rows),
         sum(row[key] == [] for row in rows)] for key in fields])
    lines += ["", "## 비교에서 제외한 실행", "",
              "아래 smoke/trial 저장소도 읽어 수를 확인했지만 본 결과 행에 혼합하지 않았다(P12-policyC-results §4).", ""]
    lines += table(["저장소", "채택 가능한 완료 쌍", "제외 이유"], [
        [a["store"], a["accepted"], "smoke/trial, 본 보고서 비교 대상 아님"] for a in excluded])
    lines += ["", "### 본 구간의 제외 attempt", ""]
    attempt_rows = [[a["cohort"], f"{a['store']}/progress.jsonl:L{r['line']}", r["case"], r["arm"], r["attempt"], r["reason"]]
                    for a in audits for r in a["excluded"]]
    lines += table(["구간", "출처", "case", "arm", "attempt", "제외 이유"], attempt_rows) if attempt_rows else ["없음."]
    for audit in audits:
        cohort = audit["cohort"]
        batch = [row for row in rows if row["cohort"] == cohort]
        lines += ["", f"## {cohort} — {audit['policy']} ({len(batch)}행)", "",
                  "방식별 행수: " + ", ".join(f"{arm}={count}" for arm, count in sorted(Counter(r["arm"] for r in batch).items())), ""]
        values = []
        for row in batch:
            models = row["models"]
            if isinstance(models, list):
                models = "; ".join(f"{m.get('role', MISSING)}={m.get('model', MISSING)}/{m.get('effort', MISSING)}"
                                   f" [{m.get('status', MISSING)}, attempts={m.get('attempts', MISSING)}]" for m in models) or "없음([])"
            claims = row["evidence_claims"]
            evidence = dict(sorted(Counter(c["verdict"] for c in claims).items())) if isinstance(claims, list) else claims
            candidates = row["candidates"]
            if isinstance(candidates, list):
                candidates = "; ".join(f"{c.get('rank', MISSING)}. {c.get('target', MISSING)}/{c.get('cause_class', MISSING)}"
                                       f" ({c.get('confidence', MISSING)})" for c in candidates) or "없음([])"
            restoration = row["restoration"]
            if isinstance(restoration, dict):
                restoration = f"ok={restoration.get('ok', MISSING)}, stable_s={restoration.get('stable_s', MISSING)}"
            values.append([row["case"], row["arm"], row["level"], row["workflow"], models,
                           row["cost_usd"], row["elapsed_s"], row["decided_by"], row["routing_reasons"],
                           row["status_reason"], candidates, evidence, row["outcome"], restoration, row["source"]])
        lines += table(["case", "arm", "level", "Workflow", "models / effort / status / attempts", "cost USD (estimated)", "elapsed s",
                        "판단 주체", "라우팅 이유", "결과 사유", "원인 후보 (상위 3)", "근거 일치 판정 건수", "outcome", "원복", "출처"], values)
    lines += ["", "## 해석 경계", "",
              "P13-main N1-03은 rollout_undo 버그가 있던 원본이며 P13-rerun은 수정 후 별도 실행이다. 기존 행을 수정하거나 두 실행을 하나로 중복 제거하지 않았다.",
              "이 표는 원본 기록의 재표현이지 신규 실험이나 목표 달성 증명이 아니다. 정책/근거/라벨 차이와 publishable 제한은 [P12 최종 보고서](P12-final-report.md), [정책 C](P12-policyC-results.md), [P13](P13-results.md)를 따른다.",
              "원본 `.e2e-tmp`는 로컬 보존 자료다. 깨끗한 checkout에 없다면 동일 저장소를 복원해야 재생성할 수 있으며, 생성기는 원본 부재 시 실패한다.", ""]
    return "\n".join(lines), text.getvalue()


def write_outputs(root: Path, markdown: str, csv_text: str, check: bool = False) -> int:
    """Only these two fixed targets may be written; check mode never writes."""
    directory = root / "docs/plans/revamp"
    outputs = ((directory / "P14-case-table.md", markdown), (directory / "P14-case-table.csv", csv_text))
    if check:
        stale = [str(path) for path, content in outputs
                 if not path.is_file() or path.read_bytes() != content.encode("utf-8")]
        if stale:
            print("STALE: " + ", ".join(stale))
            return 1
    else:
        directory.mkdir(parents=True, exist_ok=True)
        for path, content in outputs:
            path.write_bytes(content.encode("utf-8"))
    return 0


def main(argv=None) -> int:
    import argparse
    import sys
    from collections import Counter

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root containing original .e2e-tmp stores")
    parser.add_argument("--check", action="store_true", help="verify byte-identical outputs without writing")
    args = parser.parse_args(argv)
    try:
        rows, audits, excluded = collect_all(args.root.resolve())
        markdown, csv_text = render(rows, audits, excluded)
        result = write_outputs(args.root.resolve(), markdown, csv_text, args.check)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    if result == 0:
        print(json.dumps({"ok": True, "check": args.check, "rows": len(rows),
                          "per_cohort": dict(Counter(row["cohort"] for row in rows)),
                          "excluded_smoke_trial_pairs": sum(a["accepted"] for a in excluded),
                          "excluded_failed_attempts": sum(a["failed"] + a["interrupted"] for a in audits)},
                         ensure_ascii=False, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
