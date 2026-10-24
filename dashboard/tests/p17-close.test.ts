import { test } from 'node:test';
import assert from 'node:assert/strict';
import { acceptsInstructions, closable, isFinished, narrativePollMs, STAGE_KO } from '../src/narrative.ts';
import { incidentStatusKo, incidentStatusTone, STAGE_FROM_STATUS } from '../src/labels.ts';
import { alertText, canSteer, diffAlerts } from '../src/feed.ts';
import { CLOSE_EFFECTS, closeConfirmText, closeErrorKo, closeReasonError } from '../src/closeIncident.ts';
import type { Incident } from '../src/api.ts';

const inc = (status: Incident['status']): Incident => ({ id: 856, title: 'OOMKilled', namespace: 'liveprobe-target', workload: 'inventory', status, severity_level: 'NORMAL', signal_count: 1, created_at: '', updated_at: '', closed_at: null, total_cost_usd: null, agent_run_count: 0 });

test('운영자 종료는 끝난 상태이며 한국어 이름·중립 색상을 쓴다', () => {
  assert.equal(isFinished('closed'), true);
  assert.equal(narrativePollMs('closed'), 10000);
  assert.equal(canSteer('closed'), false);
  assert.equal(incidentStatusKo('closed'), '운영자 종료');
  assert.equal(incidentStatusTone('closed'), 'muted');
  assert.equal(STAGE_FROM_STATUS.closed.label, '운영자 종료');
  assert.equal(STAGE_KO.closed, '운영자 종료');
});

test('인계와 남은 승인 대기만 종료할 수 있고 종료 후에는 지시를 받지 않는다', () => {
  assert.deepEqual(['open', 'analyzing', 'action_pending', 'acting', 'resolved', 'handed_off', 'failed', 'closed'].filter(closable), ['action_pending', 'handed_off']);
  assert.equal(acceptsInstructions('handed_off'), true);
  assert.equal(acceptsInstructions('closed'), false);
});

test('종료 확인 문구는 부작용을 설명한 뒤 정말 진행할지 묻는다', () => {
  const text = closeConfirmText(856, '대상 정상 확인');
  assert.match(text, /#856/);
  assert.match(text, /대상 정상 확인/);
  for (const effect of CLOSE_EFFECTS) assert.ok(text.includes(effect), effect);
  assert.ok(CLOSE_EFFECTS.some(e => e.includes('클러스터') && e.includes('변경하지 않')));
  assert.ok(text.trimEnd().endsWith('정말 진행하시겠습니까?'));
});

test('종료 사유는 1~500자이고 서버 거부 사유를 한국어로 설명한다', () => {
  assert.ok(closeReasonError('  '));
  assert.ok(closeReasonError('가'.repeat(501)));
  assert.equal(closeReasonError('가'.repeat(500)), '');
  assert.match(closeErrorKo(409, 'an action is still pending or running; approve or reject it first'), /승인 또는 거절/);
  assert.match(closeErrorKo(409, 'an operator command is still in progress; finish or cancel it first'), /운영자 지시/);
  assert.match(closeErrorKo(409, 'handoff is claimed by another operator'), /다른 운영자/);
  assert.match(closeErrorKo(409, 'incident is analyzing; only a hand-off or a stale approval can be closed'), /최신 상태/);
  assert.match(closeErrorKo(500, ''), /종료하지 못했습니다/);
});

test('인계에서 종료로 바뀌면 해결이 아니라 운영자 종료로 알린다', () => {
  const [a] = diffAlerts([inc('handed_off')], [inc('closed')]);
  assert.equal(a.kind, 'closed');
  assert.equal(alertText(a), '운영자 종료: inventory');
  assert.equal(diffAlerts([inc('acting')], [inc('resolved')])[0].kind, 'resolved');
});
