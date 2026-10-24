# LiveProbe K8S

Kubernetes 이상 신호를 사건으로 묶고, 난이도·위험도·근거 충분도에 따라 AI Agents Group Workflow와 역할별 모델을 선택하는 로컬 운영 PoC다. 근거를 독립 검증한 뒤 허용된 Kubernetes 조치만 실행하거나 운영자에게 이관한다. 현재 기준은 **정책 C**, **Codex CLI 구독 호출 전용**이다. 과거 MVP-0의 “F1/F4·롤백만” 범위와 현재 구현을 구분한다.

- [GitHub 저장소·코드 수정 PR 정책](docs/operations/github-repositories.md) · [저장소 매핑](repositories.json) · [공개·운영 안전 경계](SECURITY.md)
- LiveProbe 소스는 https://github.com/live-probe/liveprobe 에 공개하고, 테스트 시스템 6개는 `live-probe` organization의 비공개 저장소로 분리한다. `order` 저장소 안에 주문·재고 두 서비스를 함께 둔다.
- P18(다중 환경·시스템·코드 수정 PR·승격)은 개발 중이다. 계획 문서는 완료 증거가 아니며, 저장소 생성·소스 업로드와 실제 PR/배포 실연 결과를 구분한다. 공개본에는 기존 로컬 Git 이력·자격정보·원본 운영 증거를 포함하지 않는다.

- [시스템 구조도: draw.io 3페이지](docs/diagrams/liveprobe_agent.drawio)
- [P12 결과보고서](docs/plans/revamp/P12-final-report.md) · [정책 C](docs/plans/revamp/P12-agent-action-policy.md) · [P13 신규 사례](docs/plans/revamp/P13-results.md)
- [시험 사례별 결과표](docs/plans/revamp/P14-case-table.md) · [CSV](docs/plans/revamp/P14-case-table.csv)
- [과제계획서 대비 차이](docs/plans/revamp/P14-plan-gap.md) · [P14 통합·검증 기록](docs/plans/revamp/P14-execution.md)

## 현재 구조와 소스

```text
Kubernetes Event / Pod / Endpoint 감시 + Alertmanager + Hubble drop/error
  → 사건 병합 → 실시간 상태·지표·로그·변경 근거 수집
  → 규칙 + LLM Router (SIMPLE / NORMAL / COMPLEX / HIGH_RISK)
  → 수준별 Agents Group + 런북·공개 문서 RAG
  → 독립 근거 검증 / 필요한 역할만 승격 / 조기 종료
  → 안전 실행기 또는 운영자 이관
  → PostgreSQL/pgvector 기록 + SSE → React 대시보드

Istio → Kiali 서비스 메시 그래프
Hubble → 자원·네트워크 지도 + NetworkDrop 사건(HIGH_RISK 이관)
```

| 경로 | 역할 |
|---|---|
| `liveprobe/app/main.py`, `models.py`, `db.py`, `liveprobe/alembic/` | FastAPI, 사건·호출·근거·감사 저장, DB 마이그레이션 |
| `watcher.py`, `incidents.py`, `evidence.py`, `k8s.py` (`liveprobe/app/`) | 신호 감시, 사건 병합, Kubernetes/Prometheus/Loki 근거 수집 |
| `router.py`, `engine.py`, `agent_groups.py`, `config.py` (`liveprobe/app/`) | 라우팅, 수준별 실행 흐름, 역할·모델·예산·승격 규칙 |
| `model_settings.py`, `network_signals.py` (`liveprobe/app/`) | 버전형 그룹 모델 배치·실행 스냅샷, 대상 Deployment의 네트워크 신호 연결 |
| `rag.py`, `rag_ingest.py`, `liveprobe/runbooks/` | pgvector 검색, 런북·공개 문서·정제된 종료 사건 인덱싱 |
| `actions.py`, `safety.py`, `rollback_controller.py`, `policy.py` (`liveprobe/app/`) | 타입 조치, 권한·정책·동시성·원복 검증, 검토 장부 |
| `dashboard/src/` | Kiali 첫 화면, 지도, 사건 작업공간, 검토 기록, 설정 |
| `deploy/base/`, `deploy/overlays/`, `deploy/optional/` | Kustomize 리소스, 환경별 값·이미지 pin, 선택적 쓰기 실행 계정 |
| `services/order/`, `services/inventory/` | Spring 참조 서비스와 DB 경로 |
| `chaos/`, `evaluation/` | 장애 사례·주입/원복 장치, 비교 방식·원본 저장·결정적 지표 계산 |
| `scripts/export_case_results.py` | 기존 평가 원본 → Markdown/CSV, 오프라인·LLM 호출 없음 |

### 모델과 Workflow

운영 기본값은 `liveprobe/app/config.py`의 다음 라인업이다. 품질·처리량 점수는 설정상 상대 추정치이며 벤치마크 보장이 아니다.

| 계층 | 모델 ID | 대표 역할·추론 강도 |
|---|---|---|
| 낮음 | `gpt-6-luna` | Router·신호 분석·로그 요약 / low |
| 중간 | `gpt-6.1-sol` | 근거 정리·진행 조정·복구 계획·결과 확인 / low 또는 medium |
| 최고 | `gpt-6-astra` | Kubernetes 분석·원인 추론·반론 검토 / high, 안전 검증 / xhigh |

- SIMPLE: 신호 분석 + 결과 확인. NORMAL: 로그 요약·Kubernetes 분석·조정·독립 검증. COMPLEX: Kubernetes/로그 분석·원인 추론·복구 계획·반론·독립 검증. HIGH_RISK: 근거 정리·안전 검증 후 이관.
- 근거 위조·누락·낮은 신뢰도는 해당 역할만 승격하거나 이관한다. 호출 수·비용·시간·재추론 횟수 상한이 있다.
- **P10~P13 평가 기록은 이전 `gpt-6-luna` / `gpt-5.6-terra` / `gpt-6-sol` 조합**이다. 새 모델명이나 현재 가격으로 과거 결과를 덮어쓰지 않는다. 비용은 기록된 토큰 환산 추정치이며 구독 청구액이 아니다.
- 코드에는 다른 공급자 어댑터도 있지만, 이 프로젝트의 현재 운영·평가 경로는 **`codex_cli`만 사용**한다. 제한·실패 시 API 키나 다른 공급자로 우회하지 않는다.
- **그룹 모델 편집**: `/settings`에서 네 수준의 역할별 모델·추론 강도와 공통 분류 역할을 수정한다. 주 분석 모델은 기본 `라우터 자동 선택`이며 `직접 지정`에서만 고정한다. `/api/model-settings`에 revision 기반으로 저장하고 충돌은 409로 거절한다. 새 설정은 **신규 실행부터** 적용하며 진행 중 실행·평가 arm·과거 기록은 바꾸지 않는다. 기본 구성도 revision 0으로 기록한다. 검증 역할의 모델·추론 안전 하한과 가격·승격 사다리 검증은 완화할 수 없다.
- 기획과 실제 검증은 [그룹 모델 설정 기획](docs/plans/revamp/P14-group-model-settings.md), [실행 기록](docs/plans/revamp/P14-execution.md)에 분리했다. 실제 사건 #990에서 저장 버전 3의 astra/high·astra/xhigh 호출을 확인한 뒤 기본 배치를 버전 4로 복원했다.
- [Decisions API·Jev 기술 검토](docs/plans/revamp/P14-technology-review.md): 공식 기술을 비교했으나 별도 API 인증·과금·데이터 처리 경계가 Codex CLI 구독 전용 제약과 달라 현재는 도입하지 않았다.

### 정책 C와 안전 경계

- SIMPLE·NORMAL·COMPLEX는 검증된 조치를 기본 auto로 제안할 수 있다. 전역/사건별 `approval`, `suggest_only`는 이를 더 엄격하게 제한한다. 근거 부족·저신뢰·앱 코드/외부 의존성 원인은 이관한다.
- 평가 기본 프로필 `assessment`에서 **HIGH_RISK는 자동 실행도 승인 실행도 하지 않고 이관**한다. 별도 `poc_autonomy`는 고정된 PoC 클러스터에서만 허용되며, 여기서도 HIGH_RISK는 자동 실행이 아니라 운영자 승인 또는 제안에 머문다. 활성 상태는 `/api/policy/profile`로 확인한다.
- 현재 기본 allowlist: `rollout_undo`, `rollout_restart`, `pod_delete`, `scale_replicas`, `set_memory_limit`. 복제 수 1..2, 메모리 256..768Mi, 대상 소유권·범위·신뢰도·독립 검증·resourceVersion 등을 통과해야 한다.
- `configmap_restore` 핸들러는 있지만 기본 allowlist에 없고 승인된 불변 버전 목록도 비어 있으므로 **현재 실행 가능 조치로 세지 않는다**.
- API 관찰 계정과 쓰기 계정은 분리한다. 롤백은 전용 컨트롤러 경로, 타입 조치는 선택적 `safe-action-executor` 경로다. 실행 계정 토큰이 없으면 타입 조치는 닫힌 채로 거부된다. AI가 자유형 셸·kubectl·YAML을 실행하는 경로는 없다.
- NetworkPolicy·RBAC·admission 제한이 있다. 단, 구독 CLI를 위한 공개 HTTPS egress 허용은 데이터 유출 방지 경계가 아니며 프로덕션용 도메인 제한을 대신하지 않는다.

## 실행 환경과 설치

기준 환경은 **로컬 Rancher Desktop / Kubernetes / Docker**다. `kubectl`, `docker`, `bash`, `openssl`, Python **3.12**, `uv`, 프런트 개발용 Node/npm이 필요하다. 관측 환경의 Istio/Kiali·Hubble 연결 여부도 확인한다. 호스트 실행 도구와 이미지/모델 다운로드에는 네트워크가 필요하다.

**설치·갱신은 클러스터 쓰기 작업**이다. 아래는 운영자가 승인된 로컬 환경에서 실행할 절차이며, README/결과표 생성 작업 자체는 배포나 LLM 실호출을 하지 않는다.

1. 대상 context, namespace, 기존 데이터 보존·백업, 이미지 digest와 자원 여유를 먼저 확인한다. 기본 설치 대상은 `rancher-desktop`, `liveprobe-system`·`liveprobe-observability`·`liveprobe-target`과 프로젝트 RBAC/admission 리소스다. `KUBE_CONTEXT`, `OVERLAY`로 바꿀 수 있으므로 기본값만 믿지 않는다.
2. 구독 Codex CLI 인증과 설치 스크립트가 요구하는 운영자 소유 비밀 파일을 **로컬에서 안전하게 준비**한다. Secret·인증 파일·토큰·비밀번호를 화면/로그/문서/채팅에 출력하지 않는다. `scripts/create-secrets.sh`가 설치 과정에서 Secret을 준비한다. 인증 갱신 시 PVC에 복사된 Codex home의 반영 방식도 확인한다.
3. `deploy/overlays/rancher-desktop/platform.yaml`의 ingress/storage/API·Node CIDR와 실제 환경이 일치하는지 확인한다. `portable-example`은 예시이며 검증된 새 환경 설치로 간주하지 않는다.
4. 기본 overlay의 API/controller/대상 서비스는 digest pin을 사용한다. **`scripts/build-images.sh`로 `:dev`를 빌드하는 것만으로 pin이 바뀌지 않는다.** 신규 설치에 필요한 pin 이미지가 로컬 런타임에 있어야 하며, 없다면 실제 OCI manifest/index digest를 확보하고 overlay와 롤백 이미지 정책을 함께 검토해야 한다.

```sh
# 저장소 루트에서, 승인된 환경과 비밀 입력을 준비한 후
scripts/validate-manifests.sh
KUBE_CONTEXT=rancher-desktop OVERLAY=deploy/overlays/rancher-desktop scripts/install.sh
scripts/rbac-matrix.sh
scripts/netpol-check.sh

# 읽기 전용 상태 확인
kubectl --context rancher-desktop -n liveprobe-system get pods
kubectl --context rancher-desktop -n liveprobe-observability get pods
kubectl --context rancher-desktop -n liveprobe-target get pods
curl --fail http://liveprobe.localhost/api/healthz
curl --fail http://liveprobe.localhost/api/readyz
```

`install.sh`는 base stack을 적용하지만 **선택적 실행 계정, Istio/Kiali/Hubble까지 모두 새로 구성하는 정책 C 원클릭 설치는 아니다**. `deploy/optional/safe-action-executor/`는 기본 base에 포함되지 않는다. 정책 C 쓰기를 재현하려면 해당 계정·RBAC·admission·토큰 마운트와 런타임 활성 상태를 별도 승인·검증해야 한다. 관측 컴포넌트가 없으면 Kiali iframe/네트워크 흐름도 정상 표시되지 않을 수 있다. 상세 운영 조건은 [재현·운영 게이트](docs/operations/kan57-reproduction.md)를 참고하되, 그 문서의 과거 cookie 인증 설명보다 현재 `auth.py`와 아래 로컬 접근 규칙을 우선한다.

### 접속 경로

| URL/경로 | 기능 |
|---|---|
| `http://liveprobe.localhost/` | **Kiali 서비스 메시 첫 화면** (LiveProbe 셸 내부 iframe) |
| `/map` | 자원·네트워크 지도 |
| `/incidents`, `/incidents/:id` | 사건 목록·근거·라우팅·호출·조치·대화/지시·인수 |
| `/ledger` | 운영자 검토 장부 (평가 결과 UI가 아님) |
| `/settings` | 자동화 설정·상태, 그룹별 역할 모델·추론 강도 편집 |
| `http://kiali.localhost/kiali/console` | Kiali 별도 화면 |
| `http://grafana.localhost` | 관측 대시보드 |
| `/api/docs`, `/api/openapi.json` | 현재 FastAPI 인터페이스 |
| `/api/status`, `/api/policy/profile`, `/api/llm/accounts` | 시스템·정책·구독 계정 상태 |
| `/api/incidents`, `/api/incidents/{id}`, `/api/incidents/{id}/events` | 사건 조회·상세·이벤트 스트림 |
| `/api/stream`, `/api/topology` | 전역 SSE·자원/흐름 정보 |
| `/api/settings`, `/api/review-ledger`, `/api/review-ledger.csv` | 운영 설정·검토 장부 |
| `/api/model-settings` | 그룹 모델 버전형 설정 조회·저장(동일 출처 쓰기 가드) |

현재 로컬 PoC는 **사용자 로그인/계정 인증이 없다**. API의 Host allowlist와 쓰기 요청의 same-origin/Fetch-Metadata·`X-LiveProbe-Client` 검사만으로 접근을 제한하며, 이는 사용자 인증/권한 관리가 아니다. Alertmanager webhook 토큰 검증은 별도다. 네트워크에 공개하지 않는다. 프로덕션 사용 전 사용자 인증·권한·TLS·egress·백업/복구·다중 replica 제어를 별도로 검증해야 한다.

## 개발·검증·갱신

```sh
# 백엔드 Python 3.12 환경 준비
cd liveprobe
uv sync --group dev
# 전체 API 테스트: LIVEPROBE_TEST_DB_ADMIN이 가리키는 전용 pgvector 테스트 DB 필요
# 테스트는 public schema를 drop/recreate하므로 운영 DB를 절대 지정하지 않는다.
uv run pytest
cd ..

# 평가 도구 단위 테스트: DB/클러스터/LLM 호출 없음
PYTHONDONTWRITEBYTECODE=1 liveprobe/.venv/bin/python -m pytest -p no:cacheprovider evaluation/tests -q

# 프런트
cd dashboard
npm ci
npm test
npm run build
cd ..
```

프런트 개발 서버는 `cd dashboard && npm run dev`로 실행한다. Vite 개발 프록시/현재 API 도달성을 함께 확인한다. API의 일반 운영 경로는 인클러스터 kube/DB/설정·RAG와 연결되므로 `uvicorn`만 실행해 완전한 로컬 데모가 된다고 가정하지 않는다.

코드를 갱신할 때는 관련 테스트 → 프런트 빌드 → 필요한 이미지 빌드(`scripts/build-images.sh liveprobe dashboard` 등) → **실제 배포 digest 및 overlay/롤백 정책 동기화** → 변경 리소스 diff 검토 → 승인된 범위 적용 → rollout·readyz·UI·활성 정책/모델 read-back 순서로 확인한다. Docker config ID를 배포 manifest digest와 혼동하지 않는다. `:dev` 이미지 교체나 설치 스크립트 재실행만으로 최신 소스가 실행 중이라고 주장하지 않는다. 전체 overlay 재적용은 변경 범위·webhook CA·불변 ConfigMap/PVC 영향을 먼저 검토한다.

P14는 기존 rancher-desktop에 배포했다. E8은 실제 배정 노드 상태, E9는 실제 참조 ConfigMap의 키·해시·변경 시각·승인 버전 일치만 제공하며 값·주석은 제외한다. 근거 수집은 Kubernetes/Prometheus/Loki 3개 작업자로 병렬화하고 전체 30초·개별 조회 제한시간과 완료 부분결과를 보존한다. 이벤트/로그는 반복 수를 유지해 중복 집계한다. 같은 경로의 drop/error 5건은 NetworkDrop 사건으로 연결하고 규칙상 HIGH_RISK는 LLM 응답과 관계없이 유지한다. 감지·전달 지연은 성능저하로 표시하며 결과 불명 신호는 중복 재전송하지 않는다.

최종 검증: API 631개, 평가·chaos 96개(부분 시험 18개 별도), 프런트 단위 29개, 브라우저 75개 통과. 실제 NetworkPolicy 차단 시험 #990은 HIGH_RISK 인계·자동 조치 0건, 정책·연결·시험 자원·모델 배치 복원 확인. 프런트의 약 512KB 청크 경고는 남아 있다. 전체 평가 재실행이나 목표 수치 개선을 뜻하지 않는다. 상세 근거와 제한은 [P14 실행 기록](docs/plans/revamp/P14-execution.md)에서 확인한다.

### 기존 평가 원본으로 결과표 재생성 (오프라인)

```sh
python3 scripts/export_case_results.py
python3 scripts/export_case_results.py --check
PYTHONDONTWRITEBYTECODE=1 liveprobe/.venv/bin/python -m pytest -p no:cacheprovider evaluation/tests/test_case_export.py -q
```

- 원본: `.e2e-tmp/revamp-P10/store-pilot`(A 64), `.e2e-tmp/revamp-P11b/store-chaos`(B 176), `.e2e-tmp/revamp-P12/store-policyC`(C 88), `store-p13`(24), `store-p13-n103`(2; 두 P13 경로는 revamp-P12 아래).
- 결과: **354행**, 구간별로 분리한다. 이는 서로 다른 case×arm 354개가 아니다. P13 수정 전/후 N1-03도 각 실행으로 보존한다. smoke/trial 13쌍과 A의 실패 attempt 1건은 본 표에서 제외하고 별도 내역을 남긴다.
- `manifest.json`·`progress.jsonl`의 해시, start/done, 입력·출력 대응, 완료/원복, 구간별 행수를 검증한다. CSV의 출처 줄 번호·줄 해시로 원본을 추적할 수 있다.
- 별도 사건 수준·라우팅 선택 이유·실제 인용/로그 본문은 progress에 없어 **미수집**으로 밝힌다. Workflow/결과 사유/근거 판정 건수로 대신 꾸며내지 않는다. 빈 목록·null·키 부재는 구분한다. DB 보충이나 현재 모델·단가 재계산은 하지 않는다.
- `.e2e-tmp` 원본은 로컬 자료라 clean checkout에는 없을 수 있다. 동일 원본을 복원해야 재생성할 수 있으며 누락 시 스크립트는 실패한다. `--check`는 기존 두 산출물과 바이트 단위로 대조하고 쓰지 않는다.
- 현재 목표 전부를 달성한 것은 아니다. Workflow 정확도·비용 절감·복구/이관 100%는 과거 보고서에서 미달이며, 정책·사례·지표 수집 조건이 다른 구간을 합쳐 개선을 주장하지 않는다. 새 모델 라인업 재평가는 수행하지 않았다.

### 실장애 시험은 별도 승인 필요

`chaos/`와 `scripts/eval-real.sh`는 실제 대상 변경과 구독 LLM 호출을 일으킨다. 자동으로 실행하지 않는다. 평가 lock·단일 주입 소유권·기준선·자원 여유·범위 백업을 확인하고 한 사례씩 주입 → 분석 → 원복 → 안정 상태를 증명한다. 불확실한 쓰기·원복 실패 시 즉시 중단한다. `scripts/e2e-mvp0.sh`는 예전 F1/F4 smoke 경로이며 전체 4 Family/60사례 검증을 대신하지 않는다. `scripts/uninstall.sh`는 namespace와 데이터를 삭제할 수 있으므로 데이터 보존 결정과 복구 가능한 백업 없이 실행하지 않는다.
