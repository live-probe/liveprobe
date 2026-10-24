# P9 통합 검증 기록 (2026-10-05)

모두 기존 rancher-desktop 클러스터, 실제 장애 주입 + 실제 Codex CLI(구독, API 키 없음). 로그: .e2e-tmp/revamp-P9/e2e-mvp0.log

## 관측된 결과
| 시나리오 | 결과 |
|---|---|
| Family1 (잘못된 이미지, inventory) | 사건 #65 resolved, SIMPLE, 조치 1건 실행(롤백), 실제 에이전트 3회, 인용 6, 근거 출처 7. 이후 기준선 회복(이미지·selector·엔드포인트·order 200) |
| Family4 (Service selector 파손) | 사건 #66 handed_off, HIGH_RISK, 조치 실행 0, 에이전트 3회, 인용 6. 이후 기준선 회복 |
| 대시보드 감지 | 장애 중 지도에서 inventory 빨간 표시, 문제 목록 "일부 Pod 비정상"→"열린 사건 2건", 사건 배지 2, Service no_endpoints 표시(fault4-map-1440.png) |
| 실행 피드 | #66 이벤트 12개(증거 수집→에이전트 시작/종료→라우팅→인계) |
| 검토 기록 | #66 HIGH_RISK 인계가 review ledger에 1건 기록 |
| 계정 전환 | primary 인증 만료 상태에서 spare가 두 사건 전부 처리(spare calls 6). 피드에 "primary 인증 실패 — 재로그인이 필요합니다" 기록 |
| 스모크 | scripts/smoke-api.py를 로그인 제거 구조에 맞게 수정, 읽기 200·낯선 Host 거부·교차출처/무표식 쓰기 403 PASS |

## 발견 사항
1. 클러스터의 primary Codex 인증은 9/23~10/03에 만료돼 있었고 이 세션 이전부터 방치됨(내가 만든 장애 아님). 로컬 갱신본을 Secret liveprobe-llm에 반영(다른 키 유지, 백업 .e2e-tmp/revamp-P9/before-liveprobe-llm.secret.yaml — 비밀 포함이므로 커밋 금지) 후 API 재시작, 두 계정 available 확인.
2. 설계 문서는 "인증 오류는 spare로 넘기지 않는다"였으나 실제로는 인증 실패한 계정을 건너뛰고 spare가 처리함. 결과적으로 유용하나 설계 기술과 다름 → 문서 정정 필요.
3. 사이드카 주입으로 postgres 재시작 중 약 18초 5xx(내 작업 영향, 기준선에서 제외).
4. 한도(quota) 오류의 실제 형식은 아직 관측 못 함. quota 전환·대기는 모의 테스트 증거뿐.
5. 장애 중 브라우저 toast는 관측하지 못함(자동 캡처 시점 8회 모두 0건): 화면 배지·문제 목록은 확인. toast는 새 사건 감지 시점이 짧아 놓쳤을 수 있어 별도 확인 필요.
6. 네트워크 차단(drop) 경고 end-to-end는 미실행(NetworkPolicy 장애 미주입).
7. Kiali에서는 장애 시점의 5xx/연결 오류가 빨간 간선으로 표시됨을 사이드카 재시작 때 관측(이번 inventory 장애 중 화면은 미캡처).


## 추가 검증 (2026-10-05 00:43~00:55, 모두 실제 클러스터)

| 항목 | 관측 |
|---|---|
| 네트워크 차단 경고 | 방식: liveprobe-target의 allow-same-namespace 정책을 임시 삭제(백업 .e2e-tmp/revamp-P9/allow-same-namespace.yaml) 후 order 재시작. 결과: Retina가 POLICY_DENIED 98건(→102→109) 관측, network_alerts 1건, loadgen·order 노드 network_warnings, 지도에 "차단 109" 간선 라벨과 빨간 ! 배지 3개, 문제 목록 "네트워크 차단 102". 주문 요청은 503/타임아웃. 정책 복원 후 5/5 200, 정책 7개 원상 복구 |
| 시행착오 | egress 정책 추가(p9-fault-order-egress-deny)와 order 재시작 없는 정책 삭제는 차단을 만들지 못함(NetworkPolicy는 합집합이고 연결 재수립 전엔 기존 연결 유지). 제거 후 재시작으로 해결. 첫 두 시도는 증거로 쓰지 않음 |
| Kiali 장애 화면 | docs/plans/revamp/P9-captures/network-drop-kiali-1440.png — loadgen→order 간선과 order 노드가 빨간/주황으로 표시 |
| toast | MutationObserver로 기록: "새 사건: inventory ErrImagePull on Deployment/inventory" (class toast new), 사건 배지 1→2 |
| 운영자 일시정지 | #67에서 pause→이벤트 "운영자가 일시정지를 요청했습니다", 에이전트 다음 단계 전에 정지, resume 후 계속 |
| 운영자 메시지 | POST messages(필드명 body) 202 applies=next_node_boundary → 이벤트 #10 "운영자 메시지 1건을 signal_analyst 시작에 반영", 에이전트 결과가 메시지 요구대로 현재 이미지 태그를 인용 |
| 해결·원복 | #67 resolved, rollout_undo succeeded 및 검증 통과, 이미지 digest 기준선 복구 |
| 계정 | #67에서 primary auth 실패 → account_switched 이벤트 → spare가 처리 |

## primary 계정 후속 원인과 조치
앞서 Secret만 갱신했을 때 primary가 계속 auth_failed였던 원인: 파드 PVC의 /var/lib/liveprobe/codex/auth.json(9/23자, 만료된 refresh token)이 있으면 init 컨테이너가 Secret으로 덮어쓰지 않음(`if [ ! -f ... ]`, 실행 중 갱신된 토큰 보호 설계). 조치: 기존 파일을 auth.json.bak-20261005로 보관하고 Secret 값으로 교체, API 재시작 → primary·spare 모두 available. 코드는 의도된 보호라 변경하지 않음. 비밀값은 출력하지 않고 해시만 비교.
**주의**: 토큰은 약 7일 단위로 만료되므로 장기 실행(240쌍) 중 반복될 수 있음 → P10에서 사전 점검 항목에 포함.

## 미검증으로 남는 것
- 실제 Codex quota(한도) 오류 형식. 모의 테스트 증거뿐. 240쌍 중 실제로 맞닥뜨리면 그때 형식을 기록.
- 고위험(HIGH_RISK) 실제 실행은 assessment 프로필 정책상 수행하지 않음(인계가 정답).
