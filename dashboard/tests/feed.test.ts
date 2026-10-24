import { test } from 'node:test';
import assert from 'node:assert/strict';
import { diffAlerts, alertText, buildFeed, canSteer, type RunEvent } from '../src/feed.ts';
import type { Incident } from '../src/api.ts';

const inc = (id: number, status: Incident['status'], level: Incident['severity_level'] = 'NORMAL'): Incident =>
  ({ id, title: `t${id}`, namespace: 'ns', workload: `w${id}`, status, severity_level: level, signal_count: 1, created_at: '', updated_at: '', closed_at: null, total_cost_usd: null, agent_run_count: 0 });

test('first snapshot never notifies (no history replay)', () => assert.deepEqual(diffAlerts(null, [inc(1, 'open')]), []));
test('new open incident notifies once; unchanged does not', () => {
  const a = [inc(1, 'open')]; assert.deepEqual(diffAlerts(a, a), []);
  assert.deepEqual(diffAlerts(a, [...a, inc(2, 'open')]).map(x => [x.id, x.kind]), [[2, 'new']]);
});
test('already-resolved newcomer is not an alert', () => assert.deepEqual(diffAlerts([], [inc(3, 'resolved')]), []));
test('escalation and resolution', () => {
  assert.deepEqual(diffAlerts([inc(1, 'open', 'NORMAL')], [inc(1, 'analyzing', 'HIGH_RISK')]).map(x => x.kind), ['escalated']);
  assert.deepEqual(diffAlerts([inc(1, 'open')], [inc(1, 'resolved')]).map(x => x.kind), ['resolved']);
  assert.deepEqual(diffAlerts([inc(1, 'open', 'HIGH_RISK')], [inc(1, 'open', 'NORMAL')]), []);
});
test('alert text', () => assert.match(alertText({ id: 1, kind: 'new', title: '', level: 'COMPLEX', workload: 'orders', namespace: 'x' }), /orders.*복잡/));

const ev = (seq: number, kind: string, summary = '', detail: Record<string, any> = {}): RunEvent => ({ seq, kind, actor: 'x', summary, detail, at: '2026-10-02T00:00:00Z' });
test('operator message is folded with its applied marker; agent_started is hidden', () => {
  const f = buildFeed([ev(1, 'operator_message', '로그 먼저', { message_id: 7 }), ev(2, 'agent_started'), ev(3, 'operator_applied', '', { message_ids: [7] })]);
  assert.deepEqual(f.map(x => x.type), ['operator']);
  assert.equal((f[0] as any).applied, true);
  assert.equal((buildFeed([ev(1, 'operator_message', 'x', { message_id: 8 })])[0] as any).applied, false);
});
test('agent card carries causes with evidence refs, failed agents are marked', () => {
  const f = buildFeed([ev(1, 'agent_finished', '요약', { role: 'signal_analyst', status: 'succeeded', input_tokens: 5, output_tokens: 7, root_cause_candidates: [{ cause: 'bad tag', confidence: 0.9, evidence_refs: ['E1'] }] }),
    ev(2, 'agent_finished', '실패', { role: 'x', status: 'failed' })]);
  const a = f[0] as any, b = f[1] as any;
  assert.deepEqual([a.ok, a.tokens, a.causes[0].refs], [true, '5 / 7', ['E1']]); assert.equal(b.ok, false);
});
test('steps get a tone from the result, not from the wording', () => {
  const f = buildFeed([ev(1, 'action_executed', '', { status: 'failed' }), ev(2, 'verification_result', '', { ok: true }), ev(3, 'completed', '', { outcome: 'handed_off' }), ev(4, 'waiting_quota')]);
  assert.deepEqual(f.map(x => (x as any).tone), ['danger', 'ok', 'warn', 'warn']);
});
test('feed is ordered by seq even when events arrive shuffled', () => assert.deepEqual(buildFeed([ev(3, 'note'), ev(1, 'note'), ev(2, 'note')]).map(x => x.seq), [1, 2, 3]));
test('only unfinished incidents can be steered', () => {
  assert.equal(canSteer('analyzing'), true); assert.equal(canSteer('action_pending'), true);
  for (const s of ['resolved', 'handed_off', 'failed']) assert.equal(canSteer(s), false);
});
