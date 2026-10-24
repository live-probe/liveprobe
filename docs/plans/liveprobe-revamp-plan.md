# LiveProbe 관제 개편 · 실제 240쌍 평가 · 참고자료 구축 상세 계획

상태: 진행 중. D1~D8 권장안 확정, D2는 사용자 지시로 변경(P0 결과: docs/plans/revamp/P0-feasibility.md). 이 문서는 계획이며 구현·배포·장애 주입·실제 AI 호출의 승인이 아니다.
작성일: 2026-10-01
입력: `docs/plans/dashboard-revamp-interview.md`(인터뷰 6문항 확정), `과제계획서.md`, `docs/plans/evaluation-60cases-4arms-plan.md`, 저장소 코드 조사 결과.
작업 추적: Jira 사용 안 함(사용자 지시). 이 문서의 단계(P0~P13)와 완료 기준이 작업 추적의 기준이다. 결정 변경은 `.hermes/plans/decision-log.md`에 기록한다.

---

## 1. 최종 목표 (완료 시 참이어야 하는 것)

1. 로컬 컴퓨터에서 로그인 없이 `http://liveprobe.localhost`(또는 127.0.0.1 포트)로 바로 관제 화면에 들어간다.
2. 첫 화면에 전용 PoC 클러스터의 전체 자원(Node, Namespace, Workload, Pod, Service, Ingress, 외부 목적지)이 지도로 보이고, 실제 수집된 네트워크 흐름(허용·실패·차단, 네임스페이스 간, 외부 연결)이 선으로 표시된다. 구성 관계와 실제 관측 흐름은 시각적으로 구분되고, 관측 안 된 구간은 "미관측"으로 표시된다.
3. 클러스터에서 이상이 발생하면 화면 알림(토스트 + 브라우저 알림)이 오고, 지도에서 영향 자원에 경고 배지가 뜬다. 배지를 누르면 해당 사건 작업 화면으로 바로 이동한다.
4. 사건 작업 화면은 ChatGPT 같은 대화형 피드로 LangGraph 에이전트의 진행(근거 수집 → 사건 수준 판단 → Agents Group 구성 → 역할별 분석 → 검증 → 조치 → 결과 확인)을 보여주고, 운영자는 메시지로 방향을 조정·일시정지·중단할 수 있다. 개입하지 않으면 끝까지 자동 진행한다.
5. 제품 LangGraph 에이전트의 모든 LLM 호출은 Codex CLI로만 이뤄지고, 주 계정 한도 도달 시 예비 계정으로 전환하며, 둘 다 막히면 상태를 저장하고 대기한다. API·Claude로 전환하지 않는다.
6. PoC 클러스터에서는 고위험 작업(Node 재시작·drain, 클러스터 RBAC·네트워크 기반 변경, LiveProbe·관측 시스템 재시작 포함)도 사전 정의된 형식화 조치로 실행되고, 각 작업은 "운영자 검토 권장" 기록으로 별도 축적된다.
7. 디자인은 밝고 절제된 통합관제 화면(ChatGPT 라이트 모드 참고)이며, 사용자가 화면 검수에서 승인한다.
8. 위 기능 완료 후 60개 사건 × 4방식 = 240쌍을 실제 Codex 호출·실제 장애 주입으로 직렬 실행하고, 쌍마다 기준선 회복이 확인된 기록이 남는다.
9. 240쌍 결과로 (a) 과제계획서 형식의 결과보고서, (b) 향후 AI 판단용 참고자료(검색·인용 가능한 판단 가이드)를 만든다.
10. 참고자료의 효과는 240쌍에 포함되지 않은 새 장애 사례에서 참고자료 사용/미사용 비교로 검증한다. 새 사례 정답은 참고자료와 에이전트 입력에 들어가지 않는다.

## 2. 하지 않는 것 (비목표)

- 외부망·사내망 공동 접근, 사용자 계정·권한 체계(향후 요청 시 별도 도입).
- 제품 에이전트의 API 키 사용, Claude 사용(Claude는 Hermes 오케스트레이션 전용).
- Secret 값 변경·노출, 영구 데이터(PV·DB 데이터) 삭제, 임의 Control Plane 변경, 자유형 kubectl/YAML/셸 실행.
- 기존 공유 클러스터(rancher-desktop 기본 클러스터)에서의 고위험 조치. 고위험 실행은 재생성 가능한 전용 PoC 클러스터에서만 허용한다.
- Istio 서비스 메시 도입(아래 3.2 선택 근거 참고).
- 비공개 내부 사고 과정 표시. 화면에는 관측 근거·단계·도구 작업·판단 요약·결과만 표시한다.
- 가짜 실행기·합성 fixture 결과를 평가 완료로 세는 것.

## 3. 저장소에서 확인한 현재 상태와 차이

| 영역 | 현재 (확인한 근거) | 필요한 변화 |
| --- | --- | --- |
| 대시보드 | React 19 + Vite + react-router, `dashboard/src/App.tsx` 한 파일에 Login·사건 목록·상세·설정. 401 시 `/login` 이동(`api.ts`) | 로그인 제거, 지도 첫 화면, 사건 대화형 작업 화면, 디자인 시스템 재구성 |
| API 인증 | `liveprobe/app/auth.py` 쿠키 로그인, `require_user`, `/api/login`·`/api/logout`·`/api/me`, 설치 시 관리자 비밀번호 파일 필요(README) | 사용자 인증 제거. Alertmanager 웹훅 인증(`require_webhook`)은 서비스 간 인증이므로 유지 |
| 네트워크 흐름 | `deploy/`에 Istio·Cilium·Hubble·OTel 없음(검색 0건). Prometheus·KSM·Loki·Alloy·Grafana·Alertmanager만 존재 | 흐름 수집 계층 신규 구축 |
| 클러스터 | 현재 컨텍스트 `rancher-desktop`. 조사 시점 `kubectl get nodes` 응답 없음(클러스터 미기동 또는 접근 불가). kind 0.30.0, docker 29.5.3-rd 설치. cilium/hubble CLI 없음. helm 설치 여부 불명확 | 전용 PoC 클러스터(kind + Cilium) 생성 스크립트 |
| 호스트 자원 | CPU 10, 메모리 16 GiB | kind 3노드 + Cilium + 관측 스택 + LiveProbe가 들어갈지 P0에서 측정 |
| LangGraph | `workflows.py`(SIMPLE·HIGH_RISK), `agent_groups.py`(NORMAL·COMPLEX, 공통 메시지 형식·인용 검증·호출 한도). 체크포인트·중단·운영자 메시지 기능 없음. 재시작 시 진행 중 사건은 failed 처리(`engine.recover_on_startup`) | 실행 기록 이벤트화, 체크포인트, 노드 경계 개입 |
| 조치 | `safety.py`: HIGH_RISK는 어떤 설정에서도 조치 금지. `actions.py`: pod_delete, rollout_restart, scale_replicas, set_memory_limit, configmap_restore만, Node·RBAC·네트워크 조치 없음 | PoC 자율 정책 프로필 + 고위험 형식화 조치 + 검토 기록 |
| Codex | `llm/codex_cli.py`: `codex exec --ephemeral -s read-only`, CODEX_HOME 하나. 인증 오류만 분류, 한도 초과 분류·계정 전환 없음. codex-cli 0.137.0 설치 | 2계정 풀, 한도 오류 분류, 대기 상태 |
| 평가 | 60개 사건(F1~F4 각 15) 확인, `LABELS.json` status `approved`. 하네스는 dry-run만 허용(`NotAuthorized`), 가짜 실행기만 존재, USD 비용 상한 필수. 4방식 모델 값 `draft_pending_KAN-58` | 실제 실행기·장애 실행기, 비용 상한 대체, 방식 확정 |
| 지표 | `evaluation/metrics/definitions.py` `kan59-metrics/v1`: HIGH_RISK 정답은 "이관"만 성공 | PoC 고위험 실행 정책과 충돌 → 정책 프로필 분리로 해결(결정 D1) |

### 3.1 외부 자료 확인 (2026-10-01 조회)

- Cilium 공식 문서(Network Observability with Hubble, 1.20.2): Hubble은 노드 단위 흐름을 제공하고, Hubble Relay를 배치하면 클러스터 전체 가시성을 제공한다. Hubble UI는 L3/L4 및 L7 서비스 의존 지도를 자동 발견한다. Hubble exporter로 흐름 로그를 내보낼 수 있다.
- Kiali 공식 문서(Topology): 그래프는 Istio 설정과 실시간 요청 트래픽을 결합한다. 즉 메시에 포함된 워크로드의 트래픽 위주다.
- Codex CLI 공식 문서: `--profile`로 `$CODEX_HOME/<profile>.config.toml` 계층 적용, `codex exec`는 JSONL 출력과 세션 재개 지원, 모델 카탈로그 출력 명령 존재.
- 미확인(구현 전 P0에서 확인): Codex 구독 한도 초과 시 실제 오류 문자열·재설정 시각 제공 여부, 구독 계정에서 사용 가능한 정확한 모델 이름, kind + Cilium이 이 Mac(arm64, Rancher Desktop moby)에서 정상 동작하는지, Hubble L7 가시성의 조건.

### 3.2 네트워크 흐름 수집 방식 선택

| 선택지 | 장점 | 단점 | 판단 |
| --- | --- | --- | --- |
| A. Cilium(CNI) + Hubble Relay + Hubble exporter → Alloy → Loki | 시스템 네임스페이스 포함 전체 L3/L4, 차단(DROPPED)·정책 판정·외부 연결 관측. 흐름이 로그로 남아 "로깅에서 가져온다"는 요구와 일치 | CNI 교체 필요 → 기존 클러스터 재구성 불가, 전용 클러스터 필요 | 채택 |
| B. Istio + Kiali | Istio 화면과 가장 비슷한 HTTP 지표 | 메시 밖(시스템·관측 도구) 미관측, 차단된 연결은 네트워크 계층에서 안 보임, 사이드카 자원 부담 | 기각 (전체 관측 요구 미충족) |
| C. 앱·Ingress 로그 + Prometheus 지표로 선 추정 | 기존 스택으로 가능 | 실패·차단 연결 불가, 추정치를 관측처럼 보이게 될 위험 | 기각 (보조 정보로만) |
| D. 자체 eBPF 수집기 | 완전 통제 | 개발량 과다 | 기각 |

전용 클러스터는 인터뷰 4번 답변(재생성 가능한 격리 PoC 클러스터)과도 일치한다. kind 멀티노드(제어 1 + 워커 2)를 기본으로 제안한다. kind 노드는 Docker 컨테이너이므로 "Node 재시작"은 클러스터 밖 호스트 실행기가 컨테이너를 재시작하는 방식으로 구현한다(외부 복구 경로와 같은 위치).

## 4. 목표 구조

```
[호스트 Mac]
  ├─ 호스트 실행기 (node 재시작, 클러스터 재생성, 독립 실행 기록 ~/.liveprobe-poc/records)
  └─ Docker
      └─ kind 클러스터 liveprobe-poc (Cilium + Hubble Relay)
          ├─ liveprobe-system: API(FastAPI) · 엔진(LangGraph) · 흐름 집계기 · 대시보드(nginx)
          ├─ liveprobe-observability: Prometheus · KSM · Alertmanager · Loki · Alloy · Grafana
          ├─ liveprobe-target: inventory · order · DB (시험 대상)
          └─ kube-system: Cilium · Hubble Relay · CoreDNS
흐름: Hubble exporter(JSON) → Alloy → Loki → LiveProbe 흐름 집계기(LogQL, 10~15초 주기) → /api/topology
지표: Hubble metrics(drop 등) → Prometheus → Alertmanager → LiveProbe 웹훅 → 사건
상태: Kubernetes API watch → 자원 목록·구성 관계
AI: 엔진 → Codex 계정 풀(주/예비 CODEX_HOME) → codex exec
화면: SSE(사건·실행 이벤트·지도 갱신) → 대시보드
```

## 5. 핵심 설계 결정

### 5.1 인증 제거와 로컬 접근 제한

- 삭제: 로그인 화면, `/api/login`·`/api/logout`·`/api/me`, 쿠키 세션, 설치 시 관리자 비밀번호 요구, 401 리다이렉트.
- 유지: Alertmanager → LiveProbe 웹훅 토큰, LiveProbe ServiceAccount RBAC, 조치 실행기 별도 신원, Secret 보호.
- 인증이 없어지면 브라우저의 다른 사이트가 로컬 API에 요청을 보내는 공격(CSRF, DNS rebinding)이 가능해진다. 대응:
  - Host 헤더 허용 목록(`liveprobe.localhost`, `127.0.0.1:<port>`) 외 요청 거부.
  - 변경 요청(POST/PUT/DELETE)은 Origin 일치 + 사용자 지정 헤더(`X-LiveProbe-Client`) 필수.
  - 노출 경로는 127.0.0.1 바인딩만 허용. kind의 포트 매핑을 `listenAddress: 127.0.0.1`로 고정하고 0.0.0.0 노출이 없음을 검증.
- 감사 기록 actor는 `local-operator`로 고정.
- 사용자 결정(2026-10-01): Rancher Desktop `ingress.localhostOnly=false`로 사내망 PC에서도 접근 가능한 상태를 허용한다("열려도 된다"). 따라서 "127.0.0.1 바인딩만" 검증 항목은 삭제하고, Host 허용 목록에 LAN 접근용 호스트를 쓰려면 `LIVEPROBE_ALLOWED_HOSTS`로 추가한다. 인증이 없으므로 같은 망 사용자는 조치 승인까지 가능하다는 점이 수용된 위험이다.

### 5.2 클러스터 지도

- 화면 단위: 기본 보기는 Namespace 묶음 안에 Workload(Deployment·StatefulSet·DaemonSet) 노드, Service·Ingress·외부 목적지 노드. Pod·Node 보기는 전환 또는 확대 시 표시.
- 선 종류: 구성 관계(점선, Service→Workload selector, Ingress→Service, 소유 관계)와 관측 흐름(실선, 최근 5분 Hubble 흐름). 실선 색: 정상(회색 계열), 오류(L7 5xx·연결 실패, 주황), 차단(DROPPED, 빨강).
- 신선도: 마지막 흐름 수신 60초 초과 시 상단에 "흐름 관측 지연" 표시, 선은 흐리게. 미관측 구간을 정상으로 그리지 않는다.
- 선택 시 우측 패널: 자원 상태, 최근 흐름 통계(요청 수, 차단 수, 상위 상대), 관련 사건, Grafana·Loki 링크.
- 라이브러리 제안: `@xyflow/react` + `elkjs`(자동 배치). 신규 의존성이므로 P8 시작 시 번들 크기와 성능(노드 300개, 선 1000개에서 60fps 근접) 측정.

### 5.3 알림과 경고 표시

- 사건 생성·수준 변경·조치 실패·검토 권장 조치 실행 시 SSE 이벤트 발생.
- 화면 알림: 토스트 + 브라우저 Notification API(사용자가 처음 허용 시). 외부 메신저 알림은 범위 밖(가정 A3).
- 지도: 사건의 영향 자원 키(`namespace/kind/name`)에 경고 배지(!). 수준별 색: SIMPLE·NORMAL 주황, COMPLEX·HIGH_RISK 빨강. 해결되면 배지 해제, 최근 해결은 회색 기록으로 10분간 유지.
- 네트워크 차단 급증(Hubble drop 지표)은 지도 경고로 표시하고 일반 사건으로 생성한다. 단 240쌍 평가 대상 Family는 기존 4종 그대로다.

### 5.4 대화형 AI 작업 화면과 운영자 개입

- 실행 이벤트 형식(`run_event/v1`): `evidence_collected`, `routing_decided`, `group_composed`, `agent_started`, `agent_finished`(요약·근거 인용·모델·추론 강도·토큰·시간), `escalated`, `action_proposed`, `action_executed`, `verification_result`, `review_recommended`, `operator_message`, `operator_applied`, `paused`, `resumed`, `stopped`, `account_switched`, `waiting_quota`, `completed`.
- DB에 순서 보장 저장 → SSE로 재생 가능(새로 고침해도 같은 피드).
- 운영자 메시지 반영 시점: 다음 LangGraph 노드 경계. 진행 중인 Codex 호출은 1회성 실행이므로 중간 주입하지 않는다. 메시지는 다음 에이전트 입력의 "운영자 지시" 칸에 들어가며, 근거가 아니라 지시로 표시된다.
- 일시정지: 다음 노드 시작 전과 모든 조치 실행 직전에 멈춘다. 이미 시작된 쓰기 조치는 끝까지 수행하고 검증·원복까지 마친 뒤 멈춘다.
- 중단: 진행 중 Codex 프로세스 종료, 미실행 조치 취소, 실행된 조치는 검증·원복 규칙대로 처리, 사건은 `handed_off`(사유: 운영자 중단).
- 체크포인트: LangGraph 체크포인터(Postgres)를 추가해 일시정지·계정 대기 후 재개를 지원한다. 신규 의존성 `langgraph-checkpoint-postgres` 필요. 재시작 시 "failed, 재실행 안 함" 원칙은 조치 단계에는 유지하고, 분석 단계는 체크포인트에서 재개 가능하게 바꾼다.

### 5.5 Codex 계정 풀

- 계정별 별도 CODEX_HOME(Secret 두 개, 볼륨 두 개). 인증 파일 값은 저장소·DB·로그·대화에 남기지 않는다. 생성은 기존 `scripts/create-secrets.sh` 흐름을 계정별로 확장.
- 호출 단위: 에이전트 1회 호출. `codex exec --ephemeral`은 상태가 없으므로 실패한 호출을 예비 계정으로 그대로 재시도할 수 있다.
- 오류 분류 추가: `quota`(사용 한도), `auth`, `timeout`, `provider`. `quota` 판정 문자열은 P0에서 실제 출력으로 확인 후 설정 파일로 둔다(추측 문자열 금지).
- 전환 규칙: 주 계정 `quota` → 예비 계정 재시도 → 예비도 `quota` → 실행 상태 `waiting_quota` 저장, 사건 피드에 표시, 재설정 시각이 있으면 그 시각, 없으면 30분 간격 재확인(재확인 자체도 호출 비용이 드므로 최대 횟수 기록). API·Claude 전환 없음.
- 중복 조치 방지: 조치는 기존 워크로드 잠금(`lock_key`)과 조치 기록으로 멱등 처리. 계정 전환은 분석 호출에만 영향.
- 제품 설정 검증: PoC 프로필에서 `llm.provider != codex_cli`이면 기동 거부. 다른 제공자 코드는 삭제하지 않고 비활성(변경 범위 최소화).

### 5.6 정책 프로필 (결정 D1)

| 프로필 | 사용처 | HIGH_RISK 처리 | 근거 |
| --- | --- | --- | --- |
| `assessment` | 240쌍 공식 비교 평가 | 과제계획서 원칙대로 자동 조치 중지·운영자 이관 | 과제계획서 목표("고위험/저신뢰만 사람에게 이관", 정책 위반 0건)와 승인된 정답표(`LABELS.json` approved)를 그대로 쓸 수 있음. 정답표 재작성 불필요 |
| `poc_autonomy` | 라이브 관제 시연, 고위험 시나리오 시험, 새 사례 검증 | 형식화된 고위험 조치 실행 + 검토 권장 기록 | 인터뷰 3·4번 답변 |

- `poc_autonomy`는 클러스터 신원 표식(kube-system UID + `liveprobe-poc` ConfigMap 지문)이 전용 PoC 클러스터와 일치할 때만 활성화된다. 다른 컨텍스트에서는 자동으로 `assessment`로 내려간다.
- 과제계획서 문구 수정은 하지 않고, 결과보고서에 "PoC 자율 실행 시험"을 별도 장으로 둔다.

### 5.7 고위험 형식화 조치 (poc_autonomy 전용)

모든 조치는 고정 파라미터 형식, 실행 전후 대상 재검증, 스냅샷, 낙관적 동시성, 실행 후 검증, 보상 원복, 감사 기록, 검토 권장 기록을 가진다.

| 조치 | 대상 제한 | 원복 | 실행 주체 |
| --- | --- | --- | --- |
| `node_cordon` / `node_uncordon` | kind 워커 노드만, 제어 노드 금지 | uncordon | 클러스터 실행기 |
| `node_drain` | 워커 노드, 시험·LiveProbe 네임스페이스 Pod만 축출, PDB 준수, 시간 제한 | uncordon + 재배치 확인 | 클러스터 실행기 |
| `node_restart` | kind 워커 컨테이너 | 컨테이너 기동 + Ready 확인 | 호스트 실행기 |
| `component_restart` | LiveProbe·관측 스택 Deployment 허용 목록 | 상태 확인, 실패 시 이전 ReplicaSet | 클러스터 실행기 |
| `networkpolicy_apply` / `networkpolicy_restore` | 저장소에 미리 정의한 정책 템플릿만 | 이전 정책 스냅샷 복원 | 클러스터 실행기 |
| `rbac_binding_apply` / `rbac_binding_restore` | 미리 정의한 RoleBinding/ClusterRoleBinding 템플릿만, cluster-admin 부여 금지 | 이전 바인딩 복원 | 클러스터 실행기 |
| 기존 조치 | rollout_undo, pod_delete, rollout_restart, scale_replicas, set_memory_limit, configmap_restore | 기존 규칙 | 기존 |

검토 권장 기록(`review_ledger`): 사건, 조치, 대상, 위험 판단 근거, 예상 영향, 실행 전 스냅샷 요약, 실행 결과, 검증, 원복 여부, 운영자 개입 여부, 에이전트별 의견. 대시보드에 "검토 기록" 화면과 CSV/JSON 내보내기를 둔다.

### 5.8 디자인

- 기준: 흰 배경, 중립 회색 4~5단계, 강조색 1개, 상태색(정상·주의·위험)만 채도 사용. 시스템 글꼴(Pretendard 또는 시스템 산세리프, 한글 우선), 8px 간격 체계, 얇은 경계선, 그림자 최소화.
- 금지: 그라디언트 남용, 장식용 이모지, 의미 없는 카드 나열, 영문 대문자 장식 문구(현재 `INCIDENT MANAGEMENT / 01` 같은 표기), 가짜 숫자.
- 화면 구성: 좌측 얇은 내비(지도 · 사건 · 검토 기록 · 평가 · 설정), 상단 클러스터 상태 줄(노드 Ready 수, 흐름 신선도, 열린 사건 수, Codex 계정 상태), 본문.
- 사건 작업 화면: 좌측 대화형 피드(ChatGPT식 말풍선·단계 카드), 우측 맥락 패널(영향 자원 미니 지도, Workflow 그래프, 근거, 조치·검증 결과).
- 승인 방법: 주요 화면 4개(지도, 사건 작업, 검토 기록, 설정)를 1440px·1280px 너비로 캡처해 사용자 검수. 검수 체크리스트(5.8 기준)와 대비율 WCAG AA 자동 검사 통과.

### 5.9 240쌍 평가 실행

- 방식 4종은 Codex 모델로 확정 후 고정(결정 D3). 현재 초안: Single-small(Luna/low), Single-High(Sol/xhigh), Fixed(복잡 Group), Adaptive. P0에서 구독 계정이 실제로 쓸 수 있는 모델 이름을 확인해 대응표를 만든다.
- 실제 실행기: LiveProbe 엔진을 방식별 설정으로 호출(`assessment` 프로필), 공개 입력만 전달. 실제 장애 실행기: `chaos/inject.sh`·`chaos/recover.sh`·기준선 확인.
- USD 비용 상한 대체(결정 D4): 구독 CLI는 실제 USD를 주지 않는다. 하네스의 필수 USD 상한을 "토큰·호출 수 기록 + 선택적 실행 쌍 수 제한"으로 바꾸고, 비용 지표는 토큰 × 기준 단가표로 계산한 `estimated`로 표기한다.
- 실행 순서: 사건 순, 방식 순, 직렬. 쌍마다 기준선 확인 → 주입 → 실행 → 항상 원복 → 기준선 회복 확인 → 기록.
- 중단 조건: 기준선 회복 실패(격리, 다음 쌍 금지), 정답 유출 감지, 두 계정 모두 한도(대기 후 재개, 진행 중 쌍은 실패 기록 후 명시적 재실행).
- 시간 예측은 시범 실행(4사건 × 4방식 = 16쌍)에서 실측 후 확정한다. 추측값을 일정으로 쓰지 않는다.

### 5.10 결과보고서와 참고자료

- 결과보고서(과제계획서 "완료 시 확인 가능한 결과물" 형식): 60개 사건별 선택 이유·사용 모델·비용(추정)·시간·분석 근거·복구/이관 결과, 4방식 비교표, 지표별 목표 달성 여부(`kan59-metrics/v1`), 시스템 구조도, 실행 화면, 원본 기록 위치. 기존 `evaluation/bundle_guard.py` 무결성 검사 통과.
- 참고자료(판단 가이드): 장애 유형·신호 조합별 항목. 각 항목은 관측 신호, 확인해야 할 근거, 흔한 가짜 단서, 원인 후보 순위, 적합했던 Agents Group·모델 구성, 효과 있던 안전조치와 원복 결과, 실패 사례와 이유, 원본 실행 기록 인용. Markdown 원문(`docs/reference/`) + RAG 색인(출처 유형 `reference_guide`, 버전 해시 고정).
- 정답 유출 방지: 참고자료는 240쌍 종료 후 생성. 새 사례 정답·식별자는 참고자료 생성 입력에서 제외하고, 생성 후 새 사례 정답 문자열 검사(기존 `assert_no_hidden` 방식)를 통과해야 색인한다.

### 5.11 새 사례 검증

- 새 사례 12개 제안(4 Family × 3, 원인·대상·가짜 단서를 기존 60개와 다르게). 사용자가 시연용 사례를 추가하면 별도 묶음으로 관리.
- 비교: Adaptive 방식, 같은 새 사례에서 참고자료 미사용 vs 사용. 지표는 원인 top-3 적중률, Workflow 일치율, 추정 비용, 시간, 근거 일치율.
- 결과는 "새 사례 n개에서의 관찰"로 보고하고 일반화 주장은 하지 않는다.

## 6. 단계별 작업 (실행 순서와 의존성)

각 단계는 이전 단계의 완료 기준이 확인된 뒤 시작한다. 장애 주입·실제 Codex 호출이 들어가는 단계는 시작 전에 사용자 승인을 받는다(표시: [승인 필요]).

### P0. 실행 가능성 확인 [승인 필요: Codex 1회성 확인 호출]
- TASK: 호스트 자원, kind + Cilium + Hubble 동작, 127.0.0.1 바인딩, Codex 두 계정 로그인 상태(값 비노출), 구독 모델 목록, 한도 오류 형식 확인 방법 조사.
- DELIVERABLE: `docs/plans/revamp/P0-feasibility.md` (실제 명령 출력 요약, 미확인 항목 명시)
- SCOPE: 읽기·임시 클러스터 생성/삭제만. 기존 rancher-desktop 클러스터 변경 없음.
- VERIFY: 임시 kind 클러스터에서 `hubble observe`로 흐름 확인, 메모리 사용량 측정, `codex` 모델 카탈로그 출력에 사용할 모델 존재 확인.
- STOP WHEN: 전용 클러스터 구성안과 모델 대응표가 실측으로 확정되거나, 불가 항목이 대안과 함께 기록됨.
- depends_on: 없음

### P1. 전용 PoC 클러스터
- TASK: `scripts/poc-cluster/{create,destroy,recover,status}.sh`, kind 설정(제어 1 + 워커 2, 127.0.0.1 포트 매핑), Cilium + Hubble Relay + exporter, `deploy/overlays/poc-kind`, 클러스터 신원 표식, 호스트 독립 실행 기록 디렉터리.
- DELIVERABLE: 스크립트, overlay, 문서.
- VERIFY: create → 전체 스택 Ready → destroy → create 반복 2회 성공, `scripts/validate-manifests.sh`에 overlay 추가 후 통과, 0.0.0.0 노출 없음 확인.
- STOP WHEN: 한 명령으로 재생성 가능하고 기존 시험 서비스 기준선(`chaos/recover.sh` 기준선 확인)이 통과.
- depends_on: P0

### P2. 인증 제거와 로컬 보호
- TASK: 5.1 적용(API·대시보드·설치 스크립트·README).
- VERIFY: `cd liveprobe && uv run pytest` 통과, 인증 관련 테스트를 "Host·Origin·헤더 검사" 테스트로 교체, 잘못된 Host·Origin 요청 거부 테스트, 웹훅 인증 유지 테스트.
- STOP WHEN: 로그인 없이 전 화면 사용 가능, 보호 테스트 통과.
- depends_on: P1

### P3. 흐름 수집과 지도 API
- TASK: Hubble exporter → Alloy → Loki 수집, Hubble 지표 → Prometheus, 흐름 집계기, `/api/topology`(자원·구성 관계·관측 흐름·신선도), SSE 지도 갱신.
- VERIFY: 단위 테스트(집계·신선도·미관측 처리), PoC 클러스터에서 order→inventory 흐름, 의도적 NetworkPolicy 차단 시 DROPPED 선, 외부 연결 표시 확인. Loki 라벨 카디널리티 제한 확인.
- STOP WHEN: 실제 흐름이 API로 나오고 Hubble 중단 시 지연 표시가 뜸.
- depends_on: P1

### P4. 알림·경고 연결
- TASK: 사건 영향 자원 키, SSE 이벤트, 브라우저 알림, drop 급증 규칙.
- VERIFY: F1·F4 장애 1회씩 주입해 배지·알림·사건 이동 확인 [승인 필요: 장애 주입].
- depends_on: P2, P3

### P5. 실행 이벤트·대화형 피드·개입
- TASK: `run_event/v1` 저장·재생, 체크포인터, 운영자 메시지·일시정지·중단.
- VERIFY: 가짜 LLM으로 메시지 반영 시점·일시정지·중단·재시작 재개 테스트, 조치 중 일시정지 시 조치 완료 후 정지 테스트.
- depends_on: P2

### P6. Codex 계정 풀
- TASK: 5.5 적용, 계정별 Secret·볼륨, 오류 분류, 대기 상태, 피드 표시.
- VERIFY: 가짜 codex 실행 파일로 quota→전환→둘 다 quota→대기→재개 테스트, 비밀값 비노출 테스트(기존 `test_codex_provider.py` 방식 확장). 실제 확인 호출은 [승인 필요].
- depends_on: P0, P5

### P7. 정책 프로필과 고위험 조치
- TASK: 5.6·5.7 적용, 호스트 실행기, 검토 기록.
- VERIFY: 조치별 거부·실행·원복·동시성 충돌 테스트, 다른 클러스터 신원에서 `poc_autonomy` 비활성 테스트, PoC 클러스터에서 조치별 1회 실행·원복 [승인 필요].
- depends_on: P1, P5

### P8. 대시보드 재구성
- TASK: 5.8 디자인 시스템, 지도·사건 목록/작업·검토 기록·설정 화면. 평가 결과 화면은 P10–P12 결과와 함께 구현하며 여기서 완료로 주장하지 않는다.
- VERIFY: `cd dashboard && npm test && npm run build && npm run test:e2e`, Playwright 실배포 화면 테스트, 대비율 검사, 캡처 4화면 × 2너비.
- STOP WHEN: 사용자 추천안 위임에 따라 별도 디자인 재승인 대신 위 화면 검증 완료. 기록: `docs/plans/revamp/P8-service-map-verification.md`.
- depends_on: P3, P4, P5 (P6·P7 화면 요소는 병행 연결)

### P9. 통합 검증
- TASK: 설치부터 장애 감지 → 알림 → 피드 → 조치 → 검증까지 F1~F4 각 1회, 고위험 조치 1회, 계정 전환 모의 1회.
- VERIFY: `scripts/e2e-mvp0.sh` 확장판, 기록 위치 문서화 [승인 필요: 장애 주입·실제 Codex].
- STOP WHEN: 모든 시나리오 기록이 남고 기준선 회복 확인.
- depends_on: P2~P8

### P10. 평가 준비
- TASK: 방식 4종 Codex 모델 확정·고정(D3), USD 상한 대체(D4), 실제 실행기·장애 실행기, 방식 설정 해시 고정, 시범 16쌍 [승인 필요].
- VERIFY: 하네스 테스트 갱신 통과, 시범 16쌍 모두 기준선 회복, 쌍당 시간·토큰 실측.
- STOP WHEN: 시범 결과로 240쌍 예상 시간·계정 한도 소진 예상을 사용자에게 보고하고 시작 승인.
- depends_on: P9

### P11. 240쌍 실행 [승인 필요]
- TASK: 직렬 실행, 15분 간격 진행 확인(사용자 요청), 중단·재개 규칙 준수.
- VERIFY: 240쌍 각각 정식 완료 기록 1건 + 기준선 회복 증거, 가짜·dry 기록 0건.
- depends_on: P10

### P12. 결과보고서와 참고자료
- TASK: 5.10 적용.
- VERIFY: `bundle_guard` 통과, 참고자료 각 항목이 원본 실행 기록을 인용, 정답 유출 검사 통과, RAG 색인 버전 고정.
- depends_on: P11

### P13. 새 사례 검증과 시연 준비 [승인 필요]
- TASK: 새 사례 12개 작성(정답 분리 보관), 참고자료 미사용/사용 비교, 시연용 사례 묶음.
- VERIFY: 새 사례 정답이 참고자료·입력에 없음 검사, 비교 결과표.
- depends_on: P12

## 7. 위험과 대응

| 위험 | 영향 | 대응 |
| --- | --- | --- |
| 16 GiB 메모리에서 kind 3노드 + 전체 스택 부족 | 전용 클러스터 불가 | P0 실측, 워커 1개 축소안, Loki·Prometheus 보존 기간 축소 |
| kind + Cilium이 arm64/Rancher Desktop moby에서 불안정 | 흐름 수집 불가 | P0 확인, 실패 시 Rancher Desktop 내 별도 VM 또는 colima 대안 검토 후 사용자 결정 |
| Codex 한도 오류 형식을 모름 | 전환 실패·무한 재시도 | P0·P6에서 실측 기반 분류, 미분류 오류는 재시도 1회 후 대기 |
| 구독에서 Luna/Terra/Sol 이름 모델이 없음 | 4방식 정의 불가 | 동급 모델 대응표(과제계획서 "동급 모델로 교체" 허용), D3에서 확정 |
| 240쌍 소요 시간이 매우 김(수십 시간) | 일정·계정 한도 | 시범 실측 후 재계획, 진행 기록으로 재개 |
| 인증 제거로 로컬 브라우저 공격 경로 | 무단 조치 승인 | 5.1 Host·Origin·헤더 검사, 127.0.0.1 바인딩 |
| 고위험 조치가 PoC 밖 클러스터에서 실행 | 실제 피해 | 클러스터 신원 표식 일치 시에만 활성화, 컨텍스트 하드코딩 금지 |
| 관측 스택 재시작 중 관제 끊김 | 기록 손실 | 호스트 독립 실행 기록, 재연결 후 SSE 재생 |
| 참고자료에 평가 정답 유출 | 새 사례 검증 무효 | 생성 시점 분리, 유출 검사 통과 후 색인 |
| 디자인 주관성 | 반복 수정 | 캡처 검수 + 체크리스트, 사용자 승인으로 종료 |

## 8. 사용자 확인이 필요한 결정 (권장안 포함)

| ID | 결정 | 권장안 | 이유 |
| --- | --- | --- | --- |
| D1 | 240쌍 평가에 고위험 실행 정책을 적용할지 | `assessment` 프로필(과제계획서 원칙) 유지, 고위험 실행은 PoC 시연·새 사례에만 | 승인된 정답표와 과제계획서 지표를 그대로 사용, 비교 가능성 유지 |
| D2 | 클러스터 형태 | **변경(사용자 지시 2026-10-01)**: 기존 rancher-desktop 클러스터에서 진행, 흐름 수집은 Retina(Hubble 제어 평면). kind+Cilium 시도는 폐기 | 사용자 지시. 단일 노드라 Node 재시작·drain은 제외 |
| D3 | 4방식 모델 | P0 모델 목록 확인 후 대응표 제시, 사용자 승인 | 실제 사용 가능 모델로 고정 필요 |
| D4 | 비용 상한 | USD 상한 제거, 토큰 기록 + 추정 비용 표기 | 구독 CLI는 실제 USD 없음, 기존 평가 계획의 "상한 없음"과 일치 |
| D5 | 운영자 메시지 반영 시점 | 다음 노드 경계, 일시정지는 조치 직전 포함 | Codex 1회성 실행 특성 |
| D6 | 외부 알림 | 화면·브라우저 알림만 | 로컬 PoC 범위 |
| D7 | 새 사례 수 | 12개(Family별 3) + 시연 사례 별도 | 비교에 필요한 최소 규모 |
| D8 | 기존 rancher-desktop 클러스터 | 유지, PoC 작업은 전용 클러스터에서만 | 공유 환경 보호 |

## 9. 가정 (확인 전까지 이 값으로 진행)

- A1 시스템 네임스페이스 흐름도 관측하되 조치 대상은 5.7 표의 허용 목록뿐이다.
- A2 Pod 수준 흐름은 확대 보기에서만 표시한다(기본 화면은 Workload 수준).
- A3 외부 메신저 알림은 하지 않는다.
- A4 대시보드 언어는 한국어, 자원 이름은 원문 그대로.
- A5 Grafana는 보조 도구로 유지한다.
- A6 실제 AI 호출 일시 중지(2026-09-29 지시)는 P0 이후 단계별 승인으로 해제한다.

## 10. 완료 판정

- 1장 목표 1~10이 각 단계 VERIFY의 실제 기록(명령 출력, 화면 캡처, 실행 기록 파일 경로)으로 확인될 때만 완료로 보고한다.
- 가짜 실행기·합성 데이터·추정치는 완료 증거로 쓰지 않는다. 추정 비용은 `estimated`로 명시한다.
- 미완료 항목은 이유와 다음 행동을 함께 보고한다.
