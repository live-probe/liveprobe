import type { Command, Operation } from './api.ts';

export const COMMAND_STATUS_KO: Record<string, string> = {
  planning: '계획 수립 중', awaiting_confirmation: '실행 확인 대기', awaiting_secret: '비밀값 입력 대기', executing: '실행 중',
  succeeded: '실행 완료', failed: '실행 실패', cancelled: '취소됨', rejected: '계획 거절됨', rolling_back: '원복 중', rolled_back: '원복 완료',
};
export const OP_KO: Record<string, string> = { apply: '적용', patch: '수정', delete: '삭제', scale: '수량 조정', restart: '재시작', cordon: '배치 차단', uncordon: '배치 재개', secret_update: '비밀값 변경' };
export const DOWNTIME_KO: Record<string, string> = { none: '중단 예상 없음', possible: '중단 가능성 있음', expected: '서비스 중단 예상' };
export const downtimeKo = (v: string | null | undefined) => v ? DOWNTIME_KO[v] ?? '중단 영향 미확인' : '중단 영향 미확인';
export const reversibleKo = (v: boolean | null | undefined) => v === true ? '원복 가능' : v === false ? '원복 불가' : '원복 가능 여부 미확인';
export const COMMAND_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'rejected', 'rolled_back']);
export const commandPollMs = (commands: Command[]) => commands.some(c => !COMMAND_TERMINAL.has(c.status)) ? 2000 : 10000;
export const secretKeys = (operations: Operation[] | null | undefined): string[] => [...new Set((operations ?? []).filter(o => o.op === 'secret_update').flatMap(o => o.keys ?? []))];
export function commandControls(command: Command) {
  const confirming = command.status === 'awaiting_confirmation';
  // Fail closed: never approve without a hash, disclosed effects and a successful dry-run for EVERY operation.
  const ops = command.plan?.operations ?? [];
  const dry = command.dry_run ?? [];
  const safeToConfirm = !!command.plan_sha256 && !!command.side_effects && ops.length > 0 && dry.length === ops.length &&
    ops.every((_o, i) => { const matches = dry.filter(d => d.op_index === i); return matches.length === 1 && matches[0].ok; });
  return {
    confirm: confirming,
    confirmEnabled: confirming && safeToConfirm,
    cancel: ['planning', 'awaiting_confirmation', 'awaiting_secret'].includes(command.status),
    secret: command.status === 'awaiting_secret',
    rollback: command.status === 'succeeded' && command.side_effects?.reversible === true && !!command.plan_sha256,
    busy: ['planning', 'executing', 'rolling_back'].includes(command.status),
  };
}
export const commandStatusKo = (s: string) => COMMAND_STATUS_KO[s] ?? '상태 미확인';
export const opKo = (s: string) => OP_KO[s] ?? '작업';
// 서버(secret_writer/command_api)와 같은 UTF-8 바이트 기준. 서버 제한이 최종 경계다.
export const SECRET_VALUE_MAX_BYTES = 2048;
export const SECRET_BODY_MAX_BYTES = 40960;
const utf8 = (s: string) => new TextEncoder().encode(s).length;
export function secretSizeError(values: Record<string, string>, planSha: string): string | null {
  const over = Object.keys(values).filter(k => utf8(values[k]) > SECRET_VALUE_MAX_BYTES);
  if (over.length) return `값이 2KiB를 넘습니다: ${over.join(', ')}`;
  if (utf8(JSON.stringify({ plan_sha256: planSha, values })) > SECRET_BODY_MAX_BYTES) return '전체 입력이 40KiB를 넘습니다. 키를 나누어 진행하세요.';
  return null;
}
type RollbackInfo = { requires_review?: boolean; verified?: boolean; approved_fields_restored?: boolean };
export function rollbackReviewKo(command: Command): string | null {
  const r = (command.rollback && typeof command.rollback === 'object' ? command.rollback : {}) as RollbackInfo;
  if (!r.requires_review && !String(command.error ?? '').includes('admission_drift')) return null;
  const restored = r.approved_fields_restored ? '승인한 변경은 되돌렸지만, ' : '';
  return `원복 완료가 아닙니다. ${restored}실행 중 클러스터가 승인하지 않은 변경을 추가해 남아 있을 수 있습니다. AI는 이 값을 저장하거나 자동 삭제하지 않으니 운영자가 대상 객체를 직접 검토하세요.`;
}
const COMMAND_ERROR_KO: Record<string, string> = {
  plan_has_no_operations: 'AI가 변경할 대상을 특정하지 못했습니다. 종류·이름·네임스페이스를 함께 적어 다시 지시하세요.',
  invalid_operation_count: '계획의 작업 수가 허용 범위(1~8개)를 벗어났습니다.',
  unreviewed_storage_resource: '저장소(PV·PVC·StorageClass) 변경과 새 볼륨 연결은 비밀값 우회 위험 때문에 지원하지 않습니다.',
  secret_single_operation_required: '비밀값 변경은 다른 작업과 함께 실행할 수 없습니다. 따로 지시하세요.',
  plan_hash_mismatch: '확인한 계획과 현재 계획이 다릅니다. 최신 계획을 다시 확인하세요.',
  stale_plan: '계획 이후 대상 내용이 바뀌었거나 API가 재시작되어 계획을 다시 검증할 수 없습니다. 다시 지시해 새 계획을 받으세요.',
  stale_rollback: '실행 이후 대상이 바뀌어 자동 원복을 멈췄습니다.',
  admission_drift: '클러스터 정책이 계획과 다른 변경을 덧붙여 실행을 멈췄습니다.',
  execution_admission_drift: '실행 중 클러스터가 승인하지 않은 변경을 덧붙였습니다. 대상 객체를 검토하세요.',
  rollback_unreviewed_admission_drift: '클러스터가 승인하지 않은 변경이 남아 있어 완전 원복이 아닙니다. 대상 객체를 검토하세요.',
  delete_not_completed: '삭제 요청은 수락됐지만 제한 시간 안에 객체가 사라지지 않았습니다. finalizer를 확인하세요.',
  secret_writer_unavailable: '비밀값 쓰기 계정이 준비되지 않았습니다.',
  operator_executor_unavailable: '운영자 실행 계정이 준비되지 않았습니다.',
  process_restarted_outcome_requires_inspection: '처리 중 API가 재시작되어 결과를 확정할 수 없습니다. 대상 상태를 직접 확인하세요.',
};
export function commandErrorKo(code: string | null | undefined): string {
  const c = String(code ?? '');
  if (COMMAND_ERROR_KO[c]) return COMMAND_ERROR_KO[c];
  if (c.startsWith('kubernetes_error:409')) return 'Kubernetes가 충돌(409)로 거부했습니다. 대상이 동시에 바뀌었을 수 있습니다.';
  if (c.startsWith('kubernetes_error:403')) return '실행 계정 권한 또는 클러스터 정책이 거부했습니다(403).';
  if (c.startsWith('kubernetes_error:422')) return '요청 형식이 Kubernetes 검증을 통과하지 못했습니다(422).';
  return '처리하지 못했습니다. 아래 오류 원문을 확인하세요.';
}
