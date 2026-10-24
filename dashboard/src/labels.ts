// Korean labels shared by every screen (pure, no React). The server sends Korean names too; these are the fallback
// so a bare role id or status code never reaches the operator.

/** Preserve legacy namespace/workload identity when environment metadata is absent. */
export function placeKo(environment: string | null | undefined, system: string | null | undefined, workload: string | null | undefined, namespace?: string | null): string {
  if (environment || system) return [environment, system, workload].filter(Boolean).join(' · ');
  return [namespace, workload].filter(Boolean).join('/') || '위치 미관측';
}
export const ROLE_KO: Record<string, string> = {
  router: '사건 수준 판단', signal_analyst: '신호 분석', result_verifier: '결과 확인', coordinator: '진행 조정',
  k8s_analyst: '쿠버네티스 분석', log_summarizer: '로그 요약', group_verifier: '결과 확인',
  k8s_log_analyst: '쿠버네티스·로그 분석', cause_reasoner: '원인 추론', challenger: '반론 검토', recovery_planner: '복구 계획',
  evidence_curator: '근거 정리', safety_verifier: '안전 검증', interpreter: '한글 해석', steering: '지시 해석', operator_planner: '작업 계획',
};
export const roleKo = (role: string | null | undefined, fallback?: string | null): string =>
  !role ? (fallback || '단계') : ROLE_KO[role] ?? (fallback && /[가-힣]/.test(fallback) ? fallback : '등록되지 않은 역할');

export const LEVEL_KO: Record<string, string> = { SIMPLE: '단순', NORMAL: '보통', COMPLEX: '복잡', HIGH_RISK: '고위험' };
export const GROUP_KO: Record<string, string> = { SIMPLE: '단순 그룹', NORMAL: '보통 그룹', COMPLEX: '복잡 그룹', HIGH_RISK: '고위험 그룹' };
export const levelKo = (level: string | null | undefined) => level ? LEVEL_KO[level] ?? level : '미분류';
export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'muted';
export const levelTone = (level: string | null | undefined): Tone =>
  level === 'HIGH_RISK' || level === 'COMPLEX' ? 'danger' : level === 'NORMAL' || level === 'SIMPLE' ? 'warn' : 'muted';

export const INCIDENT_STATUS_KO: Record<string, string> = {
  open: '접수', analyzing: '분석 중', action_pending: '승인 대기', acting: '조치 중', resolved: '자동 복구 완료', handed_off: '운영자 인계', failed: '처리 실패', closed: '운영자 종료',
};
export const incidentStatusKo = (s: string) => INCIDENT_STATUS_KO[s] ?? '상태 미확인';
export const incidentStatusTone = (s: string): Tone =>
  s === 'resolved' ? 'ok' : s === 'failed' ? 'danger' : s === 'handed_off' || s === 'action_pending' ? 'warn' : s === 'closed' ? 'muted' : 'info';

/** AI stage when the server did not send one: derived from the incident status only. */
export const STAGE_FROM_STATUS: Record<string, { key: string; label: string }> = {
  open: { key: 'routing', label: '사건 수준 판단 중' }, analyzing: { key: 'analyzing', label: '에이전트 분석 중' },
  action_pending: { key: 'acting', label: '운영자 승인 대기' }, acting: { key: 'acting', label: '조치 실행 중' },
  resolved: { key: 'resolved', label: '자동 복구 완료' }, handed_off: { key: 'handed_off', label: '운영자 인계' }, failed: { key: 'failed', label: '처리 실패' },
  closed: { key: 'closed', label: '운영자 종료' },
};
export const stageTone = (key: string | null | undefined): Tone =>
  key === 'resolved' ? 'ok' : key === 'failed' ? 'danger' : key === 'handed_off' ? 'warn' : 'info';

export const ACTION_KO: Record<string, string> = {
  rollout_undo: '이전 버전으로 롤백', rollout_restart: 'Deployment 재시작', pod_delete: 'Pod 재생성',
  scale_replicas: '레플리카 수 조정', set_memory_limit: '메모리 한도 조정', configmap_restore: 'ConfigMap 복원',
};
export const EFFORT_KO: Record<string, string> = { low: '낮음', medium: '중간', high: '높음', xhigh: '최고' };
export const effortKo = (e: string | null | undefined) => e ? EFFORT_KO[e] ?? e : '미기록';

export const SOURCE_KO: Record<string, string> = { kubernetes: 'Kubernetes', loki: 'Loki 로그', prometheus: 'Prometheus 지표' };
export function sourceKo(source: string, roleName?: (role: string) => string): string {
  if (source.startsWith('agent:')) { const r = source.slice(6); return `에이전트 · ${roleName ? roleName(r) : roleKo(r)}`; }
  return SOURCE_KO[source] ?? source;
}
export const KIND_KO: Record<string, string> = { event: '이벤트', log: '로그', status: '상태', metric: '지표', config: '설정', node: '노드', agent: '에이전트 결론' };
export const SEVERITY_KO: Record<string, string> = { error: '오류', warn: '경고', info: '정보' };

export const INTENT_KO: Record<string, string> = {
  instruction: '지시 반영', escalate: '상위 모델 승격', reroute: '그룹 변경', stop: '분석 중단', cluster_change: '클러스터 변경 요청',
};

/** Korean headline for a Kubernetes warning reason. Unknown reasons say so; the raw reason and message stay visible. */
export const EVENT_REASON_KO: Record<string, string> = {
  FailedMount: '볼륨 연결 실패', FailedAttachVolume: '볼륨 연결 실패', Unhealthy: '상태 확인(probe) 실패', BackOff: '재시작 대기(반복 실패)',
  CrashLoopBackOff: '반복 재시작', FailedSync: '파드 동기화 실패', FailedScheduling: '노드 배치 실패', OOMKilling: '메모리 초과 강제 종료',
  Evicted: '노드 자원 부족으로 퇴출', Killing: '컨테이너 종료', Failed: '컨테이너 실행 실패', ErrImagePull: '이미지 내려받기 실패',
  ImagePullBackOff: '이미지 내려받기 재시도 대기', FailedCreate: '생성 실패', FailedGetResourceMetric: '자원 지표 조회 실패',
  FailedComputeMetricsReplicas: '자동 확장 계산 실패', Rebooted: '노드 재부팅', NodeNotReady: '노드 준비 안 됨',
};
export const eventReasonKo = (reason: string) => EVENT_REASON_KO[reason] ?? '등록되지 않은 경고';
