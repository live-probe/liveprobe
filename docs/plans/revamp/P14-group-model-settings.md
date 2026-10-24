# P14 그룹별 모델 설정 기획

## 목표와 범위

운영자가 설정 화면에서 네 가지 사건 수준별 역할의 모델과 추론 강도를 바꾸고, 다음 분석 실행부터 적용한다. Jira는 사용하지 않는다. 과거 사건 기록과 진행 중 실행은 바꾸지 않는다. 기존 자동조치·고위험 인계·독립 검증 정책은 유지한다.

### 선택한 설계

- 기존 `/settings`에 `그룹별 모델` 영역을 추가한다. 네 가지 그룹 버튼, 역할/모델/추론 강도 행, 저장 전 변경 요약, 기본값 불러오기, 저장/다시 불러오기를 제공한다.
- 모델은 현재 검증한 3종 `gpt-6-luna`, `gpt-6.1-sol`, `gpt-6-astra`만 선택한다. 호출 방식은 Codex CLI로 고정하고 API 키 입력/전환을 제공하지 않는다.
- 주 분석 역할은 기본적으로 기존 라우터 자동 선택을 따른다. `직접 지정` 모드에서만 저장한 모델을 고정하며 화면에 이 차이를 명시한다. 역할별 추론 강도는 어느 모드에서도 적용한다.
- 라우터 분류 역할의 모델·강도도 공통 설정으로 제공한다. 승격 사다리/예산/안전조치는 이 화면에서 편집하지 않고 현재 값을 설명한다.
- SIMPLE: signal_analyst, result_verifier. NORMAL: coordinator, k8s_analyst, log_summarizer, group_verifier. COMPLEX: k8s_log_analyst, cause_reasoner, challenger, recovery_planner, group_verifier. HIGH_RISK: evidence_curator, safety_verifier.
- NORMAL과 COMPLEX의 group_verifier는 같은 이름이어도 별도 배치다. 설정은 수준별로 분리 저장하고 실행 직전에 그 그룹 배치를 복사한다.

### 저장과 적용

- `GET /api/model-settings`: revision, source, provider, catalog, defaults, router, groups, budget/ladder 반환.
- `PUT /api/model-settings`: expected_revision + router + groups. 전체 구성 검증 후 PostgreSQL settings의 별도 키에 원자 저장하고 감사 기록을 남긴다.
- 최초 저장도 advisory transaction lock으로 직렬화한다. 오래된 revision은 409로 거부하며 사용자 변경을 덮어쓰지 않는다. 알 수 없는 모델/강도/역할/그룹·여분 필드는 422로 거부한다.
- 실행 시작 시 revision과 설정을 한 번 읽어 실행 전용 cfg에 복사한다. 실행 중 저장/재시작이 발생해도 그 실행 배치를 바꾸지 않는다. 새 실행은 최신 revision을 읽는다. 기존 평가 arm은 운영 설정을 적용하지 않아 재현성을 보존한다.
- RoutingDecision.policy에 설정 revision, 실제 역할 배치와 주 모델 선택 출처를 보관한다. 실제 AgentRun 모델·추론 강도 기록도 그대로 유지한다.

### 안전 한도

- 결과 검증은 중간 모델 이상, COMPLEX 반론 검토와 HIGH_RISK 안전 검증은 최고 모델을 유지한다. 안전 검증 강도는 high/xhigh만 허용한다.
- 직접 지정 주 모델은 수준별 최소 품질을 충족해야 한다. 알 수 없는 가격이나 승격 불가능한 미등록 모델은 저장하지 않는다.
- provider와 안전 정책은 저장 payload에 없다. 임의 문자열이나 명령 입력을 허용하지 않는다.

### 대안과 거절 이유

- ConfigMap 직접 수정: 관찰 API에 Kubernetes 쓰기 권한을 추가하고 재시작까지 필요해 거절.
- 프로세스 전역 cfg 변경: 진행 중 사건의 모델이 바뀌고 여러 replica가 달라질 수 있어 거절.
- API 공급자/외부 decision engine 전환: 구독 Codex CLI 경계와 충돌 여부를 공식 출처 조사 후 별도 문서로 판단. 이름만으로 의존성이나 어댑터를 추가하지 않는다.

## 완료 기준

1. API 기본 구성, 검증 거절, 저장/재조회, 충돌, 재초기화 후 보존, 감사 기록 테스트.
2. NORMAL/COMPLEX 동일 역할 분리, 자동/직접 주 모델, 실행 스냅샷과 평가 제외 회귀 테스트.
3. 화면에서 모델·추론 수정→요약→저장→재조회 확인. 375/768/1440px에서 핵심 컨트롤 접근·가로 잘림 없음.
4. B4 라우팅/근거 패널은 좁은 화면에서도 토글로 접근 가능.
5. 전체 API/평가/프런트 테스트 및 빌드, 실제 배포 후 새 설정 조회·저장 검증. 시험 저장은 원래 배치로 복원한다.

## 검증 명령

- `uv run --project liveprobe pytest liveprobe/tests/test_model_settings.py -o addopts='' -q`
- `uv run --project liveprobe pytest liveprobe/tests -o addopts='' -q`
- `cd dashboard && npm test && npm run build`
- `cd dashboard && npx playwright test e2e/model-settings.spec.mjs`

외부 기술 검토: `P14-technology-review.md`. 현재 문서는 기획이며 실행 완료 증거는 `P14-execution.md`에 따로 기록한다.
