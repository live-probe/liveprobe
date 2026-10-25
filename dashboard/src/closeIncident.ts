// 운영자 종료(P17): AI가 이미 멈춘 장애를 사람이 확인하고 닫는 기록 결정. 클러스터 변경 경로가 아니다.

/** 종료의 부작용. 확인 창과 카드가 같은 문구를 보여 준다. */
export const CLOSE_EFFECTS = [
  '클러스터는 변경하지 않습니다. 기록만 남깁니다.',
  '지도 장애 표시와 진행 중 장애 알림에서 즉시 빠집니다.',
  '이 장애에는 더 이상 AI 지시나 조치를 받지 않습니다.',
  '같은 증상이 다시 감지되면 새 장애로 접수됩니다.',
] as const;

export const CLOSE_REASON_MAX = 500;

export function closeReasonError(reason: string): string {
  const r = reason.trim();
  if (!r) return '종료 사유를 입력하세요.';
  if (r.length > CLOSE_REASON_MAX) return `종료 사유는 ${CLOSE_REASON_MAX}자 이하로 입력하세요.`;
  return '';
}

export function closeConfirmText(id: number, reason: string): string {
  return [`장애 #${id}을(를) 운영자 종료합니다.`, `사유: ${reason.trim()}`, '', ...CLOSE_EFFECTS.map(e => `· ${e}`), '', '정말 진행하시겠습니까?'].join('\n');
}

export function closeErrorKo(status: number, detail: string): string {
  if (status === 409 && detail.includes('action')) return '승인 대기 중이거나 실행 중인 조치가 있습니다. 먼저 승인 또는 거절하세요.';
  if (status === 409 && detail.includes('command')) return '진행 중인 운영자 지시가 있습니다. 지시를 마치거나 취소한 뒤 종료하세요.';
  if (status === 409 && detail.includes('another operator')) return '다른 운영자가 인수한 장애입니다. 인수한 운영자만 종료할 수 있습니다.';
  if (status === 409) return '장애 상태가 바뀌어 종료할 수 없습니다. 최신 상태를 확인하세요.';
  if (status === 422) return '종료 사유와 확인이 필요합니다.';
  return '장애를 종료하지 못했습니다. 잠시 후 다시 시도하세요.';
}
