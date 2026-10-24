# P14 실행·검증 기록

최종 결과·검증 경계·배포 digest·실측·원복은 [P14-final-verification.md](P14-final-verification.md)를 우선한다. 아래는 실패와 수정 경과를 포함한 시간순 기록이다.

## 확정 범위

- 사용자가 B1~E2 실행, 그룹별 모델 수정 화면 기획·구현, GPT Decision API/Jev(TypeSafe AI) 검토를 요청했다.
- Jira는 연결하지 않는다. 이 문서와 P14-plan-gap.md가 이번 작업 기록이다.
- 기존 Codex CLI 구독 호출만 사용한다. 비밀값/API 키를 도입하거나 고위험 자동조치 경계를 변경하지 않는다.
- 평가 결과 전용 화면은 만들지 않는다. 과거 평가의 모델·가격·정책 이력을 변경하지 않는다.

## 소유권·의존성

동시에 실행하는 각 작업자는 같은 저장소에서 아래 경로만 수정한다. 모델은 부모와 같은 gpt-6.1-sol을 사용해 다른 공급자나 API 키로 우회하지 않는다. 커밋·배포·장애 주입은 통합 담당자만 수행한다.

| 담당 | 수정 범위 | 의존성 | 완료 판단 |
|---|---|---|---|
| 근거 수집 | liveprobe/app/evidence.py, k8s.py, agent_groups.py의 E8/E9 보기 연결, tests/test_evidence*.py, test_p14_evidence.py, conftest.py, deploy/base/liveprobe/liveprobe.yaml의 읽기 RBAC | 없음 | E8/E9 값 비노출, 중복 집계, 조회 병렬화와 출처별 실패 격리 테스트 |
| 네트워크 신호 | liveprobe/app/flows.py, watcher.py, router.py, 새 network_signals.py, tests/test_network_signals.py | main.py 연결은 통합 담당 | 드롭 임계·대상 한정·중복 방지·HIGH_RISK 규칙 테스트 |
| 사례표·README | README.md, scripts/export_case_results.py, evaluation/tests/test_case_export.py, docs/plans/revamp/P14-case-table.md 및 CSV | 설정 화면 명세는 추후 동기화 | 기존 원본에서 생성·행 수/누락 검증·정책 분리, 실행 문서 최신화 |
| 기술 조사 | docs/plans/revamp/P14-technology-review.md | 없음 | 공식 출처·실제 기술 식별·Codex 제약 비교·도입/보류 근거 |
| 통합 담당 | config.py, main.py, engine.py, models.py, 모델 설정 모듈·테스트, dashboard/src 및 테스트, P14 기획/증거 문서 | 네트워크 main 연결 및 최종 문서·배포는 작업자 반환 후 | 설정 영속화·동시수정 방지·새 실행 적용·안전 검증, 브라우저 저장/재조회·375/768/1440px |

공유 읽기 인터페이스: AppConfig/WorkflowPolicy(config.py)는 통합 담당 소유. 근거 E1~E7 식별자는 보존한다. main.py는 통합 담당 소유이므로 네트워크 작업자는 필요한 연결 코드를 반환한다. README 및 기술 조사 문서는 서로 수정하지 않는다.

## 검증 경계

- 동작 변경은 실패 테스트를 먼저 실행하고 최소 구현 후 관련 테스트를 통과시킨다.
- 전체 API 테스트는 lptest-pg 실행을 확인한 상태에서 수행한다. 프런트는 npm test, npm run build 및 실제 브라우저로 검증한다.
- 배포 전 관련 Deployment/ConfigMap/RBAC를 .e2e-tmp/revamp-p14/에 백업한다. 기존 rancher-desktop만 사용한다.
- NetworkPolicy 주입은 평가가 실행 중이 아님을 확인하고 제한된 시험 대상에만 수행하며 원복·회복을 확인한다.
- 로컬 테스트 결과와 배포 실측을 구분해 기록한다. 확인하지 않은 항목은 완료 처리하지 않는다.

## 실행 증거

- 시작 상태: main, HEAD 8816bc3. 미커밋 변경은 liveprobe/app/k8s.py 73줄(이전 B1 초안)뿐이다.
- lptest-pg 실행 중, dashboard/liveprobe-api 각각 READY 1/1 확인.
- B4는 기존 커밋에 있으나 작은 화면에서 숨겨지는 문제를 이번 검증 범위에 포함한다.

### 그룹 모델 설정 — 통합 전 검증

- 기획: P14-group-model-settings.md, 화면 계약: dashboard/DESIGN.md.
- API RED: /api/model-settings가 404로 실패한 뒤 저장·재조회 구현.
- 검증 하한 RED: 결과 검증/반론/안전 검증의 부적합 설정 5종이 200을 반환해 실패한 뒤 422 거절 구현.
- 실행 스냅샷 RED: snapshot_config/apply_group 부재 및 실행 분리 부재를 확인하고 구현. 데이터베이스 손상 시험은 JSONB 깊은 복사로 실제 손상을 저장하도록 시험 준비 코드를 수정했다.
- 모델 설정+기존 API: 39 passed (11.33s). 전용 시험 DB lptest_p14_models로 다른 작업자 fixture의 DROP SCHEMA 간섭을 분리했다. 작업자 DB는 lptest_p14_evidence/lptest_p14_network다.
- 화면 RED: 그룹별 모델 제목 부재를 확인하고 구현. 모델 설정 브라우저 테스트는 375/768/1440px에서 저장·재조회/초안 보존·409/503·재시도·자동/직접 지정·기본값 초안 복원을 확인해 9 passed (5.4s). 이 테스트의 모델 설정 API는 명시적 모의 응답이며 운영 영속화 증거가 아니다.
- B4 RED: 375px에서 판단·근거 접근 버튼 부재를 확인하고 모바일 전환을 구현. 실제 기존 사건 #987을 개발 화면에서 열어 375/768/1440px 접근·가로 overflow 검사를 통과했다.
- 프런트 단위 테스트 29 passed, npm run build 성공. 기존 청크 크기 권고 경고(약 511KB)는 남아 있다.
- 네트워크 작업자 중간 변경의 network_drop_signal 필드 KeyError로 기존 평가 fixture 회귀를 관측해 해당 담당자에게 수정 요청했다. 최종 통합 테스트는 모든 작업자 반환 후 다시 수행한다.
- 개발 Vite 서버: proc_54dcc2fcdfd4, 127.0.0.1:5181. 추가 화면 검증 후 종료 확인.
- 실제 렌더 검증: P14-surface-verification.md. 별도 DB에서 저장 버전 1·새로고침 보존·버전 2 기본값 복원 확인, 1440/768/375px 캡처·키보드 이동·콘솔/네트워크 오류 0. 화면에서 발견한 모바일 메뉴/역할 순서/선택 배지 문제를 실패 테스트로 재현하고 수정했다. 이후 Playwright 24개·프런트 단위 29개 통과, 빌드 성공.
- 화면 검증 서버 5181/5182/8001은 종료했고 포트 비활성 확인. QA 데이터베이스 lptest_p14_ui는 검증 기록을 위해 보존한다.
- 이 시점에는 새 API/화면을 클러스터에 배포하지 않았다.

### 작업자 반환 후 통합

- B1~B3 근거 수집: E8/E9, ConfigMap 값 비노출, 이벤트/로그 중복 집계, 3-worker·30초 수집 제한을 통합했다. 네트워크 수집 FlowStore를 Watchers에 전달하도록 main.py 연결을 완료했다.
- 분석 중 늦게 병합된 NetworkDrop의 조치 차단을 테스트로 먼저 재현했다. 엔진 제안/실행 직전 재검사, 롤백·타입 조치 정책/승인 검사까지 보강한 뒤 관련 9개 테스트 통과.
- 기존 RBAC 테스트의 Binding 전면 금지 가정은 새 읽기 전용 Binding과 충돌했다. 대상 ns의 configmaps get/list Binding 딱 한 개만 허용하도록 명시했으며 전체 바인딩의 쓰기·Secret·pods/log 금지는 계속 검사한다.
- 전체 API suite: 597 passed in 180.32s, skip 0. 평가+chaos: 96 passed, 18 subtests passed in 11.32s.
- 사례표 생성기 --check 통과: 354행(A64/B176/C88/P13본24/재실행2), 실패 attempt 1건·smoke/trial13쌍 제외 내역 보존. 과거 모델·가격·사건 이력 불변.
- 기술 조사 원문의 제품명/엔드포인트/모델 표를 통합 담당자가 공식 OpenAI Decisions 가이드와 TypeSafe Models 페이지에서 다시 읽어 확인했다. 두 API는 별도 인증·과금 경계 때문에 미도입이며 Codex 경로를 변경하지 않았다.
- 빌드 이미지: API liveprobe/liveprobe:p14b, dashboard liveprobe/dashboard:p14b. 실제 RepoDigest와 앱 소스 일치 검사를 배포 전에 수행한다.
- 배포 전 API/dashboard Deployment 및 시험 시 변경할 allow-same-namespace NetworkPolicy를 .e2e-tmp/revamp-p14/before-*.yaml에 백업했다. 현재 rancher-desktop 단일 노드 Ready, CPU23%·메모리44%를 관측했다.
- 읽기 전용 독립 검토 2건에서 정상 Node의 보호 리소스 오분류(P1), 선택 조회 지연으로 핵심 근거 폐기(P2), 네트워크 소유자/Service 조회 무제한 대기(P2), 기본 설정 버전 기록 누락(P2), 공통 분류 모델 가격 검증 누락(P2)을 재현했다. 모두 배포 전 차단 사항으로 기록했다.

### 독립 검토 후 수정 — 배포 보류

- 근거·네트워크 3건은 단일 수정 작업자에게 맡겼다. evidence/k8s/router/network_signals/watcher와 관련 테스트만 소유하며 부모의 모델 설정 경로와 겹치지 않는다. 실제 RED→GREEN 및 지연 작업의 늦은 상태 변경 방지를 요구했다.
- 모델 설정 2건과 명세의 승격 사다리 등록 검증을 회귀 테스트로 추가했다. 최초 실행 snapshot=None, 분류 모델만 가격 없는 경우 통과, 승격 사다리에서 빠진 모델 통과를 3 failed / 14 passed로 먼저 확인했다.
- 저장 전 기본값도 revision 0으로 역할 배치를 기록한다. 분류/모든 그룹 역할에 가격 존재·유한·비음수·승격 사다리 등록 검증을 공통 적용했다. 설정/평가/group/late NetworkDrop 통합 테스트 106 passed (12.31s). 독립 재검토를 요청했다.
- 프런트 재검증: 단위 29 passed, tsc 및 Vite build 성공. 청크 512.28KB 권고 경고는 기능 오류와 별개로 남는다.
- 배포 전 실제 inventory 근거 수집 3회: 0.5569/0.2549/0.2228초, 모두 출처 오류 0. 기존 E1~E7·이벤트/로그 0행 조건이며 새 수집기와 조회 범위가 달라 엄밀한 동일 작업 성능 비교는 아니다. 원본: .e2e-tmp/revamp-p14/before-collect.json.
- 평가 lock 비활성 확인. 격리된 p14-net-probe/p14-net-sink와 값 없는 메타데이터 검증용 ConfigMap을 사용하는 한정 시험 스크립트를 준비했다. 아직 실행하지 않았다. 기존 업무 Deployment에는 장애를 주입하지 않으며, 시험 시 allow-same-namespace 선택자 한 필드만 조건부 변경하고 원복·연결 회복·소유 UID 확인 후 정리하도록 제한했다.

### 수정본 통합 재검증

- 근거/네트워크 수정 작업자의 보고를 받은 뒤 통합 담당자가 전체 API suite를 다시 실행해 615 passed (182.63s), skip 0을 확인했다. 로그: .e2e-tmp/revamp-p14/full-api-tests.log.
- 정상 Node 보조 관측은 supporting=True로 구분하되 Node/Secret/Namespace 실제 사건의 보호 규칙은 유지한다. Kubernetes 핵심 근거는 깊은 복사 체크포인트로 보존하며 선택 조회 지연은 미완료 항목에만 오류로 기록한다.
- 네트워크 owner/service는 _request_timeout=8, 관측은 잠금 대기 포함 10초 제한·adapter당 관측 worker 1개다. 기한 지난 worker의 emit/오류/watermark 변경은 금지한다. 동기 emit callback 자체의 무기한 대기를 강제 중단하는 보장은 없으며 별도 경계로 남긴다.
- 정식 liveprobe/Dockerfile 전체 빌드 성공. API p14-full RepoDigest: sha256:addea147a482ff384428c340d6afbd4cfd4db941f6141792ff2a516e33440234. 이미지 내부 main/engine/evidence/k8s/network_signals/router/model_settings의 SHA-256이 로컬 소스와 일치하고 app.main import를 통과했다. app-layer 증분 빌드만 검증한 상태는 해소됐다.
- 배포 직전 DB 백업: .e2e-tmp/revamp-p14/db/liveprobe-20261018T132651Z.dump (3,622,587 bytes), pg_restore --list 통과. Alembic 0007, 사건 988개/agent_runs 4192개; 설정과 기존 이력을 포함하며 비밀값은 출력하지 않았다.
- 수정 경계 독립 재검토 2건의 반환 전까지 배포하지 않는다.
- 그룹 모델 설정 재검토(deleg_3fa6aada)는 통과했다. 독립 메모리 검증 10묶음에서 실제 Engine.process 경로의 revision 0/역할 배치 기록, router 전용 가격 누락·NaN/무한대/음수·승격 미등록 거절, 평가 arm 우회와 실행 snapshot 불변을 확인했다. 새 보안·논리 결함은 보고되지 않았다. 실제 DB/브라우저 쓰기 검증을 대신하지 않으며 근거·네트워크 재검토는 여전히 반환 대기다.
- 근거·네트워크 재검토(deleg_0e8b166e)는 원래 3건의 해결과 비밀 표식 비노출을 확인했다. 다만 동기 emit callback의 무제한 대기를 추가 P2로 재현했다. 따라서 배포 보류를 유지하고 별도로 수정했다.
- 신호 전달 지연/실패 테스트 2개가 0.35초까지 대기하거나 기한 뒤 성공으로 보고하는 RED를 확인했다. 신호 전달 worker를 최대 1개로 제한하고 완료 대기를 폴링 deadline 안으로 묶었다. timeout은 실패가 아니라 delivery_pending으로 남겨 중복 재전송하지 않으며 감시 상태는 degraded다. 이미 시작한 intake의 외부 DB 반영은 강제 취소하지 않고 다음 폴링이 성공/실패를 확정한 뒤에만 watermark 또는 재시도를 적용한다. worker는 Future 이외의 adapter/감시 상태를 변경하지 않는다. 관련 근거/네트워크/실행 경계 54 passed (3.21s). 마지막 변경 범위만 독립 재검토를 요청했다.
- 전달 경계 최종 재검토(deleg_70710e7a) 통과: 무기한 callback에서 기본 제한 10.013초 반환, pending 1개·중복 없음·늦은 상태 변경 없음·wait/Future 경합 확인.
- 배포 뒤 실제 #989에서 LLM이 HIGH_RISK에 동의하면 COMPLEX로 떨어지는 기존 분기 결함이 발견됐다. 네트워크/보호 자원/라우팅 × 4출력 12개 중 3개 RED 후 규칙 불하향 조건을 수정했다. #989는 실패로 남기고 시험 자원/정책/모델 배치를 복원했다.
- #990 재시험은 NetworkPolicy 제한 → HIGH_RISK handed_off/조치 0 → 정책/연결 회복 → 시험 자원 제거로 통과. 실제 저장 버전 3의 astra/high·astra/xhigh 구독 호출 성공, 기본 배치를 버전 4로 복원. 최종 read-back은 사건 snapshot=3/현재 설정=4를 확인했다.
- 하이픈 앱의 Loki 질의 인용 결함도 2개 RED 후 JSON 문자열 인용으로 수정했다. 실제 200 확인. 로그 0행은 Alloy order/inventory allowlist 때문이어서 별도 실제 Pod stdout를 오프라인 전송 재생해 20줄/1,200자→1줄/80자를 확인했다. 운영 Loki 실수집 감소라고 주장하지 않는다.
- 전체 브라우저 768px 지도 상세 2건 RED를 기존 600~1100px 숨김 CSS 수정으로 해결했다. 최종 API 631 passed, 평가/chaos 96 passed+18 subtests, 프런트 단위 29 passed, 전체 브라우저 75 passed, 정식 API/프런트 빌드 통과.
- 최종 API 917366e7…/dashboard 454183d9… 배포·소스 51개 일치·readyz·network_poller running·최소 권한·정리 후 자원 부재를 확인했다. 마지막 실측에서 추가된 3개 소규모 수정은 별도 독립 검토를 요청했다.
- 마지막 소규모 수정 독립 검토(deleg_f44cd59c)도 통과했다. 관련 비DB 44개·추가 분류 조합 27개·실제 Loki 읽기·7개 폭에서 상세 패널 포인터/닫기 확인, 새 보안/논리 결함 없음. 전체 회귀 재실행과 별개의 좁은 검토다. 최종 문서·제공 화면 게이트·비밀 검사까지 동기화했다.
