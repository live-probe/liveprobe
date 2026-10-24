# P15 화면 작업자 초기 제출 기록

이하 본문은 작업자가 최초 제출한 시점의 이력이다. 2026-10-21 레이아웃 재구성(지도 중심 운영 관제·세로 흐름·한글 우선 근거) 이후의 최신 수치·캡처는 `e2e/artifacts/manifest.json`과 `docs/plans/revamp/P15-final-verification.md`만 기준으로 한다. 아래의 캡처 경로는 레이아웃 재구성 전 이름이며 현재는 `artifacts/mock/`·`artifacts/live/` 아래에 있다. 이후 부모가 비밀값 바이트 제한·원복 검토 안내·재시작 합계·오류율 비율 표시를 보강했다. 최신 관측은 `docs/plans/revamp/P15-final-verification.md`를 따른다. 2026-10-22 p15p-local의 최신 관측은 단위 73개, 모의 API 브라우저 95개 통과·4개 의도적 건너뜀, 실배포 읽기 전용 23개 통과·16개 의도적 건너뜀이다. 캡처는 모의 99장·실배포 75장, 배치 측정은 249회이며 넘침·겹침·검사 대상 잘림은 0이다. 근거는 `e2e/artifacts/manifest.json`이다. 이전 레이아웃 검토의 5건과 후속 경계 결함을 수정했고 마지막 한정 독립 재검토가 통과했다. 검토 범위와 실패 이력은 최종 검증 문서를 따른다. 아래 예전 소스 해시와 미수행 목록을 현재 상태로 해석하지 않는다.

## 결과

- 운영 관제와 장애 관제실을 분리했고 Kiali 화면·구형 지도 화면 및 해당 탐색 항목을 제거했다. `/map`은 `/`로 이동한다.
- 네임스페이스 지도·장애 핀·원천 상태·경고 원문·미관측 처리, 세 열 관제실과 작은 화면 탭, 역할 상세·입력 근거 강조, 운영자 지시와 의무 부작용 확인·취소·원복·마스킹 입력을 구현했다.
- 검토 기록과 모델 설정을 유지했다. 새 실행 의존성과 패키지 설정 변경은 없다. 실제 클러스터 쓰기·배포·커밋은 수행하지 않았다.
- 최종 지시에 따라 메시지는 분석 중 2000자, 종료 후 지시는 8000자로 맞췄고 아래 최종 브라우저 시험에서 입력 제한 속성을 확인했다.

## 실제 검증 결과

작업 위치: `/Users/hwany/sds-ai-professional/dashboard`

| 명령 | 결과 | 증거 |
| --- | --- | --- |
| `npm test` | 41개 통과, 실패·건너뜀 0 | `e2e/artifacts/npm-test.log` |
| `npm run build` | 형 검사와 실제 빌드 성공, 종료 코드 0 | `e2e/artifacts/npm-build.log` |
| `LIVEPROBE_MOCK=1 npx playwright test` | 57개 통과, 실패·건너뜀 0, 약 2.1분 | `e2e/artifacts/playwright.log` |

375×844 / 768×1000 / 1440×1000에서 각각 실행했다. 111회 레이아웃 측정에서 문서 가로 넘침 0, 측면 영역 가로 넘침 0, 제어 겹침 0, 검사한 한국어 컨트롤 내부 잘림 0이었다. 선택한 흐름 노드가 캔버스 안에 완전히 들어오는 것도 확인했다. 이는 전면 접근성 인증을 뜻하지 않는다.

원문 보존·네 수준의 흐름·진행 중 상태·키보드 접근·확인·취소·원복·비밀값 초기화·지시 전송 경합·해시 충돌·빈 데이터·조회 실패를 확인했다. 기존 지도·라우팅 표 전용 시험은 새 화면 시험으로 대체했고 모델 설정 회귀 시험은 유지했다. 알림의 수준 표시가 한국어로 바뀌어 해당 단위 기대값을 갱신했다.

## 캡처와 직접 검토

최종 실행에서 실제 빌드 미리보기 화면 81개를 캡처했다. 화면당 경로·시각·뷰포트·소스 해시는 `e2e/artifacts/captures.jsonl`, 수치 측정은 `layout.jsonl`, 전체 연결은 `manifest.json`에 있다.

직접 이미지 검토에서 지도 제어가 노드 위를 덮는 문제와 숨겨진 모바일 탭을 연 뒤 선택 노드가 화면 밖으로 이동하는 문제를 발견해 수정했다. 제어를 캔버스 아래 별도 행으로 옮기고 실제 캔버스 크기 관측 후 선택 노드를 가운데 맞췄다. 수정한 화면은 최종 시험으로 다시 측정했다. 한국어는 줄바꿈하고 원문은 영역 안에서 이어 표시된다. 그래프 주변 노드가 캔버스 경계 밖으로 이어지는 것은 이동·확대 가능한 그래프의 의도된 동작이며, 선택 노드와 제어는 가려지지 않는다.

대표 원본 경로:

- `e2e/artifacts/overview-width-1440.png`
- `e2e/artifacts/overview-map-width-375.png`
- `e2e/artifacts/overview-map-width-768.png`
- `e2e/artifacts/overview-namespaces-width-375.png`
- `e2e/artifacts/resource-detail-width-1440.png`
- `e2e/artifacts/room-COMPLEX-resolved-flow-width-1440.png`
- `e2e/artifacts/room-SIMPLE-resolved-flow-width-375.png`
- `e2e/artifacts/command-confirm-width-375.png`
- `e2e/artifacts/command-secret-form-width-768.png`
- `e2e/artifacts/command-blocked-width-375.png`

직전 소스의 9개 묶음 이미지도 검토했으나 마지막 입력 글자수 변경 후의 증거 기준은 개별 원본과 `manifest.json`이다. 글자수 변경은 배치 변경이 아니며 최종 원본도 추가 확인했다.

## 계약 확인 요청·미수행

- 배포·실제 클러스터·실제 서버 조합의 화면 검증은 하지 않았다. 모의 서버 통과를 실제 장애 복구나 실제 비밀값 비저장 보증으로 해석하지 않는다.
- 추가로 전달받은 분석 중 `cluster_change`의 `command_id`·`applies=command_confirmation_required` 응답은 별도 분기하지 않는다. 기존 전송 성공 처리 뒤 명령 목록을 갱신하므로 카드 생성과 연결될 구조지만, 이 실제 응답의 모의 재현·통합 검증은 아직 없다.
- 명령 결과 사전은 문자열로 가정하지 않고 원문 객체를 그대로 직렬화해 표시한다.
- 추가 계약의 원복 상태 불일치 `failed`와 `requires_review` 조합은 실패 표시와 원문 반환 영역으로 남는다. `requires_review` 전용 한국어 안내 및 시험은 추가하지 않았다.
- 비밀값 입력은 현재 키별 `maxLength=2048` 문자 제한이다. 실제 서버의 키별 2KiB·요청 전체 40KiB 바이트 경계와 다르므로 다중바이트 입력의 클라이언트 사전 검증은 부모가 이어받아야 한다. 실제 서버가 최종 거부하는 제한을 완화한 것은 아니다.
- 진행 중 사례 1976과 명령 계획은 로컬 시험용 구성이다. 원문 로그·이벤트·에이전트 결론은 실제 표본에서 가져왔고 한글 해석은 화면 시험용으로 작성했다. 관측되지 않은 지표는 null이며 세부 출처와 한계는 `e2e/README.md`에 있다.
- 백엔드 계약의 오류율 단위가 비율인지 백분율인지 문서로 고정할 필요가 있다. 화면은 백분율 수치를 기대해 `%`를 붙인다.

## 변경 파일

- `dashboard/DESIGN.md`
- `dashboard/e2e/incident-routing.spec.mjs`
- `dashboard/e2e/model-settings.spec.mjs`
- `dashboard/e2e/service-map.spec.mjs`
- `dashboard/index.html`
- `dashboard/playwright.config.mjs`
- `dashboard/src/App.tsx`
- `dashboard/src/ClusterMap.tsx`
- `dashboard/src/Incidents.tsx`
- `dashboard/src/Mesh.tsx`
- `dashboard/src/ModelSettings.tsx`
- `dashboard/src/RoutingPanel.tsx`
- `dashboard/src/Shell.tsx`
- `dashboard/src/WorkflowGraph.tsx`
- `dashboard/src/api.ts`
- `dashboard/src/feed.ts`
- `dashboard/src/live.tsx`
- `dashboard/src/mapModel.ts`
- `dashboard/src/styles.css`
- `dashboard/tests/feed.test.ts`
- `dashboard/vite.config.ts`
- `dashboard/e2e/README.md`
- `dashboard/e2e/build-fixtures.py`
- `dashboard/e2e/fixtures/command-plans.json`
- `dashboard/e2e/fixtures/events-1976.json`
- `dashboard/e2e/fixtures/events-971.json`
- `dashboard/e2e/fixtures/events-976.json`
- `dashboard/e2e/fixtures/events-987.json`
- `dashboard/e2e/fixtures/events-990.json`
- `dashboard/e2e/fixtures/incidents.json`
- `dashboard/e2e/fixtures/narrative-1976.json`
- `dashboard/e2e/fixtures/narrative-971.json`
- `dashboard/e2e/fixtures/narrative-976.json`
- `dashboard/e2e/fixtures/narrative-987.json`
- `dashboard/e2e/fixtures/narrative-990.json`
- `dashboard/e2e/fixtures/overview.json`
- `dashboard/e2e/fixtures/topology.json`
- `dashboard/e2e/mock-api.mjs`
- `dashboard/e2e/p15-commands.mock.spec.mjs`
- `dashboard/e2e/p15-helpers.mjs`
- `dashboard/e2e/p15-incident-room.spec.mjs`
- `dashboard/e2e/p15-overview.spec.mjs`
- `dashboard/e2e/p15-states.mock.spec.mjs`
- `dashboard/src/CommandCard.tsx`
- `dashboard/src/FlowGraph.tsx`
- `dashboard/src/IncidentRoom.tsx`
- `dashboard/src/Overview.tsx`
- `dashboard/src/TopologyMap.tsx`
- `dashboard/src/commands.ts`
- `dashboard/src/graphControls.css`
- `dashboard/src/graphShared.tsx`
- `dashboard/src/labels.ts`
- `dashboard/src/narrative.ts`
- `dashboard/src/overviewModel.ts`
- `dashboard/src/ui.tsx`
- `dashboard/src/usePolling.ts`
- `dashboard/tests/overviewMap.test.ts`
- `dashboard/tests/p15.test.ts`

생성 증거는 `dashboard/e2e/artifacts/`와 `dashboard/e2e/test-results/`에 있다. 최종 소스 해시: `b9359145891168b6753ecf3f690c57d97fd9c5836fafdc14cfb581f9bf51a76d`.
