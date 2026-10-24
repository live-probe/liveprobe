# P0 실행 가능성 확인 결과 (2026-10-01)

사용자 지시로 계획서 D2를 변경: 새 kind 클러스터를 만들지 않고 기존 `rancher-desktop` 클러스터에서 진행한다.
이에 따라 흐름 수집은 Cilium(CNI 교체 필요) 대신 Retina(Hubble 제어 평면, CNI 무관)를 사용한다.

## 실측 사실

| 항목 | 결과 | 근거 |
| --- | --- | --- |
| VM 자원 | CPU 4, 메모리 12GB (사용자가 상향) | `docker info` cpus=4 mem=12525748224 |
| 클러스터 | k3s v1.36.2, 단일 노드 `lima-rancher-desktop`, arm64, flannel CNI | `kubectl get nodes -o wide`, `/etc/cni/net.d/10-flannel.conflist` |
| 커널 | 6.18.36-0-virt, BTF 존재(`/sys/kernel/btf/vmlinux`) | `rdctl shell` |
| kind 시도 | kind 클러스터 + Cilium 설치는 helm 대기 중 실패, 임시 클러스터 삭제 완료 (`kind get clusters` 비어 있음) | 터미널 기록 |
| Retina | `retina-hubble` 차트 v1.2.9, arm64 이미지 존재, kube-system에 설치 성공 (agent 1, operator 1, hubble-relay 1 모두 Running) | `helm install ... STATUS: deployed` |
| 자원 비용 | retina-agent 66Mi/15m, relay 13Mi/2m, operator 25Mi/2m. 노드 전체 4437Mi(37%) | `kubectl top` |
| 흐름 데이터 | Relay `GetFlows` gRPC 응답 200건 (허용 187, 차단 13), 연결 노드 1, 초당 약 57건, 링 버퍼 4095건 | `probe.py` 출력 |
| 관측된 통신 예 | loadgen→order, order→loadgen, coredns↔loadgen, coredns→apiserver, metrics-server, 외부 IP | `probe.py` 엣지 집계 |
| Codex | codex-cli 0.137.0, `~/.codex/auth.json` 1개(계정 1개 로그인, 값 미열람), 기본 모델 gpt-6-astra, `exec`에 --ephemeral·--output-schema·--json·resume 지원 | `codex login status`, `codex exec --help` |

## 발견한 한계 (계획 반영)

1. 흐름의 목적지가 Service ClusterIP(예: 10.43.0.10, 10.43.138.142)로 나오는 경우가 있다. 집계기에서 Service/Endpoint 정보로 이름을 풀어야 한다.
2. 출발·도착이 `0.0.0.0`인 DROPPED 13건은 의미 없는 노이즈 가능성이 있어 집계 시 분리해 확인한다.
3. 단일 노드라 `Node 재시작·drain`을 클러스터 안에서 시험하면 클러스터 전체가 중단된다. kind 방안을 접었으므로 이 조치는 이 클러스터에서 "하지 않음"으로 둔다(계획서 5.7 변경).
4. Retina는 클러스터 전체 통신을 보지만 암호화·메시 L7 내용은 보지 않는다. HTTP 단위 정보는 지금 보이지 않는다(L3/L4 중심).
5. Codex 계정이 현재 1개만 로그인되어 있다. 두 번째 계정 인증 파일이 필요하다(아래 미결).

## 미결 (사용자 조치 필요)

- 두 번째 Codex 계정: 별도 CODEX_HOME에 로그인한 인증이 필요하다. 인증값은 제가 만들거나 볼 수 없으므로 `CODEX_HOME=~/.codex-b codex login`을 직접 실행해 주세요. P6 전까지 필요합니다.
- 구독 한도 초과 시 오류 형식은 실제로 한도에 도달해야 확인된다. 미분류 오류는 재시도 1회 후 대기로 처리한다.

## 원복 방법

- Retina 제거: `helm --kube-context rancher-desktop -n kube-system uninstall retina`
- 설치 전 상태 백업: `.e2e-tmp/revamp-P0/pre-install-state.yaml`
