import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildMapModel, type Topology } from '../src/mapModel.ts';
import { connectionsOf, edgeText, overviewMap } from '../src/overviewModel.ts';
import { actionChanges, currentIncident, pendingActions } from '../src/narrative.ts';
import { latestOnly } from '../src/latestOnly.ts';
import type { Overview } from '../src/api.ts';

// 독립 검토(2026-10-21) 지적 5건의 회귀 시험.
const node = (id: string, extra: Record<string, unknown> = {}) => ({ id, kind: 'Deployment', name: id, namespace: 'shop', status: 'ready', role: 'service', ...extra });
const edge = (from: string, to: string, extra: Record<string, unknown> = {}) => ({ from, to, flows: 0, ok: 0, dropped: 0, errors: 0, state: 'ok', last_seen: 10000 as number | null, ports: {}, drop_reasons: {}, via_service: {}, ...extra });
const topo = (edges: ReturnType<typeof edge>[], nodes = [node('a'), node('b')]): Topology => ({ generated_at: 10000, freshness: { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 1, error: null }, nodes, structure_edges: [], observed_edges: edges as never, network_alerts: [] });
const overview = (t: Topology): Overview => ({ generated_at: null, cluster: { nodes: [], namespaces: [] }, workloads: [], events: [], incidents: { open: [], today: { total: null, resolved: null, handed_off: null, failed: null } }, topology: t, sources: {} });

test('Envoy가 측정한 오류율이나 API의 저하 상태는 흐름 차단·오류 0이어도 정상으로 바꾸지 않는다', () => {
  assert.equal(buildMapModel(topo([edge('a', 'b', { req_rate: 10, err_rate: 1, state: 'degraded' })])).edges[0].state, 'degraded');
  assert.equal(buildMapModel(topo([edge('a', 'b', { req_rate: 10, err_rate: 0.02, state: 'ok' })])).edges[0].state, 'degraded');
  assert.equal(buildMapModel(topo([edge('a', 'b', { req_rate: 10, err_rate: 0, state: 'ok' })])).edges[0].state, 'ok');
  assert.equal(buildMapModel(topo([edge('a', 'b', { req_rate: 10, err_rate: null, state: 'ok' })])).edges[0].state, 'ok');
});

test('현재 응답의 Envoy 전용 연결은 흐름 시각이 없다는 이유만으로 지연 관측이 되지 않는다', () => {
  const fresh = buildMapModel(topo([edge('a', 'b', { req_rate: 10, err_rate: 0, last_seen: null })])).edges[0];
  assert.equal(fresh.stale, false);
  const old = buildMapModel(topo([edge('a', 'b', { flows: 5, ok: 5, last_seen: 100 })])).edges[0];
  assert.equal(old.stale, true, '흐름 시각이 오래되었으면 여전히 지연');
  const t = topo([edge('a', 'b', { req_rate: 10, err_rate: 0, last_seen: null })]); t.freshness.stale = true;
  assert.equal(buildMapModel(t).edges[0].stale, false, 'Hubble 지연은 Hubble 흐름이 없는 Envoy 전용 연결에 번지지 않는다');
});

test('혼합 연결은 상세에 모든 흐름 수치를 남기고 지도 문구는 짧게 유지한다', () => {
  const t = topo([edge('a', 'b', { flows: 26, ok: 12, dropped: 13, errors: 1, req_rate: 10, err_rate: 0.1 })], [node('a'), node('b', { namespace: 'other' })]);
  const e = buildMapModel(t).edges[0];
  assert.equal(edgeText(e), '10/초 · 오류 10% · 흐름 26 · 차단 13 · 오류 1');
  assert.equal(edgeText(e, { compact: true }), '10/초 · 오류 10% · 차단 13 · 오류 1');
  assert.match(connectionsOf(overviewMap(overview(t)), 'a')[0].text, /흐름 26 · 차단 13 · 오류 1/);
});

test('연결 수치 문구는 차단·오류 건수를 함께 보존한다', () => {
  const m = overviewMap(overview(topo([edge('a', 'b', { flows: 26, ok: 13, dropped: 13, state: 'blocked' })])));
  assert.match(connectionsOf(m, 'a')[0].text, /흐름 26 · 차단 13/);
  const e = buildMapModel(topo([edge('a', 'b', { flows: 9, ok: 9, errors: 4, req_rate: 2, err_rate: 0.5 })])).edges[0];
  assert.equal(edgeText(e), '2/초 · 오류 50% · 흐름 9 · 오류 4');
  assert.equal(edgeText({ observed: true, flows: 3, dropped: 0, errors: 0 }), '흐름 3');
});

test('해석이 이전 상태여도 최신 상세의 인계 상태와 담당 정보를 사용한다', () => {
  const n = { id: 1, status: 'action_pending', updated_at: '2026-10-22T00:00:00Z' } as import('../src/api.ts').Incident;
  const d = { ...n, status: 'handed_off' as const, updated_at: '2026-10-22T00:00:01Z', handoff: { claimed_by: 'operator' } };
  assert.equal(currentIncident(n, d).status, 'handed_off');
  assert.deepEqual(currentIncident(n, d).handoff, { claimed_by: 'operator' });
  assert.equal(currentIncident(n, { ...d, updated_at: n.updated_at }).status, 'handed_off');
  assert.equal(currentIncident(n, null), n);
});

test('더 오래된 상세는 새 상태를 되돌리지 않고 같은 상태의 인수 기록만 보존한다', () => {
  const n = { id: 1, status: 'resolved', updated_at: '2026-10-22T00:00:02Z' } as import('../src/api.ts').Incident;
  const d = { ...n, status: 'handed_off' as const, updated_at: '2026-10-22T00:00:01Z', handoff: { claimed_by: 'operator' }, status_reason: '이전 인계' };
  assert.equal(currentIncident(n, d).status, 'resolved');
  assert.equal(currentIncident(n, d).handoff, null);assert.equal(currentIncident(n, d).status_reason, null);
  assert.deepEqual(currentIncident({ ...n, status: 'handed_off' }, d).handoff, d.handoff);
});

test('같은 밀리초의 마이크로초 차이도 보존하고 시간대·소수 자릿수 차이는 정규화한다', () => {
  const i = (status: 'resolved' | 'handed_off', updated_at: string) => ({ id: 1, status, updated_at } as import('../src/api.ts').Incident);
  for (const [newer, older] of [
    ['2026-10-22T00:00:00.123999Z', '2026-10-22T00:00:00.123001Z'],
    ['2026-10-22T09:00:00.000001+09:00', '2026-10-22T00:00:00Z'],
    ['2026-10-21T23:00:00.123999-01:00', '2026-10-22T00:00:00.123Z'],
  ]) {
    assert.equal(currentIncident(i('resolved', newer), i('handed_off', older)).status, 'resolved');
    assert.equal(currentIncident(i('handed_off', older), i('resolved', newer)).status, 'resolved');
  }
  assert.equal(currentIncident(i('resolved', '2026-10-22T09:00:00.123000+09:00'), i('handed_off', '2026-10-22T00:00:00.123Z')).status, 'handed_off', '실제 같은 시각이면 상세 우선');
});

test('유효하지 않은 시각은 비교로 추정하지 않고 확인된 시각 또는 상세를 사용한다', () => {
  const n = { id: 1, status: 'resolved', updated_at: '2026-10-22T00:00:02Z' } as import('../src/api.ts').Incident;
  const d = { ...n, status: 'handed_off' as const, updated_at: '알 수 없음' };
  assert.equal(currentIncident(n, d).status, 'resolved');
  assert.equal(currentIncident({ ...n, updated_at: '' }, d).status, 'handed_off');
});

test('승인 대기 자동 조치는 실행 순서대로 찾고 다른 상태는 제외한다', () => {
  const actions = [{ id: 2, status: 'pending_approval' }, { id: 1, status: 'succeeded' }, { id: 3, status: 'pending_approval' }] as never;
  assert.deepEqual(pendingActions(actions).map(a => a.id), [2, 3]);
  assert.deepEqual(pendingActions(undefined), []);
});

test('승인 카드의 변경 값은 args만 요약하고 스냅샷 같은 중첩 원문은 요약에 넣지 않는다', () => {
  assert.deepEqual(actionChanges({ args: { memory_limit_mi: 768 }, kind: 'Deployment', proposal_snapshot: { uid: 'x' } }), ['memory_limit_mi=768']);
  assert.deepEqual(actionChanges({ replicas: 2, nested: { a: 1 } }), ['replicas=2']);
  assert.deepEqual(actionChanges(null), []); assert.deepEqual(actionChanges('text'), []);
});

test('최신 성공 뒤 도착한 이전 실패도 오류 처리에 반영하지 않는다', async () => {
  const gate = latestOnly(); const seen: string[] = [];
  let rejectOld!: (e: Error) => void;
  const old = gate.run(() => new Promise<string>((_, reject) => { rejectOld = reject; }), v => seen.push(v), () => seen.push('옛 오류')).catch(() => seen.push('외부 오류'));
  await gate.run(async () => '최신 목록', v => seen.push(v));
  rejectOld(new Error('이전 조회 실패')); await old;
  assert.deepEqual(seen, ['최신 목록']);
});

test('현재 요청의 실패는 유지하되 그보다 오래된 성공과 실패는 모두 폐기한다', async () => {
  const gate = latestOnly(); const seen: string[] = [];
  let releaseOld!: (v: string) => void;
  const old = gate.run(() => new Promise<string>(resolve => { releaseOld = resolve; }), v => seen.push(v));
  await gate.run(async () => { throw new Error('현재 실패'); }, v => seen.push(String(v)), e => seen.push((e as Error).message));
  releaseOld('이전 목록'); await old;
  assert.deepEqual(seen, ['현재 실패']);
  await assert.rejects(gate.run(async () => { throw new Error('처리자 없는 실패'); }, () => undefined), /처리자 없는 실패/);
});

test('늦게 도착한 이전 요청의 응답은 새 요청 결과를 덮어쓰지 않는다', async () => {
  const seen: string[] = []; const gate = latestOnly();
  let releaseOld!: (v: string) => void;
  const old = gate.run(() => new Promise<string>(r => { releaseOld = r; }), v => seen.push(v));
  await gate.run(async () => 'new', v => seen.push(v));
  releaseOld('old'); await old;
  assert.deepEqual(seen, ['new']);
});
