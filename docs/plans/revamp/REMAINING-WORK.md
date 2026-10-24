# LiveProbe 개편 — 남은 작업과 확인 방법

기준: docs/plans/liveprobe-revamp-plan.md (P0–P13). 갱신: 2026-10-11.

## 진행 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| P0 | 환경 조사(기존 클러스터, Retina) | 완료 |
| P1 | 흐름 수집기 설치 스크립트 | 완료 |
| P2 | 로그인 제거, Host/Origin 방어 | 완료 |
| P3 | 흐름 수집, /api/topology | 완료 |
| P4 | 경고 판정(drop 5건 이상, 사건) | 코드 완료, **실제 차단 경고·알림은 미확인** (P9) |
| P5 | 실행 기록·대화형 피드·개입 | 코드 완료, **실제 진행 중 사건으로는 미확인** (P9) |
| P6 | Codex 계정 풀 | 코드 완료, **예비 계정 로그인 없음**, 실제 한도 오류 형식 미확인 |
| P7 | 정책 프로필·검토 기록 | 완료 (assessment 기본) |
| P8 | 대시보드(Ingress→서비스→DB 흐름도) | 완료 (1440/1280/390 검증) |
| P9 | 통합 검증(장애 주입→감지→알림→처리→원복) | **차단: 실제 LLM 호출 중지 지시** |
| P10 | 평가 준비·시범 16쌍 | 대기 |
| P11 | 240쌍 실행 | 완료(정책 A 64쌍 + 정책 B 176쌍, `P11-evaluation-results.md`) + 정책 C 보완 88쌍(`P12-policyC-results.md`) |
| P12 | 결과보고서·판단 참고자료서 | 완료(`P12-final-report.md`, `docs/reference/liveprobe-judgement-guide.md` 유출 검사 PASS·RAG 색인 sha 2fa8005b) |
| P13 | 신규 12개 사례 검증 | 완료(`P13-results.md`: 참고자료 미사용 10/12, 사용 9/12, 차이 1건은 지표 수집 차이로 개선 근거 없음) |

## 사용자만 할 수 있는 일

1. 실제 Codex 호출 중지 해제 승인 (P9 이후 전부 필요)
2. 두 번째 Codex 계정 로그인: `CODEX_HOME=~/.codex-b codex login`
   이후 `CODEX_SPARE_AUTH_FILE=~/.codex-b/auth.json scripts/create-secrets.sh` 와 API 재시작
3. 장애 주입 승인(기존 클러스터의 liveprobe-target 대상)

## 알려진 위험·한계

- 백엔드 이미지는 의존성 다운로드 시간 초과로 전체 재빌드를 못 해, 이전 이미지 위에 app 계층만 덧씌워 배포함. 깨끗한 전체 빌드는 미검증.
- 흐름 정보는 L3/L4(HTTP 아님). "요청 수"가 아니라 "흐름 이벤트 수".
- 단일 노드라 Node 재시작·drain 시험 제외.
- 변경 사항 전부 커밋되지 않음(git 미추적 13개 경로).
- LAN에서 대시보드 접근 가능(사용자 승인).

## 확인 주소

- 대시보드: http://liveprobe.localhost
- Grafana: http://grafana.localhost
- 상태: /api/readyz, /api/topology, /api/llm/accounts, /api/policy/profile

## Rancher 재시작 후 점검 명령

```
kubectl --context rancher-desktop get pods -A | grep -vE "Running|Completed"
curl -s http://liveprobe.localhost/api/readyz
```

Retina 이미지는 반드시 로컬에 있어야 함(imagePullPolicy IfNotPresent). 재시작 직후 DNS 지연으로 ghcr.io 풀이 실패한 사례 있음.
