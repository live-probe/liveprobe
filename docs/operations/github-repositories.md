# GitHub 저장소와 코드 수정 검토

## 공개 범위

| 구분 | 저장소 | 공개 범위 | 코드 배치 |
|---|---|---|---|
| LiveProbe 플랫폼 | https://github.com/live-probe/liveprobe | 공개 | API·AI 분석·대시보드·운영 도구·배포 정의 |
| 주문 시스템 | https://github.com/live-probe/order | 비공개 | `services/order`와 `services/inventory` |
| 결제 시스템 | https://github.com/live-probe/payment | 비공개 | 서비스 코드가 저장소 루트 |
| 회원 시스템 | https://github.com/live-probe/member | 비공개 | 서비스 코드가 저장소 루트 |
| 상품 시스템 | https://github.com/live-probe/catalog | 비공개 | 서비스 코드가 저장소 루트 |
| 배송 시스템 | https://github.com/live-probe/delivery | 비공개 | 서비스 코드가 저장소 루트 |
| 알림 시스템 | https://github.com/live-probe/notification | 비공개 | 서비스 코드가 저장소 루트 |

정확한 매핑은 루트의 `repositories.json`으로 관리한다. 시스템 `order`와 서비스 `order`를 구별한다. 서비스 `inventory`의 수정 PR도 `live-probe/order`에 만든다.

## 코드 수정과 사용자 검토

1. LiveProbe가 장애의 환경·시스템·서비스와 코드 근거를 연결한다.
2. 개발 환경 러너가 해당 비공개 저장소의 기준 브랜치를 가져온다. GitHub 인증은 호스트의 `gh` 인증을 사용하며 토큰을 설정 파일·소스·로그·API 응답에 담지 않는다.
3. 수정과 회귀 시험을 전용 브랜치에서 실행한다. 성공한 변경만 GitHub에 push하고 실제 PR을 생성한다.
4. 운영자는 GitHub의 Files changed·시험 결과·PR 대화를 검토한다. 자동 병합은 하지 않는다. 사용자와 실행기가 같은 GitHub 계정을 쓰면 작성자가 자기 PR에 공식 APPROVE를 남길 수 없으므로, 사용자가 직접 검토 후 병합한다.
5. 각 환경 승격은 여전히 LiveProbe에서 대상·부작용·원복 방법을 확인한 뒤 승인한다. PR 생성·병합이 배포 승인을 대신하지 않는다.
6. prd 승격은 실제 GitHub 병합 상태와 빌드한 정확한 소스 SHA가 대응해야 한다. GitHub 조회 실패·SHA 불일치·미병합을 승인으로 간주하지 않는다.

이 문서는 목표 계약이다. 실제 연결·PR 생성·승격을 시험하기 전에는 해당 기능을 완료로 보고하지 않는다. 최초 코드 업로드는 배포나 장애 주입이 아니다.

## 공개 저장소에 넣지 않는 자료

- 테스트 시스템 원본 소스(비공개 저장소에만 보관)
- `.env*`, OAuth·Codex·Claude 인증정보, 토큰·개인키·클러스터 Secret
- `.e2e-tmp`, `.liveprobe-runner`, DB dump·백업, 로컬 실행 로그
- `.hermes` 세션·개인 작업 기록, 원본 API 응답·운영 스크린샷·시험 원본 증거
- `node_modules`, 가상환경, Maven `target`, 패키지·이미지 산출물
- 기존 로컬 Git 이력(공개 저장소는 검사한 현재 파일로 새 초기 이력을 만든다)

공개 전 검사 결과는 값이 아닌 파일·규칙·건수로만 보관한다. 보안 회귀 시험에 필요한 합성 비밀값은 실제 자격정보가 아니며, 검사 예외가 필요하면 검토한 시험 파일의 정확한 위치·내용으로 한정한다. 운영 파일 전체를 검사에서 제외하는 방식은 쓰지 않는다.

## GitHub 요금제 제한

이 organization은 GitHub Free다. 비공개 `order` 저장소의 main branch protection 설정을 실제 API로 시도했으나 HTTP 403(업그레이드 또는 공개 저장소 필요)으로 거부됐다. 비공개 요구를 지키며 저장소를 공개하거나 요금제를 임의 변경하지 않는다. 따라서 비공개 main에 대한 GitHub 서버 차원의 강제 보호가 설정되었다고 주장하지 않는다. 사용자는 PR을 직접 검토·병합할 수 있으며, LiveProbe 러너의 main 직접 push 금지·자동 병합 금지·prd 병합/SHA 확인은 별도로 구현·검증한다.

공개 LiveProbe main에는 PR 경유·정확한 head의 `dashboard`/`backend` 성공·force push 금지·관리자 적용 보호를 설정했다. 같은 GitHub 계정이 PR 작성과 사용자 검토에 쓰이므로 자기 PR 공식 승인 요구를 1건으로 강제하지는 않는다. 이는 사용자 검토를 자동으로 대체한다는 뜻이 아니다.

7개 저장소 모두 Actions 기본 권한은 읽기 전용이며 Actions가 PR을 승인하지 못하게 설정했다. 자동 병합은 비활성화다.

## 로컬 작업과 원격 저장소의 관계

기존 작업 폴더와 이력은 그대로 보존한다. 분리된 저장소에 올릴 때는 필요한 소스만 별도 경로로 복사하고, 소스 파일 SHA와 원격 커밋을 기록한다. 공개용 저장소에 기존 폴더를 `git push --mirror`하지 않는다.

새 checkout에서 대상 시스템을 함께 빌드하려면 비공개 저장소 접근 권한으로 별도 clone해야 한다. 공개 저장소만 내려받아 테스트 시스템 전체를 받은 것으로 간주하지 않는다. 이미지는 비공개 소스와 별개로 다루며 공개 레지스트리에 올리지 않는다.
