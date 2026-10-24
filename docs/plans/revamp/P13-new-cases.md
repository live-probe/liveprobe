# P13 새 사례 검증 프로토콜

- 승인: 2026-10-11 사용자 위임("허가할게. 진행하자"). 승인 문구 `approved_2026-10-11_user_delegated_P13`.
- 목적: 판단 참고자료(`docs/reference/liveprobe-judgement-guide.md` v1, sha256 `2fa8005b…`)가 처음 보는 사례에서 adaptive 방식의 판단을 개선하는지 관찰한다.

## 사례
- `chaos/cases-new/N1-01 … N4-03`: 12개(4 Family × 3), 참고자료를 만든 **뒤에** 작성했다. 기존 60사례와 대상·파라미터 조합이 겹치지 않는다.
- 라벨 기준은 정책 C(HIGH_RISK만 인계)다. `LABELS.json` label_version 1(approved).
- 정답 유출 검사: `python -m evaluation.guide_leak_check liveprobe/reference/liveprobe-judgement-guide.md --catalog chaos/cases --held-out chaos/cases-new`가 PASS여야 한다(정답 문구, 숨김 키, 새 사례 ID 모두 없어야 함).

## 비교 방식 (`evaluation/harness/arms.py` `P13_ARMS`)
| arm | 역할·모델 | RAG 자료 |
|---|---|---|
| adaptive_noguide | adaptive와 동일 | runbook, public_doc |
| adaptive_guide | adaptive와 동일 | runbook, public_doc, reference_guide |

## 실행
`scripts/eval-real.sh <store> --p13 --cases-dir chaos/cases-new` — 12사례 × 2 = 24쌍, 직렬 실행, 쌍마다 기준선 복구 증명.

## 지표
원인 top-3 적중, Workflow 일치, 복구/이관 성공, 추정 비용, 시간, 근거 일치(지표 엔진 `evaluation.metrics compute --cases-dir chaos/cases-new`).

## 해석 원칙
결과는 "새 사례 12개에서의 관찰"로만 보고한다. 1~2건 차이는 우연과 구분되지 않으므로 일반화하지 않는다.
