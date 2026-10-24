# P13 새 사례 (held-out)

- 목적: 판단 참고자료(`docs/reference/liveprobe-judgement-guide.md`)를 쓸 때와 안 쓸 때 adaptive 방식의 결과를 비교한다.
- 작성 시점: 참고자료 v1(sha256 `2fa8005b…`) **이후**다. 참고자료는 이 사례들을 입력으로 쓰지 않았고, `evaluation/guide_leak_check.py --held-out chaos/cases-new` 로 정답 문구와 사례 ID가 참고자료에 없음을 확인한다.
- 라벨 기준: **정책 C**(HIGH_RISK만 인계, 쿠버네티스 조치 5종). 원래 60사례 라벨(정책 B 이전 승인)과 기준이 다르므로 두 결과를 합산하지 않는다.
- 구성: 4 Family × 3. 기존 60사례와 대상·파라미터 조합이 겹치지 않는다(지속 시간 21~26초, 풀 점유 3·5, 지연 200ms, 셀렉터 order-broken-19, 이미지 태그 -19).
- 승인: 2026-10-11 사용자 위임("허가할게. 진행하자"). `LABELS.json` label_version 1.
