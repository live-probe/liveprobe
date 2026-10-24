# P14 최종 검증

## 결과

B1~B5·E1/E2 및 추가 요청인 그룹 모델 설정 기획/API/UI/실행 적용과 Decisions API·Jev 검토를 마쳤다. 기존 rancher-desktop에 API·대시보드와 대상 namespace의 ConfigMap 읽기 Role/Binding만 반영했다. 전체 overlay·업무 Deployment 재적용, 새 클러스터, Jira 연결, 외부 push, 평가 결과 전용 화면은 없다.

- API: `liveprobe/liveprobe@sha256:917366e7e14226bad696ac671952de40ed80ef601f41fcdbc474aba45ae64617`
- 대시보드: `liveprobe/dashboard@sha256:454183d91947c028ce3b74055329ef7349dac7c88c28e4e1094a02e776507cb1`
- 정식 Dockerfile 전체 빌드 성공, API Python 소스 51개가 이미지와 일치. Deployment 이미지·가용 replica·readyz를 다시 읽었다.
- 최종 API 설정 버전은 **4**, 역할 배치는 시험 전 기본값으로 복원했다. 사건 #990의 실행 스냅샷은 **3**으로 보존된다.

## 자동 검사

| 범위 | 실제 결과 | 경계 |
|---|---|---|
| LiveProbe 전체 | 631 passed, 180.09초, skip 0 | 전용 `lptest_p14_models`에서 실행 |
| 평가·chaos 단위 | 96 passed, 18 subtests passed | 실제 354쌍 재실행 아님 |
| 프런트 단위 | 29 passed, skip 0 | 순수 함수 회귀 |
| TypeScript/Vite | 성공 | 약 512KB 청크 크기 권고는 남음 |
| 전체 브라우저 | 75 passed, 25.9초 | 375/390/768/1280/1440px, 프로젝트별 반복 포함 |
| 사례표 | 354행, `--check` 바이트 일치 | A64/B176/C88/P13본24/재실행2, 실패 attempt 1·smoke/trial13쌍 제외 보존 |

모델 편집의 충돌/오류 UI 회귀는 모의 API를 사용한다. 영속화·실행 적용은 아래 실제 화면/DB/모델 호출로 따로 검증했다. DB fixture가 public schema를 지우므로 운영 DB나 다른 작업자의 테스트 DB를 함께 사용하지 않았다.

## 실제 클러스터·화면 확인

### B1: 최소정보 근거

사건 #990에 성공한 `node`와 `configmap` 근거가 저장됐다. 할당 노드만 조회하고 Ready/pressure·할당 가능 자원 등을 제공한다. 실제 참조 ConfigMap만 키/해시/변경 시각/승인 버전 일치를 제공한다. 시험 값 `p14-test-value`와 last-applied 주석은 반환 근거에서 검출되지 않았다. Node 보조 관찰의 존재를 실제 보호 자원 사건과 구분한다.

API 서비스 계정의 `auth can-i` 결과를 기대값과 대조했다.

- liveprobe-target ConfigMap get/list: 허용.
- liveprobe-target Secret get, Deployment patch: 거부.
- kube-system ConfigMap get: 거부.

### B2: 반복 수 보존 집계

이벤트는 reason/object/message별 count 합산, 로그는 전송/선두 시각을 제외한 앱·본문별 반복 수를 보존한다. 합치기 전에 truncation/redaction으로 다른 원문을 같게 만들지 않도록 테스트했다.

실제 한정 Pod stdout 20줄을 읽고 **전송 응답 형식만 모의하여** 운영 `_loki` 처리기에 재생했다. 20줄→1줄, JSON 1,200자→80자, 반복 수 20 보존. 시험 Pod는 UID 확인 뒤 제거했다. 이는 실제 Pod 로그의 오프라인 처리 검증이며 **운영 Loki의 해당 시험 Pod 수집 실증은 아니다**. Alloy의 기존 앱 allowlist는 order/inventory이므로 시험 Pod 로그의 실수집은 0행이었다. 이 필터를 바꾸거나 업무 로그에 시험값을 쓰지 않았다.

### B3: 병렬화·시간 제한·실패 격리

Kubernetes/Prometheus/Loki를 3개 작업자로 수집하고 전체 30초 제한을 둔다. 선택적 Node/ConfigMap이 느려도 완료된 core/service/event와 앞선 선택 조회 결과를 깊은 복사 체크포인트로 보존한다. 늦은 worker의 반환 Bundle 변경은 금지한다.

실제 inventory 읽기 3회씩의 수집 시간(초):

- 변경 전: 0.5569 / 0.2549 / 0.2228, E1~E7, 출처 오류 0.
- 최종 배포: 0.2447 / 0.2073 / 0.2708, E8/E9 포함, 출처 오류 0.

조회 범위가 다르고 3회뿐이므로 일반 성능 개선율·p95를 주장하지 않는다. 별도 지연 주입 테스트와 운영 실측은 구분한다.

네트워크 owner/service는 개별 8초, 폴링은 잠금·관측·전달 대기 포함 10초 예산이다. 관측 worker와 admitted delivery는 각각 최대 1개다. 이미 시작한 intake는 DB 반영이 불명확할 수 있어 강제 취소하거나 timeout만으로 중복 전송하지 않는다. pending 동안 degraded, 완료 Future는 다음 폴링이 회수하고 성공 watermark/확정 실패 재시도를 처리한다. worker가 감시 오류·watermark를 늦게 직접 바꾸지 않음을 검증했다.

### B4: 실제 반응형 화면

최종 소스에서 `/settings`와 `/incidents/990`을 1440/768/375px로 재캡처했다. 사건 문맥 패널 client/scroll 폭은 379/379, 712/712, 319/319였다. 모바일 대화↔판단·근거 전환과 모델→추론 선택의 Tab 이동을 확인했다. 최종 캡처 7개에 pageerror/console/HTTP 오류 0을 기록했다.

전체 브라우저 검사는 처음 768px 지도 상세 2개가 실패했다. 600px 이하에만 있던 bottom drawer를 1100px 이하까지 적용해 동일 전체 75개 검사가 통과했다. 캡처를 직접 읽어 한글/모델명/컨트롤 겹침·가로 잘림을 확인했으며, 이를 제품 전체 접근성 인증이라고 부르지 않는다.

### B5와 모델 설정: 실제 NetworkPolicy 시험 #990

평가 lock 비활성·기준 연결 성공·기존 정책 백업을 확인했다. 새 `p14-net-probe`와 `p14-net-sink`만 만들고 allow-same-namespace의 podSelector 한 필드에서 probe를 제외했다. 업무 Deployment의 image/replicas/설정은 바꾸지 않았다.

- 연결 차단 시도 7건을 관측했다.
- NetworkDrop → 사건 **#990**, **HIGH_RISK**, **handed_off**, 조치 **0**.
- 저장 버전 3의 `primary_mode=fixed`가 RoutingDecision에 보존됐다.
- 실제 구독 Codex 호출: router luna/low, evidence_curator astra/high, safety_verifier astra/xhigh 모두 성공.
- 정책 spec 원복·연결 회복을 확인하고 시험 Deployment 2개/ConfigMap 1개를 UID 확인 후 제거했다.
- 화면에서 기본 배치를 버전 4로 복원했다. 재조회에서도 #990은 버전 3/실제 역할 배치를 유지한다.

첫 시험 **#989는 실패 기록으로 보존**했다. LLM이 규칙의 HIGH_RISK에 동의하면 기존 분기가 COMPLEX로 내리던 오류를 드러냈다. 조치 없이 인계됐으며, 네트워크/보호 자원/라우팅 × 분류 출력 4종의 12개 회귀에서 3개 RED를 확인한 뒤 불하향 조건을 수정했다. 성공 결과로 덮어쓰지 않았다.

하이픈이 든 시험 앱 이름의 Loki 질의도 기존 잘못된 인용으로 400이었다. regex와 namespace를 JSON 문자열로 인용하도록 수정하고 실제 200을 확인했다. 시험 앱의 로그 0행은 인용 오류 해결과 별개인 Alloy allowlist 조건이다. #990의 당시 Loki 오류 기록은 소급 수정하지 않았다.

## 보안·원복·기록

- 신규 ConfigMap 권한은 target namespace get/list뿐이다. 값·주석·Secret·예외 본문 비노출 회귀와 독립 검토를 수행했다.
- 실행 전 DB 백업 `.e2e-tmp/revamp-p14/db/liveprobe-20261018T132651Z.dump`: 3,622,587 bytes, pg_restore 목록 읽기 통과. Alembic 0007.
- 기존 평가의 정책·모델·가격과 354행 원본은 불변이다. C1~C3 수치 목표 미달·D1/D2 추후 확장은 남는다.
- Decisions API·Jev는 공식 자료 검토 후 별도 인증/과금/데이터 경계 때문에 미도입. 외부 API 키·추론 요청·사건 로그 전송 없음.
- 임시 개발 서버 8001/5181/5182는 종료했다. QA DB·로컬 증거와 백업은 보존한다.

## 독립 검토 종료

- 모델 설정(deleg_3fa6aada), 원래 근거/네트워크 3건, 전달 pending·시간 제한(deleg_70710e7a)의 독립 재검토를 마쳤다.
- 실측에서 추가된 라우터 동의 분기·LogQL 문자열 인용·태블릿 상세 패널은 마지막 독립 검토(deleg_f44cd59c)에서도 통과했다. 검토자는 관련 비DB pytest 44개, 추가 분류 응답 조합 27개, 실제 Loki 읽기, 375/600/601/768/1100/1101/1440px 포인터/닫기 경계를 확인했고 새 보안·논리 결함을 보고하지 않았다. 이는 전체 631개/75개 검사를 다시 수행했다는 의미는 아니다.
- 제공 화면 추가 게이트는 `P14-surface-verification.md`에 별도 기록했다. 현재 제공 JS/CSS가 최종 빌드와 바이트 단위로 일치하고, 별도 실제 브라우저에서 데스크톱·모바일 하단 버튼/Tab/사건 패널 전환을 확인했다.
- 변경 파일 46개의 정적 비밀 검사에서 발견된 두 표식은 기존 HEAD에도 있는 redaction 시험용 합성 문자열이다. 자격 증명 파일과 DB 백업은 커밋 범위에서 제외했다.

## 근거 위치

저장소 루트의 `.e2e-tmp/revamp-p14/`에 원본 실행 증거를 보존한다(비밀과 백업을 커밋하지 않도록 gitignore 유지).

- `release-readback.json`, `final-readback.json`: 실제 이미지·권한·설정·사건·정리 재조회.
- `network-live-first-failed.json`, `network-live-verification.json`: 실패/성공 시험·원복·정리 내역.
- `full-api-tests-final.log`: API 631개 결과.
- `before-collect.json`, `final-collect.json`: 수집 시간 원본.
- `log-dedup-live.json`, `log-dedup-real-pod-replay.json`: Loki 200/0행과 실제 Pod 로그 재생의 구분.
- `live-ui/final-readonly-receipt.json`: 최종 소스 해시·캡처 7개·오류 기록.

이 로컬 원본들은 clean checkout에서 자동으로 생기지 않는다. 재현 명령·기획·사례표는 저장소 문서와 테스트에 보존한다.
