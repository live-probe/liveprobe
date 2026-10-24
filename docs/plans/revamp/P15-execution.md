# P15 실행 기록

상태: 백엔드·고정 10사례 실측은 아래 당시 관측으로 보존한다. 화면은 사용자 권장안 확정 후 재구성했고 최종 2026-10-22 p15p-local을 배포했다. 레이아웃 독립 검토 지적과 후속 경계 수정을 마친 뒤 마지막 한정 재검토가 통과했고 p15p-local을 배포했다. 단위 73·모의 95/4건너뜀·실배포 읽기 전용 23/16건너뜀을 확인했으며 최신 증거·커밋은 P15-final-verification.md를 따른다. 합의한 전체 자원 변경 범위와의 차이는 미해소이며 최신 결과는 P15-final-verification.md를 따른다.
범위: `P15-ux-interview.md`, `P15-plan.md`. Jira·외부 공개·push·평가 결과 전용 화면은 범위 밖이다.

## 확인한 실행 결과

### 백업(W0)

- `.e2e-tmp/revamp-p15/`에 API·dashboard Deployment, 설정 ConfigMap, 관측 ConfigMap/Deployment, istio-system 자원·RBAC, Ingress·NetworkPolicy·VAP YAML을 저장했다.
- DB 백업: `.e2e-tmp/revamp-p15/db/liveprobe-20261018T151127Z.dump`.
- 로컬 `postgres:16`의 `pg_restore`는 서버 버전과 맞지 않아 실패했다. 서버 Pod의 `pg_restore --list`로 목록을 읽는 대체 검증은 성공했다. 복원 실행 검증은 하지 않았다.
- 인증정보·DB dump는 무시 경로에만 저장하고 내용을 출력하지 않았다.

### 백엔드 기반(W1~W6)

- migration 0008: 한글 해석 캐시·운영자 지시, 메시지 의도 필드. 상향·하향 시험 포함.
- 역할 한글 카탈로그, 원문 추출과 규칙 해석, 저비용 해석 작업기, 한글 요약 및 실행 흐름 그래프, narrative API 구현.
- 해석은 표시용이며 라우터·자동 조치 판단에 입력하지 않는다. 성공 호출의 비용을 AgentRun에 기록한다.
- 캐시·실패 격리·역할/근거 연결·흐름 분기·모델 승격 표시 시험을 통과했다.

### steering(W7)

- 명시적 중단·상위 모델·Group 변경 분류, 다음 Agent 경계 적용, 대상 역할 기록, Group 재지정 실행 시험 통과.
- HIGH_RISK 하향 금지, Group 재지정 반복 상한 유지.
- 클러스터 변경 지시는 무인 Agent 프롬프트에 전달하지 않는 회귀 시험을 추가했다. 이 지시의 별도 command 생성과 종료 후 지시 경로는 W8~W10 통합 시 연결해야 한다.
- 기존 runlog 시험의 메시지→제어 사이 새 `steering_interpreted` 이벤트 기대값만 갱신했다.

### 운영 관제 API(W11)

- `/api/overview`: 노드/Namespace/워크로드, 원문 Warning 이벤트, 사건 위치·AI 단계, Prometheus 원천 값, 토폴로지 연결선 지표.
- 미관측 값은 null로 남긴다. CPU·메모리·오류율·p95 결측을 0 또는 정상으로 바꾸지 않는다.
- 완료된 Job은 불건강 집계에서 제외하고, Pod가 없는 0-replica Deployment와 빈 Namespace도 남긴다.
- Kubernetes 인벤토리 조회와 Warning 이벤트 조회에 8초 상한을 적용했다.

### 관측 구성 준비(W15 일부)

- 주 Prometheus: 전체 Namespace의 Pod 자원 지표, 노드 root cgroup, annotation 기반 Pod 지표, 별도 Envoy 수집 작업을 준비했다. 2일/1500MB 보존 한도는 유지한다.
- KSM: 전체 Namespace, Secret 수집 권한 없음. Alloy: 전체 Namespace 로그, 기존 제한 라벨과 API 기반 수집 유지.
- Prometheus egress는 전체 Namespace의 정해진 수집 포트(8080/9090/15090/15020)로 넓혔다. 임의 포트가 모두 열리는 구성은 아니다.
- `promtool check config --syntax-only`, 7개 recording rule·8개 alert rule 검사, `alloy validate` 모두 종료 코드 0.
- 아직 클러스터에 적용하지 않았으므로 수집 target 정상 동작·Kiali 제거는 미검증이다.

## 회귀 시험

2026-10-19 01시대(KST), 전용 DB `lptest_p15_core`:

```
cd liveprobe
LIVEPROBE_TEST_DB_ADMIN=postgresql+psycopg://test:test@localhost:55432/lptest_p15_core uv run pytest \
 tests/test_p15_steering.py tests/test_runlog.py tests/test_agent_groups.py tests/test_evalrun.py \
 tests/test_router_policy.py tests/test_network_signals.py tests/test_p14_integration.py \
 tests/test_model_settings.py tests/test_p15_narrative.py tests/test_p15_interpret.py \
 tests/test_p15_interpret_worker.py tests/test_p15_interpret_hooks.py tests/test_p15_schema.py \
 tests/test_durability.py tests/test_rag.py tests/test_p15_observability.py \
 tests/test_p15_overview.py tests/test_p15_inventory.py tests/test_flows.py -o addopts='' -q --tb=short
```

실제 실행 결과: 271 passed in 31.44s, exit 0.

## 운영자 지시 실행 API 통합 후 확인

- 별도 구현 결과의 6개 파일을 읽고 지정 시험을 부모가 재실행했다: 93 passed in 20.11s, exit 0.
- `command_api.py`를 추가해 생성/목록/확인/취소/원복/Secret 입력을 연결했다. 분석 중 변경 지시와 종료 후 메시지는 자동 조치가 아니라 확인 기반 command로 보낸다.
- 확인 해시 오류·stale-plan·취소·실행·원복·잘못된 Secret 본문·본문 크기 제한·Host/Origin 방어를 HTTP 경계에서 시험했다. Secret 값은 Pydantic 입력 모델을 거치지 않으며, 잘못된 요청에도 본문을 되돌려 주지 않는다.
- 재시작 때 planning/executing/rolling_back 상태는 실패로 기록하며 자동 재실행하지 않는다. 불확정 inflight 표시도 보존한다.
- API·steering·runlog 관련 시험: 46 passed in 12.42s, exit 0.
- 백엔드 전체(아직 작성 중인 실행기 매니페스트 시험 제외): 787 passed in 211.10s, exit 0. 명령: `LIVEPROBE_TEST_DB_ADMIN=postgresql+psycopg://test:test@localhost:55432/lptest_p15_full uv run pytest tests --ignore=tests/test_p15_operator_manifests.py -o addopts='' -q --tb=short`.

### 추가로 확인한 CLI 경계

- 기존 `-s read-only`와 "파일을 읽지 말라"는 프롬프트만으로는 파일 읽기가 차단되지 않았다. Codex의 지원 feature 목록과 exec 도움말을 실제 Pod에서 확인했다.
- 새 호출 인자에 사용자 설정·규칙 상속 차단과 shell/file-image/browser/plugin/subagent/code-mode 도구 비활성화를 추가했다. 관련 LLM·계정 풀 회귀 45 passed, exit 0.
- 실제 Pod의 CLI에 새 인자를 전달해 공개용 난수 표식 파일 읽기를 시도했다. 결과: accessible=false, content=null, 표식 반환 없음, 도구 실행 완료 없음. Code Mode 비활성화에 따른 fail-closed 시작 알림 1건은 있었다.
- 최초 두 probe는 모든 error 항목을 실행 실패로 취급해 exit 1이었다. 해당 항목이 위 비활성화 알림임을 확인한 뒤 이 정확한 알림만 예상 차단으로 구분했고, 최종 probe는 exit 0이었다. 다른 오류나 실제 도구 실행을 무시하도록 바꾸지는 않았다.
- 단일 표식 시험 결과이며 OS 메모리 격리나 모든 공격 경로의 증명으로 주장하지 않는다. Secret 입력값은 저장하지 않고 참조를 제거하지만 Python 불변 문자열의 RAM 완전 소거도 보장하지 않는다.

### 아직 남은 범위 차이

- 현재 실행기 검증은 지원 목록의 일반 Kubernetes 자원에 한정된다. 임의 CR, RBAC/admission/토큰 발급, 보호 Namespace 전체, Node의 cordon/uncordon 이외 변경 등이 차단되어 인터뷰의 "Secret 외 전체 범위"보다 좁다. 이 차이를 해결하거나 명확한 미달로 남겨야 하며 전체 범위 지원 완료로 선언하지 않는다.
- 현재 command 검증은 Kubernetes API 반영 확인이다. 애플리케이션 건강·Deployment rollout 완료와 Secret 값 재조회 검증을 뜻하지 않는다.
- 실행기 독립 검토는 승인 불가였다. 부모도 원본 재현 시험을 실행해 `4 failed, 3 passed in 0.36s`, exit 1을 확인했다.
  - local PV → PVC → 새 Pod 마운트로 노드의 Secret 디렉터리를 읽을 수 있는 검증 우회.
  - StorageClass의 CSI Secret 간접 참조 매개변수가 허용되는 우회.
  - replica 1→0 축소를 서비스 중단 없음으로 설명하는 오류.
  - 실제 admission이 추가한 미승인 필드가 남아도 원복 완료·verified=true로 표시하는 오류.
- 원본 재현 파일은 `~/.hermes/cache/scratch/test_p15_independent_review.py`와 `p15_review_probe.py`이며 수정하지 않는다. 별도 수정 작업자에게 네 건만 맡겼고, 대응 영속 회귀 시험과 재검토가 통과하기 전 배포하지 않는다.
- 배포 구성 작업자에게도 PV/PVC·CSI 우회 방어를 전달했다. 기존 787건 통과만으로 위 결함이 없다고 판단하지 않는다.

- 수정 결과 수신 후 부모가 원본 scratch 시험을 재실행했다: 7 passed in 0.30s, exit 0. 전체 백엔드와 실행기 매니페스트 시험도 854 passed in 213.03s, exit 0이었다.
- 수정 내용: PV/PVC/StorageClass와 미검토 새 볼륨 연결 차단, CSI Secret 참조 키 방어, scale 양수→0의 예상 중단 명시, 실행 receipt의 값 없는 drift 표시와 미복구 drift가 있는 원복의 verified=false 처리.
- 승인 필드만 되돌린 것은 완전 원복과 구분한다. 원본 객체 전체나 미승인 값을 저장하지 않는다. 원본 scratch 재현 파일은 변경하지 않았다.
- 수정 후 실행기와 admission 구성을 별도 독립 재검토에 맡겼다. 이번 재검토는 gpt-6.1-sol / openai-codex / high로 명시해 실행했다. 판정과 실제 admission 검증 전에는 토큰을 활성화하지 않는다.

## 배포·실측(2026-10-19)

- 배포: VAP·binding → 실행 계정·RBAC·토큰 Secret → API·화면 → 관측 ConfigMap/NetworkPolicy 순서로, 바뀐 객체만 적용했다. 백업은 `.e2e-tmp/revamp-p15/deploy-20261019/`(DB dump 포함, Alembic 0007 시점).
- admission 실측: VAP `typeChecking: {}` 확인. 실행 계정 impersonation + server dry-run 16건 실패 0. hostPath·Secret 볼륨·PVC·토큰 자동 마운트·사이드카 opt-out 누락·권한 상승 Pod, 보호 namespace, 이미지 변경, scale 하위 리소스, Secret 생성은 거부. 안전 Pod·ConfigMap 생성, replicas patch, 메모리 조정은 허용. writer는 Secret patch만 가능하며 get/list/create/delete는 거부.
- Alembic 0008 적용, `interpretations`·`operator_commands` 생성. 사건 #991에서 LLM 한글 해석 17건 저장, 해석 비용 기록(gpt-6-luna/low, $0.001675).
- 관측: Prometheus 활성 대상 15개 모두 up(envoy-stats 6, kubernetes-pods 6, cadvisor·KSM·self). 수정 2건:
  - Istio가 사이드카 Pod의 수집 주석을 15020으로 바꿔 kubernetes-pods가 label_limit 초과로 실패 → 15020은 envoy-stats가 맡도록 kubernetes-pods에서 제외.
  - istiod 15014, traefik 9100, hostNetwork retina 10093 egress 차단 → 앞의 둘은 Pod 대역, retina는 기존 노드 /32 정책에만 추가.
  - p95가 앱 히스토그램이 없어 미관측 → 앱 히스토그램이 없을 때만 서버측 Envoy 지연 분포로 대체. 실측 order 31.6ms, inventory 21.7ms.
- Kiali·mesh-prometheus 제거: helm 해제와 관련 객체 삭제. 남은 객체 0. istiod·사이드카·order 200·Envoy 수집은 정상. 매니페스트 `deploy/optional/istio/{kiali-ingress,mesh-prometheus}.yaml` 삭제.
- 화면: 배포 번들과 로컬 `dist`가 바이트 단위로 일치. 재시작 합계가 워크로드 없는 namespace의 미관측 값 때문에 전체 미관측이 되던 문제 수정.
- W17 사람 개입 실측(`scripts/p15-hitm-verify.py`, sandbox `p15-sandbox`, 사건 #991):
  - 1차: 16건 중 2건 실패. ① 생성 원복(삭제)이 409 — 당시 Orphan 처리 때문이라고 판단했으나 잘못된 설명이었다. 실제로는 삭제 예행연습이 대상을 먼저 지운 결과였으며, 아래 10-21 정정에 원인을 기록한다. ② scale 0 지시가 빈 계획 — 운영자가 apiVersion을 말하지 않아 플래너가 작업을 만들지 않음.
  - 당시 우회: 같은 uid가 삭제 중이면 수락으로 보고 소멸까지 기다렸다. 이 처리는 다른 행위자의 삭제를 성공으로 오인하므로 10-21에 전부 제거했다. 플래너에 내장 종류 표준 apiVersion만 알려 주고 그 밖은 추측 금지. 빈 계획은 `plan_has_no_operations`로 한글 안내.
  - 2차: 18건 중 1건 실패. ③ Deployment 원복이 409 — 컨트롤러 status 갱신만으로 resourceVersion이 바뀌어 stale로 판정.
  - 수정: stale 판정을 uid + 원하는 상태(spec·data·metadata, status 제외)의 지문으로 바꿨다. 지문은 프로세스별 키 HMAC이라 저장값으로 내용을 추측할 수 없고, API 재시작 후에는 비교할 수 없어 stale로 안전하게 실패한다. 실제 쓰기 전제조건은 그대로 uid + resourceVersion이다.
  - 3차: 당시 18건 실패 0. 단 삭제 예행연습의 대상 보존을 검사하지 않아 잘못된 통과가 포함됐다(10-21에 보강). ConfigMap 생성→확인 전 미생성→실행→원복(삭제), scale 0(서비스 중단 예상 표시)→원복 1, Secret 키 이름만 계획→계획 밖 키 거부→값 반영(로컬 관리자 비교, 값 미출력), sentinel이 DB 12개 열·API 로그·사건 API에 없음, sandbox 정리.
  - 각 수정은 회귀 시험을 먼저 추가해 실패를 확인했다. 백엔드 전체 865 passed, 독립 검토 원본 7 passed, 화면 단위 45 passed, Playwright 57 passed.

## W18 10건 운영 경로 재현

- 방법: `scripts/p15-live-verify.py`. 평가 러너가 아니라 운영 경로(장애 주입 → 감시기가 사건 생성 → 무인 분석·조치 → 종료)로 실행. 한 번에 1건, 시작 전 노드 게이트·기준선, 종료 후 기준선 복구 확인. 인계·실패도 교체 없이 기록. 결과 `.e2e-tmp/revamp-p15/live10/results.jsonl`, 캡처 `live10/captures/`.
- OOM·풀 고갈 주입을 위해 대상 앱에 chaos 오버레이 7개 객체만 적용(P10 방식, 백업 `deploy-20261019/before-chaos-restore-*.yaml`).
- 1차(10-19): 자동 완결 8건(F1-01·F1-03·F1-09 SIMPLE 60~70초, F2-01·F2-04 COMPLEX·F2-07 NORMAL 129~156초, F3-08 NORMAL·F3-10 COMPLEX 191~211초). 모두 지도 핀 표시, 한글 해석 96~99%, 기준선 복구 정상.
  - N2-02·N3-02 첫 시도는 스크립트 결함(cases-new 경로 미지원)으로 미주입. 수정 후 재시도에서 구독 계정 2개가 모두 한도에 걸려 N2-02(#1002)는 COMPLEX 분석 중 멈춤, N3-02(#1004)는 분석 시작 전 대기 → 15분 제한 초과로 미완결 기록.
- 원인 1: 화면용 한글 해석이 분석보다 한도를 많이 사용(3시간 해석 51회·입력 59만 토큰, 라우터 12회·18만). → 보고 있는 사건만 갱신, 같은 사건 120초당 1회, 전 계정 한도 시 호출 없이 미룸.
- 원인 2: 분석이 멈춘 동안 Alertmanager 반복마다 같은 OOM 사건이 새로 열림(#1003~1016, 14건). 한도 해제 후 13건이 인계, 1건(#1003) 메모리 상향으로 해결. → 같은 원인 분류의 사건이 열려 있으면 병합 창 이후에도 병합.
- 두 수정 모두 실패 시험을 먼저 추가해 RED 확인 후 GREEN. 백엔드 전체 870 passed. API `c037a2f4…` 배포. 자동 조치가 올린 inventory 메모리 768Mi는 기준선 512Mi로 복원.
- 3번째 실행(10-20, 수정 배포 후): N2-02 #1017 COMPLEX 자동 완결 332초(신호 20회가 한 사건으로 병합), N3-02 #1018 NORMAL 자동 완결 270초(rollout_restart). 둘 다 지도 핀·한글 해석 98%·기준선 복구 정상. 2차 동안 해석 호출 2회(입력 2.5만 토큰), 중복 사건 0건.
- **최종: 10개 유형 모두 사람 개입 없이 자동 완결.** 단 N2-02·N3-02는 첫 실행 미주입(스크립트 결함)·두 번째 미완결(구독 한도) 후 원인 수정·세 번째 실행에서 완결했으며 앞선 기록은 그대로 보존한다. 두 번째 실행 캡처는 같은 파일명으로 덮어써져 사건 #1002·#1004 화면으로만 확인할 수 있다.
- 종료 후 대상 앱 chaos 오버레이 원복: 백업의 적용 구성(Deployment 3·order-config)으로 복원. chaos 경로 404, order 200, 이미지·메모리 512Mi·replica 1 기준선 확인.
- 결과 확인 페이지: `http://127.0.0.1:8765/`(`.e2e-tmp/revamp-p15/review/build.py`로 생성, 로컬 전용).

## 마지막 독립 검토 지적 및 수정(2026-10-21 KST)

- 독립 검토 `deleg_9b74b576`: 승인 보류. 기존 181개 시험은 통과했지만 새 재현에서 4개 실패·대조 8개 통과였다.
  1. 삭제 409 뒤 같은 UID의 삭제 상태를 근거로 다른 행위자의 삭제까지 실행 성공으로 기록(P1).
  2. 한도 때문에 대기한 해석이 시청 TTL 만료 후에도 실행(P2).
  3. 병합 창 밖에서 최신 열린 사건만 조회해 더 오래된 같은 원인 사건을 놓침(P2).
- 원본 재현을 영속 회귀 시험으로 편입하고 실패를 확인했다. TTL은 호출 직전 재검사하며, 시청·호출 간격 기록도 만료 시 정리한다. 사건 병합은 같은 원인 분류를 SQL에서 먼저 필터한 뒤 최신 1개를 찾는다. 사이에 다른 원인 사건이 55개 있어도 병합하는 회귀를 포함했다.
- **삭제 예행연습 원인 정정:** Kubernetes는 DELETE 본문이 있으면 URL의 DeleteOptions를 읽지 않는다(`kubernetes/v1.36.2`, `handlers/delete.go:95~125`). 기존 코드는 본문에 UID/RV 전제조건을 넣되 dryRun은 URL에만 넣어, 예행연습이 실제 삭제로 동작했다. 이것이 뒤이은 409의 원인이었으며 Orphan 자체의 문제가 아니다.
  - 임시 namespace에서 실행기 경로와 kubectl 서버 예행연습을 대조했다. 잘못된 경로는 4가지 propagationPolicy 모두 실제 삭제됐고, kubectl 서버 예행연습은 원본을 보존했다. 시험 자원은 정리했다.
  - 수정: `DeleteOptions.dryRun = ["All"]`을 본문에 포함. 실제 삭제에는 포함하지 않는다. 삭제 409를 성공으로 바꾸던 처리는 전부 제거했다. 2xx 수락 후에만 실제 소멸을 기다린다. UID+RV 전제조건과 Orphan 정책은 유지한다.
  - POST/PATCH 및 Secret 쓰기는 같은 DELETE 본문 해석 경로를 사용하지 않음을 확인했다.
- 중간 수정본 `p15g`는 W17 4차에서 ConfigMap 삭제 원복 실패(17/18 통과)로 보존했다. 단순히 결과가 소멸했다는 이유로 성공 처리하지 않고 근본 원인을 수정했다.
- 최종 확인:
  - 관련 회귀 107개 통과(22.47초), 원본 독립 재현·대조 12개 통과(3.25초).
  - 백엔드 전체 880개 통과(253.25초, exit 0). 이미지 가져오기 검사 통과. 화면 단위 45개·형 검사/빌드·모의 API Playwright 57개도 재실행 통과(실클러스터 브라우저 57건이라는 뜻은 아님).
  - API `liveprobe/liveprobe@sha256:434e6311c4f41d57fe8ec18b4c55f22b8d3e1b47ba4f43e25b4abfa741e66ff7` 배포, rollout 및 선언 이미지 일치 확인. 직전 배포 백업 `deploy-20261019/before-api-h.yaml`.
  - W17 5차 **22/22 통과**(`hitm-run5/hitm-result.json`): 기존 18항목 + 명시 DELETE 계획 확인 대기·확인 전 UID/RV/내용 불변·확인 후 소멸·새 UID/내용 복원. Secret 표식 미노출과 sandbox 정리도 재확인.
- 영향 범위 확인: 현재 저장된 운영자 지시 16건의 계획 자원은 모두 `p15-sandbox`였다. 다른 namespace를 대상으로 한 저장된 지시 0건. 이는 저장된 지시의 점검 결과이며, 모든 과거 실행의 무결성 증명을 뜻하지 않는다. 임시 시험 namespace 잔여 0개, 대시보드와 readyz는 200.
- 독립 재검토 `deleg_0aa81076` 통과: 관련 회귀 120개·새 독립 경계 시험 31개, 건너뜀·오류 0. 부모가 XML 집계와 핵심 소스 3개의 SHA-256 일치를 확인했다. W17 스크립트는 설명 문자열만 달라졌음을 확인했다. 원격 CI·전체 백엔드·실클러스터 실측은 검토자의 수행 범위가 아니며 부모 결과와 구분한다.
- 커밋 전 계약 점검에서 오류율 비율을 그대로 %로 표시하는 결함을 확인했다. API 원천은 0~1 그대로 보존하고 화면 자원·연결선에서만 100을 곱하도록 수정했다. 새 브라우저 회귀 수정 전 실패 → 수정 후 통과, 단위 46개·빌드·모의 API 브라우저 60개 통과. 화면 p15i(d50da078…) 배포, 번들 일치 확인. 마지막 표시 수정은 `deleg_c3f762c7`에서 읽기 전용 검토 중이다.
- 백엔드·관련 배포 구성은 독립 검토 통과 후 `efcd0a3`으로 커밋했다. 화면·최종 기록은 마지막 표시 검토 결과 반영 후 마감하며 push는 하지 않는다.
- 현재 실배포 재조회 35항목 통과(활성 수집 대상 14/14 정상). 검토·W17·W18·재조회 요약을 docs/evidence/P15/에 보존했다. 최종 범위·미수행은 P15-final-verification.md 참조.
- W18 10건 전체는 이 마지막 수정본에서 다시 실행하지 않았다. 앞서 완료한 실측은 당시 배포본의 관측 근거로 보존한다.

## 병렬 구현 중 이력(아래 미완료 문구는 당시 상태)


- 수정 후 재검토(gpt-6.1-sol)는 passed=false였다. app 4건은 통과했고, admission에서 새 결함 3건이 나왔다.
  - 생략된 `request.subResource`·`request.namespace` 직접 접근 → `no such key` → failurePolicy Fail로 정상 요청 6종까지 거부.
  - StatefulSet `volumeClaimTemplates`로 컨트롤러가 PVC를 대신 만드는 저장소 우회.
  - 기존 ephemeral-storage 같은 CPU/메모리 외 리소스가 있으면 scale·memory 조정까지 거부.
- 수정 작업 위임은 Codex 사용량 한도(HTTP 429)로 실패했다. 부모가 다음 날 직접 수정했다.
  - RED: k8s.io/apiserver v0.36.2 CEL 평가기(`~/.hermes/cache/scratch/p15-rereview/run_current.py`)에서 15건 중 9건 실패, exit 1. 정적 시험 1건 실패.
  - 수정: 두 정책 모두 `has()` 가드 적용, 생성 시 claim 템플릿 비어 있을 때만 허용, CPU/메모리 외 리소스 키·값은 이전과 같아야 함(추가·삭제·변경 거부).
  - GREEN: 같은 평가기 15건 실패 0(허용 대조 9, 거부 6). 각 거부가 의도한 검증 항목에서 나왔다. 기존 동적 fixture 343건 실패 0. 관련 시험 37 passed·2 skipped. 생성기 정적·discovery 23규칙 exit 0. kustomize exit 0. 서버 dry-run 선언 4개 수락, 실제 클러스터에는 미생성.
  - 한계: 로컬 평가기는 live enforcement 증거가 아니다. 활성화 후 실제 거부·허용 사례를 다시 확인한다. Go 평가기는 시험 의존성 추가를 피하려고 scratch에만 둔다.

- 화면: 위임한 작업자가 `dashboard/**`에 운영 관제·장애 관제실·명령 카드와 모의 API 시험을 구현 중이다. 부모가 결과를 재검증하기 전에는 완료로 보지 않는다.
- 운영자 지시: 별도 구현 결과와 API 통합 시험은 통과했다. 독립 보안 검토 결과에 따른 수정이 남았다.
- 배포 구성: 지정 5개 파일의 구현 결과를 받았다. 부모 재실행에서 관련 시험 39 passed in 13.73s, 생성기 discovery 23개 규칙 일치·정적 검사 모두 exit 0이었다.
- 매니페스트의 실제 표현식이 CEL 검증 입력과 일치함을 비교한 뒤 재실행했다: 34개 표현식 컴파일·343개 동적 fixture·실패 0. 첫 비교에서는 policy 2개와 binding 포함 입력 4개를 잘못 대조해 중단했으며, 같은 4개 선언을 대조한 재실행에서 일치를 확인했다.
- API 서버 dry-run은 admission 선언 4개를 수락했고 전후 미존재를 확인했다. typeChecking은 응답에 없으므로 실제 활성화·타입 검사·차단 동작의 증거가 아니다. 기존 클러스터에는 새 객체를 만들지 않았다.
- 새 워크로드는 토큰 자동 마운트·권한 상승·사이드카 자동 주입을 명시적으로 끄는 제약이 있고, 기존 워크로드는 scale·CPU/메모리·restart만 허용한다. 스토리지·Namespace 쓰기 제한을 포함한 합의 범위와의 차이는 계속 미해소로 남긴다.
- 부모의 다음 작업: 화면·보안 검토·배포 구성 결과 수신 → 지적 수정과 범위 차이 정리 → 통합 회귀 → 이미지 빌드·범위 배포 → Secret 미노출·사람 개입 자동 시험 → 고정 10건 실제 장애 재현 → 최종 독립 검토.
- Secret 우회 경로(새 Pod에 Secret 마운트, 토큰 발급, privileged/hostPath, 권한 상승)까지 막아야 하므로 `Secret get/list/watch 없음`만으로 보안 완료를 선언하지 않는다.
- 사람 개입 실제 시연은 사용자 요청 시에만 한다. 자동 시험과 모의 화면 확인은 진행한다.
- 10건 사례표와 기존 평가 354행·채점 장치는 수정하지 않았다.
