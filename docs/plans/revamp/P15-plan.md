# P15 구현 계획서 — 운영 관제·장애 관제 분리와 사람 개입 실행

근거: `docs/plans/revamp/P15-ux-interview.md`(2026-10-18 인터뷰 합의). 작업 추적은 이 문서와 `P15-execution.md`로 한다(Jira 미사용).
사용자 위임: 계획 확정 후 허가 요청 없이 끝까지 진행한다. 예외는 비밀값 노출, 외부 공개, 되돌릴 수 없는 삭제다.

## 0. 현재 상태 (2026-10-18 확인)

- HEAD `06e0046`. 운영 이미지: API `917366e7…`, dashboard `454183d9…`. Alembic 0007.
- 화면: `/`=Kiali iframe(`Mesh.tsx`), `/map`=`ClusterMap.tsx`, `/incidents/:id`=대화 피드+우측 패널(`Incidents.tsx`).
- 실행 경로:
  - 무인 조치는 `safety.execute`/`actions.py` 6종이며, 실행 계정 `safe-action-executor`는 target 2개 Deployment로 한정된다.
  - 운영자 메시지는 노드 경계에서 다음 Agent 프롬프트에 붙는다(`runlog.take_messages`).
  - 종료 상태에서는 메시지가 409로 거부된다.
- 관측:
  - 주 Prometheus는 cAdvisor `liveprobe-target`만, KSM은 2개 namespace만, Pod 지표는 order|inventory만 수집한다. Alloy 로그도 order|inventory만 수집한다.
  - mesh-prometheus는 Kiali 전용(istiod·Envoy)이다.
- 사건 #990 화면: Agent 출력이 영어 원문뿐이고, 역할 ID가 그대로 노출되며, 라우터는 "router 완료"로만 표시된다.

## 1. 설계 결정

| 번호 | 결정 | 이유 |
| --- | --- | --- |
| D1 | 한글 해석은 기존 Agent 스키마·프롬프트를 바꾸지 않는다. 별도 `interpreter` 역할(gpt-6-luna/low)이 원문 묶음을 번역·요약해 `interpretations` 테이블에 캐시한다. 결과가 나오기 전에는 알려진 패턴(ImagePullBackOff, OOMKilled 등)의 규칙 기반 한글 문장을 보여준다. | 평가 재현성과 라우팅 판단에 영향을 주지 않기 위해서다. 해석은 화면 표시용으로만 쓴다. |
| D2 | 해석 호출은 `AgentRun(role="interpreter")`로 기록해 비용에 포함한다. 흐름도에서는 분석 경로와 분리된 보조 노드로 그린다. | 브리프 가정(비용 표시)을 따르면서 분석 경로를 오염시키지 않는다. |
| D3 | 새 LLM 역할(interpreter, steering, operator_planner)은 `config.assist` 별도 섹션에 둔다. | 운영 ConfigMap이 `llm.roles`를 명시하므로, 기존 역할 검증·모델 설정 화면과 분리해야 한다. |
| D4 | 흐름도는 서버 `flowgraph.py`가 실행 사실(run_events, routing, agent_runs, actions, commands)과 예정 경로(그룹 토폴로지)로 노드·간선 JSON을 만든다. 화면은 `@xyflow/react`로 그린다. | 화면 로직을 단순하게 하고, 같은 그래프를 시험에서 검증할 수 있다. |
| D5 | steering에서 중단·모델 승격·명시적 Group 변경은 규칙으로 바로 분류하고 나머지 조사 지시는 다음 Agent의 프롬프트로 전달한다. 적용 시점은 다음 노드 경계다. Group 변경은 `RerouteRequested`로 현재 그래프를 끝내고, 같은 근거로 새 Group을 실행한다(decided_by="operator"). 클러스터 변경 지시는 무인 Agent에게 전달하지 않고 별도 확인 기반 command 경로로 보낸다. | 제어를 LLM 응답 대기 없이 일관되게 적용하고, 운영자 지시가 자동 조치 경로를 우회하지 못하게 한다. |
| D6 | 운영자 지시 실행은 별도 계정 `liveprobe-operator-executor`가 맡는다. 진행 순서는 계획(operator_planner, gpt-6.1-sol/medium) → 서버 검증 → 서버 측 dry-run(`dryRun=All`) → 부작용 설명 → 확인(계획 해시 일치) → 실행 → 검증 → 원복 수단 기록이다. | 무인 조치 계정·읽기 전용 API 계정과 권한을 분리한다. |
| D7 | 실행 계정 RBAC는 `kubectl api-resources` 기준으로 Secret을 제외한 모든 자원을 생성해 커밋한다. 제외 대상은 pods/exec·attach·portforward, serviceaccounts/token, nodes/proxy, services/proxy, admission 정책·웹훅, `escalate`/`bind` 동사다. VAP로 자기 RBAC 객체와 LiveProbe 가드 객체 변경도 거부한다. | Secret 우회 경로(exec로 마운트 읽기, 토큰 발급, 권한 상승)를 막는다. |
| D8 | Secret 변경은 계획에 key 이름만 담는다. 값은 마스킹 입력란 → 전용 엔드포인트(본문 직접 파싱, 검증 오류에 입력값을 되돌려 주지 않음) → `liveprobe-secret-writer` 계정 순서로 전달된다. 이 계정은 patch 권한만 있고 get/list/watch는 없다. 응답 본문은 읽지 않는다(`_preload_content=False`). 원복은 불가능하다고 부작용 설명에 명시한다. | AI·기록·DB·로그 어디에도 값이 남지 않게 한다. |
| D9 | 첫 화면 `/`=운영 관제(`Overview.tsx`), `/incidents`=장애 관제 목록, `/incidents/:id`=장애 관제실(`IncidentRoom.tsx`)이다. `/map`은 `/`로 넘긴다. `Mesh.tsx`·`ClusterMap.tsx`는 제거한다. 시각 기준은 상용 관측 콘솔(Datadog·Grafana Cloud·Sentry류의 밝은 고밀도 콘솔)이다. | 인터뷰 Q3·Q4. |
| D10 | 운영 관제 API `GET /api/overview`는 Prometheus 원천 질의, Kubernetes 이벤트(Warning, 전체 namespace, 원문), 토폴로지, 사건 진행 단계를 묶는다. 값을 가공해 점수로 만들지 않는다. | 원천 데이터를 최대한 그대로 쓰라는 정정을 반영한다. |
| D11 | 관측 범위를 확장한다. cAdvisor는 전체 namespace(Secret 무관), KSM `--namespaces` 제거, Pod 지표·Alloy 로그는 전체 namespace로 넓힌다. Envoy `istio_requests_total` 등 3개 지표를 주 Prometheus로 옮긴다. mesh-prometheus·kiali는 백업 후 제거하고 istiod·sidecar는 유지한다. 보존 2d/1500MB, Loki 48h 한도는 유지하고 sample_limit은 조정한다. | 인터뷰 Q3 정정과 가정을 따른다. |
| D12 | 10건 재현은 운영 경로(watcher/Alertmanager → engine)로 한다. evaluation lock을 걸지 않는다. 주입·원복은 `evaluation/harness/real.py`의 K8sFaultWorker를 재사용하고, 브라우저 증거를 단계별로 남긴다. | 지도 표시부터 완결까지를 실제 제품 경로로 증명한다. |

## 2. API 계약 (프런트·백엔드 공통)

`GET /api/incidents/{id}/narrative` 응답:
```
{ "incident": {...기존 summary 필드},
  "headline": {"what": "한글 한 문장", "impact": "한글", "stage": {"key": "routing|analyzing|verifying|acting|resolved|handed_off|failed", "label": "한글"},
               "next": "한글(운영자가 할 일 또는 AI 다음 단계)", "source": "rules|llm"},
  "raw": [{"id": "E5_recent_events#3", "ref": "E5_recent_events", "kind": "event|log|status|metric|config|node|agent",
           "source": "kubernetes|loki|prometheus|agent:<role>", "at": "iso|null", "raw": "원문 그대로", "severity": "info|warn|error",
           "ko": "한글 해석|null", "ko_source": "rules|llm|null", "cited_by": ["k8s_analyst", ...]}],
  "flow": {"nodes": [{"id", "type": "signal|evidence|router|group|agent|verify|action|outcome|steer|command|interpret",
                      "label": "한글", "role": "역할ID|null", "role_ko": "한글 역할명", "job_ko": "하는 일 한 문장",
                      "status": "planned|running|done|failed|skipped|escalated", "model", "effort", "tokens": {"in","out"}, "cost_usd", "duration_ms",
                      "inputs": ["E1_deployment", ...], "conclusion_ko": "|null", "conclusion_raw": "|null", "lane": 0, "depth": 0, "group": "SIMPLE|..|null"}],
           "edges": [{"id", "source", "target", "kind": "flow|escalate|reroute|steer|verify", "label": "|null"}]},
  "interpreting": true|false }
```
`POST /api/incidents/{id}/messages`:
- 기존 계약을 유지한다.
- 응답에 `{"intent": "instruction|escalate|reroute|stop|cluster_change", "target_role": ..., "ack_ko": ...}`를 추가한다. 분류는 비동기로 하고, 결과는 run_event `steering_interpreted`로 남긴다.
- 종료 상태에서 보낸 메시지는 409 대신 운영자 지시(command) 생성으로 처리한다.

`POST /api/incidents/{id}/commands {instruction}`: 201, `{command}`(status `planning`). 계획이 끝나면 `awaiting_confirmation`이 된다.

`GET /api/incidents/{id}/commands`: 사건의 운영자 지시 목록을 돌려준다.

command 객체:
```
{ "id", "incident_id", "instruction", "status": "planning|awaiting_confirmation|awaiting_secret|executing|succeeded|failed|cancelled|rejected|rolling_back|rolled_back",
  "plan": {"summary_ko", "operations": [{"op": "apply|patch|delete|scale|restart|cordon|uncordon|secret_update", "apiVersion", "kind", "namespace", "name",
           "manifest|patch|replicas|keys"}]},
  "plan_sha256", "dry_run": [{"op_index", "ok", "diff": [{"path","before","after"}], "error"}],
  "side_effects": {"items_ko": ["..."], "targets": ["ns/Kind/name"], "restarts": bool, "downtime": "none|possible|expected",
                   "reversible": bool, "rollback_ko": "...", "cluster_wide": bool, "warnings_ko": ["..."]},
  "confirm_question": "정말 진행하시겠습니까?", "result": {...}, "verification": {...}, "rollback": {...}, "error": "|null", "created_at", "updated_at" }
```
- `POST /api/commands/{cid}/confirm {plan_sha256}`: 해시가 다르면 409. Secret이 포함된 계획이면 `awaiting_secret`으로 바뀐다.
- `POST /api/commands/{cid}/cancel`: 지시를 취소한다.
- `POST /api/commands/{cid}/rollback {plan_sha256}`: 원복을 실행한다.
- `POST /api/commands/{cid}/secret-values` (본문 `{"plan_sha256": "...", "values": {"KEY": "..."}}`, 2KB/값, 16키): 응답은 `{status, keys}`이며 값은 돌려주지 않는다.

`GET /api/overview` 응답:
```
{ "generated_at", "cluster": {"nodes": [{"name","ready","cpu_cores","cpu_used_cores","mem_bytes","mem_used_bytes","pods"}],
  "namespaces": [{"name","pods_running","pods_pending","pods_failed","restarts_1h","cpu_cores","mem_bytes","workloads","workloads_unhealthy"}]},
  "workloads": [{"id","namespace","name","kind","ready","desired","restarts_1h","cpu","mem","req_rate","err_rate","p95_ms","incident": {id,level,status,stage}|null}],
  "events": [{"at","namespace","object","reason","type","message"(원문),"count"}],
  "incidents": {"open": [...stage 포함], "today": {"total","resolved","handed_off","failed"}},
  "topology": 기존 /api/topology 형식 + edges의 req_rate/err_rate(Envoy),
  "sources": {"prometheus": "ok|error: ...", "kubernetes": "ok|...", "hubble": "connected|...", "envoy": "ok|..."} }
```

### 2026-10-21 계약 명확화

- `err_rate`는 워크로드와 연결선 모두 0~1 비율이다. 화면에서만 100을 곱해 %로 표시하며 null은 미관측으로 유지한다. fixture 생성기도 이 원천 단위를 따른다.
- 본문을 보내는 DELETE 예행연습은 URL뿐 아니라 `DeleteOptions.dryRun: ["All"]`을 본문에 포함한다. 확인 전 UID·resourceVersion·내용 불변을 실제 클러스터에서 확인해야 한다. DELETE 409를 이후 소멸로 성공 처리하지 않는다.

## 3. 작업 항목

각 항목은 "무엇을 / 어디서 / 시험" 순서로 적는다. 백엔드는 TDD(RED 출력 확인 후 구현)로 진행한다.

| ID | 작업 | 파일 | 시험·완료 기준 |
| --- | --- | --- | --- |
| W0 | 백업 | `.e2e-tmp/revamp-p15/`: API·dashboard Deployment, liveprobe-config, prometheus·KSM·alloy ConfigMap/Deployment, istio-system kiali·mesh-prometheus 전체 YAML, DB dump | 파일 존재, `pg_restore --list` 통과 |
| W1 | migration 0008: `interpretations`, `operator_commands` | `alembic/versions/0008_p15_interpret_commands.py`, `models.py` | 마이그레이션 상향·하향 시험 |
| W2 | 역할 한글 카탈로그(역할명·하는 일·입력) | `app/roles_ko.py` | 모든 GROUP_ROLES·workflow 역할에 항목이 있어야 함 |
| W3 | 원문 항목 추출과 규칙 해석 | `app/interpret.py` | 근거 E1~E9·agent 출력에서 원문을 그대로 보존(바이트 동일), 알려진 패턴 한글 해석, 비밀 마스킹 유지 |
| W4 | LLM 해석 작업기(배치·캐시·비용 기록, 실패해도 사건 진행에 영향 없음) | `interpret.py`, `engine.py` 훅 | 가짜 provider로 캐시·원문 해시 무효화·실패 격리 시험 |
| W5 | 흐름 그래프 생성 | `app/flowgraph.py` | SIMPLE/NORMAL/COMPLEX/HIGH_RISK·승격·재라우팅·steering·command 노드·간선 시험 |
| W6 | narrative API | `main.py` | API 시험(404, 진행 중·종료, 해석 대기) |
| W7 | steering 분류·적용(escalate·reroute·stop·instruction·cluster_change) | `app/steering.py`, `runlog.py`, `engine.py`, `agent_groups.py` | 노드 경계 반영, reroute 시 새 RoutingDecision(operator), 종료 후 메시지→command |
| W8 | 운영자 지시 계획·검증·dry-run·부작용 | `app/commands.py` | Secret 값·RBAC 상승·admission·자기 객체 거부, 해시 고정, dry-run diff, 부작용 규칙(재시작·중단·원복 불가·클러스터 영향) |
| W9 | 실행·검증·원복(실행 계정 분리) | `commands.py`, `k8s.py` | 가짜 동적 클라이언트로 생성/수정/삭제/scale/restart/cordon 실행·원복·실패 시험 |
| W10 | Secret 쓰기 경로 | `app/secret_writer.py`, `main.py` | sentinel 값이 DB 전 테이블·run_events·audit·LLM 프롬프트·로그·응답에 없음, 검증 오류 시에도 값이 되돌아오지 않음 |
| W11 | overview API | `app/overview.py`, `main.py` | Prometheus 질의 가짜 응답·오류 시 출처 표시, 이벤트 원문 보존 |
| W12 | 프런트 공통: 타입, 모델, 내비게이션(운영 관제/장애 관제/검토 기록/설정), 라우트 | `api.ts`, `App.tsx`, `Shell.tsx`, `narrative.ts`, `overviewModel.ts` | node 단위 시험 |
| W13 | 운영 관제 화면(후속 권장안: 큰 KPI 띠 제거, 지도 중심·우측 보조 영역·상단 알림, 사건 핀→장애 관제, 원문 이벤트) | `Overview.tsx`, `TopologyMap.tsx`, `styles.css` | 단위·e2e·1440/768/375 캡처 |
| W14 | 장애 관제실(후속 권장안: 세로 흐름·한글 우선 근거/단계 상세·상시 대화, 조치 승인/인계·지시 확인/Secret 입력) | `IncidentRoom.tsx`, `FlowGraph.tsx`, `CommandCard.tsx`, `styles.css` | 단위·e2e·캡처, 키보드 접근 |
| W15 | 배포 구성: 실행 계정·Secret writer RBAC(생성 스크립트), VAP, API 마운트, assist 설정, 관측 범위, Kiali·mesh-prometheus 제거 | `deploy/base/liveprobe/operator-executor.yaml`(생성), `scripts/gen-operator-rbac.py`, `liveprobe.yaml`, `observability/*`, `deploy/optional/istio/*` | manifest 시험(Secret 미포함, 제외 동사), `kubectl auth can-i` 허용·거부 표 |
| W16 | 빌드·배포·실측 | 이미지 p15a~, overlay digest | readyz, served bundle 일치, can-i, Prometheus target up |
| W17 | 사람 개입 자동 실측(sandbox namespace `p15-sandbox`: ConfigMap 생성→수정→원복, Deployment scale, sentinel Secret 쓰기 후 DB·로그 검색) | `scripts/p15-hitm-verify.py` | 단계별 JSON, 정리 확인 |
| W18 | 10건 운영 경로 재현 | `scripts/p15-live-verify.py` | 사례별 지도 핀·관제실 원문/해석·흐름도·자동 완결 캡처와 JSON, 기준선 복구 |
| W19 | 독립 검토, 문서(README, DESIGN.md, P15-execution, decision-log), 최종 재조회 | — | 검토 통과, 회귀 전체 통과. 커밋·push는 별도 요청 시 진행 |

## 4. 순서와 의존 관계

1. W0
2. 백엔드 W1→W2→W3→W4→W5→W6, 이어서 W7, W8→W9→W10, W11. 한 사람이 순차로 맡는다(engine/main 공유 파일).
3. 프런트 W12→W13·W14. 2절 계약을 기준으로 백엔드와 병렬로 진행한다. 프런트 파일만 수정하며 백엔드 파일은 건드리지 않는다.
4. W15(배포 파일)는 W8~W10 이후 진행한다.
5. 통합: 전체 API pytest, 평가·chaos 시험, 프런트 test/build/e2e를 실행한다.
6. W16 → W17 → W18. 실패하면 RED→수정→재배포 후 해당 사례만 다시 확인한다. 실패 기록은 보존한다.
7. W19.

## 5. 안전·운영 규칙

- 클러스터 변경 전 W0 백업을 확인하고, 필요한 객체만 apply한다(전체 overlay 금지).
- 시험 DB는 작업별 전용 DB를 쓴다(`lptest_p15_*`). fixture가 public schema를 지우기 때문이다.
- 사람 개입 실측은 전용 sandbox namespace에서만 하고, 끝나면 namespace를 삭제했는지 확인한다.
- 10건 재현은 한 번에 하나씩 진행하고, 시작 전 기준선(이미지·selector·엔드포인트·order 200·메모리 512Mi·replica 1)을 확인한다.
- 비밀값은 출력·요약·커밋하지 않는다.

## 6. 완료 판정

인터뷰 브리프의 성공 기준 1~5를 모두 관측해야 완료로 본다. 10건 중 인계로 끝난 건은 실패로 기록하고 원인을 조사하며, 다른 사례로 바꾸지 않는다.
