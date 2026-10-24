# P10 평가 프로토콜 (2026-10-05, 사용자 "허가 없이 끝까지" 지시에 따른 권장안 확정)

기준 문서: docs/plans/evaluation-60cases-4arms-plan.md. 이 문서는 그 문서의 "8. 미결 결정"을 닫는다.
모든 결정은 `.hermes/plans/decision-log.md`에 같은 내용으로 기록한다.

## 1. 실측으로 확인한 사실 (추정 아님)

| 항목 | 값 | 근거 |
|---|---|---|
| 구독에서 호출 가능한 모델 | gpt-6-luna, gpt-5.6-terra, gpt-6-sol (effort low/medium/high/xhigh) | 컨테이너 codex 0.157, 각 1회 호출 성공(tokens used 출력) |
| 로컬 codex 0.137 | 모델 목록 응답의 `max` 값을 해석하지 못함(모델 캐시 오류), gpt-6-luna "ChatGPT 계정에서 미지원" 오류 | 로컬은 평가에 쓰지 않는다. 평가는 클러스터 API 파드 안의 codex만 사용 |
| 실제 사건 1건 소요 | 약 110~160초(수집 20초 + 에이전트 3회 + 조치/검증) | 사건 #65/#66/#67 |
| 실제 사건 1건 토큰 | 입력 5.2~5.7만, 출력 0.7~1.6천 | 같은 사건의 agent_runs |
| 최악 위험 | 하루 한도에 닿을 수 있음 | 실제 한도 형식은 아직 미관측 |
| 정답표 승인 | LABELS.json status=approved(2026-09-26, KAN-90) | chaos/cases/LABELS.json |
| 정답표 분포 | 60개 중 근거 부족 20개, reference_workflow: simple 5, moderate 25, complex 10, high_risk_handoff 20 | chaos/cases/*.yaml |

## 2. 결정

### D3 모델 고정 (4 arm)
기존 evaluation/harness/arms.py의 초안값을 그대로 확정한다. 사용 가능한 모델 세 개가 모두 실측으로 확인됐고, 임의 변경은 결과를 본 뒤 조정이 되므로 하지 않는다.

| arm | 구성 |
|---|---|
| single_small | 전 역할 gpt-6-luna / low |
| single_high | 전 역할 gpt-6-sol / xhigh |
| fixed_multi_agent | router luna/low, 분석·확인·근거 terra/medium, 안전 sol/high, 항상 complex 그룹 |
| adaptive | router luna/low, signal luna/low, verifier terra/low, curator terra/medium, safety sol/xhigh, 라우터가 그룹 선택 |

`approval` 값을 `draft_pending_KAN-58`에서 `approved_2026-10-05_user_delegated`로 바꾼다. spec_hash가 바뀌므로 이 변경은 첫 정식 실행 전 1회만 허용하고, 이후 변경은 새 manifest(새 실험)로 취급한다.

### D4 비용
USD 상한을 제거하고 토큰·소요 시간을 기록한다. 하네스의 `cost_ceiling_usd`는 안전망용 큰 값(예: 1000)만 둔다. 단가 미지정 모델은 `estimated`로 표시하고 `observed`로 쓰지 않는다. 구독 CLI라 실제 청구액은 없다.

### D1 고위험 정책
assessment 프로필. HIGH_RISK(정답이 인계인 20개 사례)는 실행하지 않고 인계가 정답이다. 이는 정답표와 지표 정의(recovery_handoff_success)와 일치한다.

### 실행 범위의 정직한 구분
60개 사례는 주입 방식이 두 종류다.

| 구분 | 사례 | 주입 방법 | 현재 가능 여부 |
|---|---|---|---|
| A. 쿠버네티스 API만으로 주입 | bad_image 8 + selector_mismatch 8 = **16개**(F1 8, F4 8) | `kubectl set image`, Service selector 패치 | **지금 가능**. 기존 chaos/run-case.py의 검증된 주입·복구 로직이 있음 |
| B. 앱 내부 chaos API 필요 | controlled_crash 7, oom 15, latency 8, pool_hold 7, endpoint_off 7 = **44개** | chaos 프로필 + 토큰으로 앱에 장애 지시 | chaos 오버레이 적용(order/inventory/loadgen 변경·재시작, 정책 3개 추가)이 선행되어야 함 |

(분류 확인: 16 + 44 = 60. A의 bad_image 8은 F1, selector_mismatch 8은 F4. B의 15개 oom은 F2 전부.)

## 3. 단계 계획

1. **P10-a (지금)**: 16개(A) × 4 arm = 64쌍을 실제 러너·실제 주입기로 실행 가능하게 만든다. 먼저 시범 16쌍(= 4사례 × 4 arm)으로 쌍당 시간·토큰·한도 소모를 실측한다.
2. **P10-b**: chaos 오버레이를 적용하고(백업·원복 경로 포함) 44개(B)의 주입이 실제로 되는지 사례 유형별 1건씩 확인한다. 이 단계는 서비스 재시작과 order DB 연결이 필요하므로 별도 위험으로 기록한다.
3. **P11**: 시범 결과로 시간을 산정해 전체 직렬 실행. 한도에 닿으면 계정 전환, 둘 다 닿으면 대기 후 재개(API/Claude 우회 없음).

## 4. 아직 사실이 아닌 것 (이 문서가 주장하지 않는 것)

- 240쌍 결과는 0건이다. 시범도 아직 실행하지 않았다.
- 예상 총 소요: 쌍당 2~3분 가정 시 직렬 약 8~12시간(실행·원복·재시도 제외). 시범에서 실측으로 교체한다.
- 실제 Codex 한도 도달 시 동작은 모의 증거뿐이다.

## 5. 정책 변경 이력 (사용자 결정 2026-10-05) — 데이터 구분

사용자 지시: "complex까지는 Agent가 진행하게 하자. 이전 것은 다시 할 필요 없고 앞으로 나오는 케이스만."

- **정책 A (구)**: SIMPLE만 자동 조치, NORMAL·COMPLEX는 항상 운영자 인계. K8s 구간 64쌍(`.e2e-tmp/revamp-P10/store-pilot`, manifest 해당)이 이 정책으로 실행됨. 재실행하지 않는다.
- **정책 B (신)**: SIMPLE·NORMAL·COMPLEX 자동 조치 가능, HIGH_RISK는 항상 인계. NORMAL/COMPLEX의 조치는 rollout_undo 하나뿐이며 `agent_groups.rollback_proposal` 조건(독립 검증됨 + 이미지/크래시 계열 원인 + 최근 롤아웃과 이전 리비전 존재 + 서비스 라우팅 문제 없음)을 모두 만족할 때만 제안하고, 실행은 기존 safety.execute 경로(전역/사건별 approval·suggest_only가 여전히 우선)를 따른다.
- 이후 새로 실행하는 쌍(P10-b 44개 사례, P13 신규 12개)은 정책 B 기준이고 **별도 저장소**에 기록한다. 정책 A 데이터와 한 표로 합산·비교하지 않는다. 보고서에는 정책 버전을 명시한다.
- 정책 B에서 정답이 "인계"인 사례(F4·고위험)는 여전히 인계가 정답이고, 정답이 "자동복구"인 사례(F1 simple·moderate·complex 일부)에서 fixed arm이 인계 대신 조치할 수 있게 된다.
