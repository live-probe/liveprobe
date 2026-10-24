# P18 실행 계획 — 환경(dev·stg·prd) × 시스템 확장과 코드 수정 승격

근거: `docs/plans/revamp/P18-extensibility-interview.md`의 정리된 요구(목표·제약·성공 기준)를 따른다.
진행 방식: 사용자가 결정을 위임했다. 권장안으로 진행하고 각 결정은 `.hermes/plans/decision-log.md`에 남긴다. 다음 세 가지만 사용자에게 묻는다: Secret 값, 외부 공개, 되돌릴 수 없는 삭제.

## 핵심 설계 결정
| ID | 결정 | 이유 |
|---|---|---|
| A1 | 환경은 네임스페이스 `dev`·`stg`·`prd`이며, 라벨 `liveprobe.io/environment=<env>`와 `liveprobe.io/env-order=1/2/3`을 단다. | 사용자 지정 이름이다. 순서는 라벨로 고정한다. |
| A2 | 시스템은 워크로드 라벨 `app.kubernetes.io/part-of=<system>`으로 구분하고, 서비스는 `app=<service>`다. order·inventory는 `part-of=order`로 붙인다. | 기존 `part-of=liveprobe-target` 태그를 이름만 바꿔 쓴다. 시스템 목록은 코드가 아닌 라벨로 발견한다. |
| A3 | 새 시스템 5개: payment(Python), member(Node), catalog(Python), delivery(Node), notification(Python). 각각 실제 업무 화면, 장애 버튼, `/metrics`를 갖춘다. 외부 의존 패키지 없이 표준 라이브러리만 쓴다. | 스프링부트 외 언어도 대응할 수 있음을 보이기 위해서다. 노드 메모리를 절약하고 빌드에 네트워크가 필요 없다. |
| A4 | 모든 대상 앱은 같은 HTTP 지표 이름(`http_server_requests_seconds_count{method,uri,status}`·`_sum`)을 노출한다. 예외는 표준 오류 형식(자바 `at …(File.java:N)`, 파이썬 `File "…", line N`, 노드 `at … (file.js:N:C)`)으로 로그에 남긴다. | 감지 규칙과 코드 위치 추출을 언어와 무관하게 하나로 쓴다. |
| A5 | 이스티오 사이드카는 prd에만 둔다. dev·stg는 메시 없이 운영하고, 관측 흐름은 Retina/Hubble로 본다. | 단일 노드 메모리(할당 가능 11.7GiB) 한도 때문이다. |
| A6 | `liveprobe-target`은 삭제하지 않는다. prd 검증이 끝나면 Deployment를 0으로 줄이고(되돌릴 수 있음), 운영 관제 환경 선택지에서 뺀다. loadgen도 0으로 줄인다. | 사용자 지시("나머지는 띄우지 마")를 따르면서 기존 시험 기록은 보존한다. |
| A7 | 환경별 inventory DB는 같은 postgres에 `inventory_dev/stg/prd`(전용 역할, 암호는 Secret로만 생성·비출력)로 새로 만든다. 기존 `inventory` DB 354행은 건드리지 않는다. | DB 시험 원칙이다. |
| A8 | 근본 수정 흐름은 아래 B 계약으로 구현한다. LLM 패치는 LiveProbe API 안의 Codex(기존 구독 인증·모델 설정)가 만든다. 소스 저장·적용·시험·이미지 빌드는 개발 환경 러너(호스트 프로세스, 바깥 방향 폴링만 함)가 맡는다. 배포 승격은 LiveProbe가 운영자 확인 뒤 `set image`로 한다. | 사용자가 지적한 "운영망과 분리된 개발 환경"을 반영한다. LiveProbe는 개발 환경으로 들어가지 않는다. 러너가 일을 받아 가는 방식은 self-hosted CI 러너와 같다. |
| A9 | 수정 대상은 `live-probe` organization의 비공개 GitHub 저장소 6개다(`order`는 order·inventory 두 서비스, 나머지는 시스템별 단일 저장소). 러너는 저장소를 clone하여 수정 브랜치를 push하고 실제 PR을 만든다. 사용자가 GitHub에서 검토·병합하며 AI는 자동 병합하지 않는다. LiveProbe 자체는 비밀정보·로컬 자료를 제외한 별도 초기 이력으로 `live-probe/liveprobe`에 공개한다. | 사용자 추가 지시로 로컬 전용 저장소·외부 push 금지 가정을 대체했다. 기존 로컬 작업 이력은 보존하고 인증정보를 공개 이력에 섞지 않는다. |
| A10 | 단계 검증 = rollout 준비 + 헬스 + 장애를 일으킨 요청 재현 성공 + 60초 동안 해당 앱 새 예외 로그 0건. 실패하면 그 단계만 이전 이미지로 자동 원복한다(쿠버네티스 범위 원복은 기존에 허용됨). | 쿠버네티스 한정 자동 원복 원칙이다. |
| A11 | 앱 예외 감지: 기존 Prometheus 5xx·Pod 신호에 더해 LiveProbe가 Loki를 15초 간격으로 조회하는 앱 예외 감시기를 둔다(`ApplicationException`). | HTTP 지표가 없는 백그라운드 작업 오류도 언어와 무관하게 감지한다. |
| A12 | 장애 관제실은 기본 01 분석 흐름 + 02 통합 분석·운영자 채팅으로 재편한다. 노드 선택은 같은 실행 카드로 부드럽게 이동하고, 상세보기 때만 기존03폭 패널을 연다. 단계별 요약·한글 결론·수집 항목 목록을 대화의 기본 내용으로 둔다. | 사용자 추가 인터뷰 요구다. 빈 단계 상세와 역할별 동일 근거 반복을 없애되 승인·지시·PR 안전 계약을 유지한다. |

## B. 코드 수정 승격 계약 (변경 요청, ChangeRequest)
- 상태: `awaiting_source → patching → awaiting_build → building → built | tests_failed | patch_failed → dev_deploying → dev_verified | dev_failed → stg_deploying → stg_verified | stg_failed → prd_deploying → prd_verified(완료) | prd_failed`. 운영자 취소는 `cancelled`.
- 생성: 분석 결론 `remediation.kind ∈ {code_fix, both}`이고 시스템 소스가 러너에 등록돼 있으면 자동 생성한다(배포가 아니므로 승인 불필요). 운영자가 관제실에서 직접 만들 수도 있다.
- 러너 API(Bearer 토큰 = Secret `liveprobe-system/liveprobe-runner-token`, 값 비출력):
  - `POST /api/runner/claim` → `{job}`. 종류: `fetch_source{cr_id, system, service, base_image, paths[]}` / `build{cr_id, system, service, diff, base_commit}`. 일이 없으면 204.
  - `POST /api/runner/jobs/{job_id}/result` ← fetch_source는 `{base_commit, files:{path:text}}`(파일당 64KB, 전체 400KB 상한), build는 `{ok, commit, branch, image, image_id, repository, pr_url, pr_number, head_sha, tests:{total,failed,report_tail}, log_tail}`.
  - 러너 설정에 `github_repo`, `source_subdir`, `base_branch`를 둔다. GitHub 인증은 호스트의 `gh` 인증만 사용한다. 검증 성공 후 수정 브랜치 push·실제 PR 생성을 하며, 자동 병합하지 않는다. UI에서 실제 PR 링크·상태·빌드 head SHA를 보여준다.
  - prd 승격은 사용자 GitHub 병합이 관측되고 빌드 SHA와 대응하는 경우에만 허용한다. GitHub 조회 실패나 미병합·SHA 불일치는 닫힌 채로 거부한다.
- 패치 생성: 역할 `code_fixer`(모델 설정에 추가, 기본 gpt-6.1-sol·high)가 진단(예외 원문, 코드 위치, 재현 요청)과 받은 파일로 unified diff를 만든다. 회귀 시험 추가를 요구한다. diff는 받은 파일만 수정할 수 있다(경로 검증).
- 승격 API:
  - `POST /api/changes/{id}/promote {stage, plan_sha256, confirm:true}`
  - `POST /api/changes/{id}/cancel`
  - `GET /api/changes?incident_id=`, `GET /api/changes/{id}`
  - 승격 전 서버가 부작용 문구(대상 환경·Deployment·이전→새 이미지·재시작 영향·원복 방법)를 계산해 돌려준다. 화면은 "정말 진행하시겠습니까?"를 확인한 뒤 호출한다.
  - 순서는 dev → stg → prd로 강제한다. 앞 단계가 verified가 아니면 409를 반환한다.
- 감사: `change_created/patch_generated/build_reported/promotion_confirmed/stage_verified/stage_failed/stage_reverted`.

## C. 백엔드 일반화 계약 (overview·topology·incident)
- 설정: `AppConfig.target_namespaces: ["dev","stg","prd"]`(순서 = 환경 순서). 기존 `target_namespace`는 호환용으로 읽기만 한다.
- Incident에 `system`(nullable, 생성 시 워크로드 part-of 라벨)을 추가한다. Alembic `0009`. API 응답에 `system`, `environment`(= namespace가 대상 환경이면 그 이름)를 포함한다.
- `/api/overview`
  - `cluster.environments`: 각 항목 `{name, order, workloads, pods_ready, pods_total, open_incidents}`, 순서 dev·stg·prd.
  - `workloads[]`에 `system`, `environment`를 추가한다.
  - `topology.nodes[]`에 `system`, `environment`를 추가한다.
  - `topology.groups`: 대상 환경 안에서는 `{id:"<env>/system/<system>", kind:"system", environment, system, label}`이고 플랫폼 네임스페이스는 기존대로 둔다.
- 안전 조치: 허용 Deployment는 대상 환경 네임스페이스 안 `liveprobe.io/managed=true` 라벨을 가진 것으로 일반화한다. 소유 라벨은 `part-of`가 등록 시스템일 것. VAP·RBAC·롤백 컨트롤러 역할을 3개 네임스페이스로 확장한다.
- 분석 출력: 결론에 `remediation:{kind: mitigation|code_fix|both|none, why, code_refs:[{file,line,symbol}], repro:{method,path,query}|null}`를 추가한다. 근거에 `E8_code_refs`(스택 프레임 결정적 추출)를 추가한다.

## D. 화면 계약
- 운영 관제: 환경 선택(dev → stg → prd, 기본 prd, "전체 환경" 포함). 환경 안에서 시스템 상자별로 서비스를 그린다. 플랫폼 구성요소는 기존 토글을 유지한다.
- 장애 목록·관제실: "prd · order · inventory"처럼 환경·시스템·서비스를 표시한다.
- 장애 관제실의 기본은 01 AI 분석 흐름 + 02 분석·운영자 통합 채팅이다. 기존 상시 근거 열·독립03 대화 열을 유지하는 가정은 폐기한다. 노드 클릭은 해당 실행 카드로 부드럽게 스크롤·강조하며 상세보기 클릭 때만 기존03폭의 오른쪽 패널을 열고 닫는다. 메시지 입력·분석 제어는02에 고정한다.
- 감지·수집·판단·분석·검증마다 다른 내용 구조를 제공한다. 수집9건은 실제9개 항목의 이름·출처·짧은 정보·상태를 보여야 하며, 분석과 결과 확인을 동일한 인용 목록만으로 대체하지 않는다. 재시도·모델 승격은 실행 ID로 정확히 연결한다.
- 통합 채팅에 변경 요청 카드를 둔다: 상태 단계 표시, PR 링크·요약, 시험 결과, 이미지, 단계별 승격 버튼(부작용 + "정말 진행하시겠습니까?"), 단계 결과·원복 기록. 긴 diff·입출력 원문·기술 메타데이터는 상세 패널에서 열람한다.
- 추가 계약과 U1~U8 검증 기준: `docs/plans/revamp/P18-incident-conversation-brief.md`. 이 항목은 계획이며 아직 새 화면 구현·배포를 의미하지 않는다.
- 새 시스템 화면 주소: `http://<system>.<env>.localhost/`(traefik Ingress). order 시스템 화면은 order 서비스 정적 페이지로 추가한다.

## 작업 분할
| 작업 | 소유 파일 | 담당 |
|---|---|---|
| W1 대상 시스템 5종 + order 화면 | `services/{payment,member,catalog,delivery,notification}/**`, `services/order/src/main/resources/static/**`, `deploy/environments/systems/**` | 위임 A |
| W2 백엔드 일반화(C 계약) | `liveprobe/app/*` 중 config·watcher·incidents·evidence·router·workflows·agent_groups·k8s·actions·safety·network_signals·overview·flows·narrative·flowgraph, `alembic/versions/0009_*`, 테스트 | 위임 B |
| W3 변경 요청·러너(B 계약) | `liveprobe/app/changes.py`·`change_api.py`·`change_models.py`, `alembic/versions/0010_*`, `scripts/liveprobe-runner.py`, `dashboard/src/{ChangeRequestCard.tsx, changes.ts}`, 테스트 | 위임 C |
| W4 화면 일반화(D 계약) | `dashboard/src/{TopologyMap,mapModel,Overview,overviewModel,Incidents,labels}.*`, `dashboard/e2e/fixtures/**`, `mock-api.mjs`(overview 부분) | 위임 D |
| W5 환경 기반·통합·배포·실연 | `deploy/environments/base/**`, 네트워크·RBAC·VAP·Prometheus 규칙, DB, main.py 연결, 빌드·배포, 실연·증거 | 부모 |
| W6 통합 채팅형 장애 관제실(추가 요구) | `IncidentRoom.tsx`·흐름/채팅 이동·단계별 카드·상세 패널·스타일, `flowgraph.py`·`narrative`의 단계별 데이터 대응, 단위·모의·실화면 U1~U8 | 계획 편입·구현 미착수. W2/W3/W4와 데이터/카드 접점을 합의한 뒤 별도 수행 |

## 완료 판정 (인터뷰 성공 기준과 동일)
1. 운영 관제 환경 선택지가 dev·stg·prd이고, 각 환경에 시스템 6개가 시스템 이름으로 묶인다. loadgen 등 다른 대상은 보이지 않는다.
2. 5개 시스템 화면에 접속할 수 있고, 화면 버튼으로 낸 장애가 해당 환경·시스템 위치의 LiveProbe 장애로 표시된다.
3. 실연에서 다음이 이어서 관측된다.
   - prd order 코드 버그를 감지한다.
   - 분석이 "코드 수정 필요"와 근거(스택·소스 위치)를 낸다.
   - prd를 롤백해 완화한다.
   - 변경 요청을 만들고 diff·시험을 거친다.
   - dev → stg → prd를 각각 승인·검증하며, 승인마다 부작용 확인을 거친다.
   - 실패한 단계는 원복한다.
4. 기존 회귀(pytest·화면 단위·모의/실배포 브라우저)가 통과한다.
5. 추가 통합 관제실 U1~U8을 통과한다. 01→02 정확한 실행 카드 이동, 9개 수집 항목 요약, 역할 고유 결론/검증 결과, 필요 시 상세 열기·닫기와 초안 보존, 02 메시지 입력, 모바일/키보드/움직임 감소 및 승인 안전 회귀를 실화면으로 확인한다.
