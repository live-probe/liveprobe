import { test } from 'node:test';
import assert from 'node:assert/strict';
import { groupRaw, evidenceGroups, layoutFlow, flowHandles, flowTitle, flowSub, FLOW_W, FLOW_H } from '../src/narrative.ts';
import { overviewMap, MAP_W, MAP_H, isPlatform, resourceHealth, edgeLabelFits, connectionsOf } from '../src/overviewModel.ts';
import type { FlowNode, Overview, RawItem } from '../src/api.ts';
import type { Topology, TopoNode } from '../src/mapModel.ts';

const raw = (id: string, extra: Partial<RawItem> = {}): RawItem => ({ id, ref: id.split('#')[0], kind: 'log', severity: 'error', source: 'loki', at: null, raw: `line ${id}`, ko: '연결 풀 고갈', ko_source: 'rules', cited_by: [], ...extra });
const node = (extra: Partial<FlowNode> = {}): FlowNode => ({ id: 'a', type: 'agent', label: '분석', role: 'signal_analyst', role_ko: '신호 분석', job_ko: '', status: 'done', model: null, effort: null, tokens: null, cost_usd: null, duration_ms: null, inputs: [], conclusion_ko: null, conclusion_raw: null, lane: 0, depth: 0, group: null, ...extra });

test('같은 종류·출처·한글 해석의 원문은 한 묶음이 되고 원문 문자열과 순서는 그대로 유지된다', () => {
  const xs = [raw('E7#0'), raw('E5#0', { kind: 'event', source: 'kubernetes', ko: '재시작 반복', severity: 'warn' }), raw('E7#1', { severity: 'warn', cited_by: ['k8s_analyst'] }), raw('E7#2', { ko: null }), raw('E7#3', { ko: null })];
  const gs = groupRaw(xs);
  assert.deepEqual(gs.map(g => g.items.map(i => i.id)), [['E7#0', 'E7#1'], ['E5#0'], ['E7#2'], ['E7#3']]);
  assert.equal(gs[0].items[1].raw, 'line E7#1'); assert.equal(gs[0].severity, 'error'); assert.deepEqual(gs[0].cited_by, ['k8s_analyst']);
  assert.equal(new Set(gs.map(g => g.key)).size, gs.length);
});

test('단계를 고르면 그 단계가 인용한 원문만 묶고, 같은 해석이라도 인용하지 않은 원문은 섞지 않는다', () => {
  const xs = [raw('E1#0'), raw('E2#0', { ref: 'E2' }), raw('E1#1')];
  const step = node({ inputs: ['E1#0'], role: 'k8s_analyst' });
  assert.deepEqual(evidenceGroups(xs, step, 'all').flatMap(g => g.items.map(i => i.id)), ['E1#0']);
  const byRole = node({ inputs: [], role: 'k8s_analyst' });
  assert.deepEqual(evidenceGroups([raw('E1#0', { cited_by: ['k8s_analyst'] }), raw('E2#0')], byRole, 'all').flatMap(g => g.items.map(i => i.id)), ['E1#0']);
  assert.deepEqual(evidenceGroups(xs, null, 'all').flatMap(g => g.items.map(i => i.id)), ['E1#0', 'E2#0', 'E1#1']);
});

test('흐름은 depth를 세로, lane을 가로로 배치하고 지시용 음수 lane도 겹치지 않는다', () => {
  const ps = layoutFlow([node(), node({ id: 'b', depth: 1 }), node({ id: 'c', lane: -1 }), node({ id: 'd', lane: 1 })]);
  assert.ok(ps[1].y > ps[0].y); assert.equal(ps[1].x, ps[0].x);
  assert.ok(ps[2].x < ps[0].x); assert.ok(ps[3].x > ps[0].x); assert.equal(ps[2].y, ps[0].y);
  for (const a of ps) for (const b of ps) if (a !== b) assert.ok(a.x + FLOW_W <= b.x || b.x + FLOW_W <= a.x || a.y + FLOW_H <= b.y || b.y + FLOW_H <= a.y);
});

test('세로 흐름의 연결선은 위아래로 마주 보는 면에 붙는다', () => {
  assert.deepEqual(flowHandles({ x: 0, y: 0 }, { x: 0, y: 100 }), { sourceHandle: 'sb', targetHandle: 'tt' });
  assert.deepEqual(flowHandles({ x: 0, y: 100 }, { x: 0, y: 0 }), { sourceHandle: 'st', targetHandle: 'tb' });
  assert.deepEqual(flowHandles({ x: 0, y: 0 }, { x: 300, y: 0 }), { sourceHandle: 'sr', targetHandle: 'tl' });
  assert.deepEqual(flowHandles({ x: 300, y: 0 }, { x: 0, y: 0 }), { sourceHandle: 'sl', targetHandle: 'tr' });
});

test('흐름 노드는 한글 역할명 또는 라벨 머리말을 제목으로, 종류와 모델·내용을 둘째 줄로 쓴다', () => {
  assert.equal(flowTitle(node({ role: 'cause_reasoner', role_ko: '원인 추론' })), '원인 추론');
  assert.equal(flowSub(node({ role: 'cause_reasoner', model: 'gpt-6.1-sol' })), '에이전트 · gpt-6.1-sol');
  const signal = node({ role: null, type: 'signal', label: '감지 신호: 반복 재시작(CrashLoop), 5xx 오류 증가' });
  assert.equal(flowTitle(signal), '감지 신호'); assert.equal(flowSub(signal), '신호 · 반복 재시작(CrashLoop), 5xx 오류 증가');
  assert.equal(flowTitle(node({ role: null, type: 'outcome', label: '자동 복구 완료' })), '자동 복구 완료');
  assert.equal(flowSub(node({ role: null, type: 'outcome', label: '자동 복구 완료' })), '결과');
});

const fresh = { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 1, error: null };
const n = (namespace: string, name: string, extra: Partial<TopoNode> = {}): TopoNode => ({ id: `${namespace}/Deployment/${name}`, kind: 'Deployment', name, namespace, status: 'ready', role: 'service', ...extra });
function world(): Overview {
  const nodes: TopoNode[] = [
    n('shop', 'entry', { kind: 'Ingress', id: 'shop/Ingress/entry', role: 'entry' }), n('shop', 'order'), n('shop', 'db', { role: 'data', kind: 'StatefulSet', id: 'shop/StatefulSet/db' }),
    n('kube-system', 'coredns', { role: 'system' }), n('kube-system', 'broken', { role: 'system', status: 'down' }),
    n('istio-system', 'istiod'), n('default', 'kubernetes', { kind: 'Service', id: 'default/Service/kubernetes' }), n('solo', 'worker'),
  ];
  const topology: Topology = { generated_at: 100, freshness: fresh, nodes, network_alerts: [], structure_edges: [{ from: 'shop/Ingress/entry', to: 'shop/Deployment/order', type: 'routes_to' }],
    observed_edges: [{ from: 'shop/Deployment/order', to: 'shop/StatefulSet/db', flows: 3, ok: 3, dropped: 0, errors: 0, state: 'ok', last_seen: 100, ports: {}, drop_reasons: {}, via_service: {} },
      { from: 'shop/Deployment/order', to: 'kube-system/Deployment/coredns', flows: 3, ok: 3, dropped: 0, errors: 0, state: 'ok', last_seen: 100, ports: {}, drop_reasons: {}, via_service: {} }] };
  return { generated_at: null, cluster: { nodes: [], namespaces: [] }, workloads: [], events: [], incidents: { open: [{ id: 7, title: '장애', namespace: 'istio-system', workload: 'istiod', severity_level: 'NORMAL', status: 'analyzing', stage: null, created_at: '' }], today: { total: null, resolved: null, handed_off: null, failed: null } }, topology, sources: {} };
}

test('플랫폼 구성요소는 기본으로 숨기되 문제·장애가 있는 구성요소와 선택한 네임스페이스는 숨기지 않는다', () => {
  const o = world();
  assert.equal(isPlatform(o.topology!.nodes[3]), true); assert.equal(isPlatform(o.topology!.nodes[5]), true); assert.equal(isPlatform(o.topology!.nodes[6]), true); assert.equal(isPlatform(o.topology!.nodes[1]), false);
  const m = overviewMap(o);
  assert.deepEqual(m.nodes.map(x => x.node.name).sort(), ['broken', 'db', 'entry', 'istiod', 'order', 'worker']);
  assert.equal(m.hiddenPlatform, 2); assert.ok(m.edges.every(e => m.nodes.some(x => x.id === e.source) && m.nodes.some(x => x.id === e.target)));
  assert.equal(overviewMap(o, '', { showPlatform: true }).nodes.length, 8); assert.equal(overviewMap(o, '', { showPlatform: true }).hiddenPlatform, 0);
  assert.equal(overviewMap(o, 'kube-system').nodes.length, 2);
});

test('엔드포인트 0인 단독 Service는 관측된 경고이며 시스템 네임스페이스에서도 숨기지 않는다', () => {
  const o = world();
  o.topology!.nodes.push(n('kube-system', 'dns-check', { kind: 'Service', id: 'kube-system/Service/dns-check', role: 'system', status: 'no_endpoints', endpoints_ready: 0 }));
  const m = overviewMap(o); const s = m.nodes.find(x => x.node.name === 'dns-check');
  assert.ok(s, '숨겨짐'); assert.equal(s!.alert, 'warn');
  assert.equal(resourceHealth(s!.node), '엔드포인트 없음');
});

test('연결선 문구는 이웃한 열·행 사이 빈 칸에 놓일 때만 보이고, 다른 자원을 건너뛰거나 묶음을 넘거나 양방향이면 숨긴다', () => {
  const m = overviewMap(world(), '', { showPlatform: true });
  const at = (name: string) => { const x = m.nodes.find(n => n.node.name === name)!; const g = m.groups.find(g => g.id === x.groupId)!; return { x: g.x + x.x, y: g.y + x.y, group: x.groupId }; };
  const entry = at('entry'), order = at('order'), db = at('db'), dns = at('coredns');
  assert.equal(edgeLabelFits(entry, order, false), true);
  assert.equal(edgeLabelFits(entry, db, false), false, '열 하나 이상 건너뜀');
  assert.equal(edgeLabelFits(order, dns, false), false, '네임스페이스 경계를 넘음');
  assert.equal(edgeLabelFits(entry, order, true), false, '양방향은 문구가 서로 겹침');
  const below = { ...entry, y: entry.y + MAP_H + 20 }, skip = { ...entry, y: entry.y + 2 * (MAP_H + 20) };
  assert.equal(edgeLabelFits(entry, below, false), true); assert.equal(edgeLabelFits(entry, skip, false), false);
});

test('자원 상세의 연결 목록은 지도에서 숨긴 문구의 원천 수치를 방향별로 보존한다', () => {
  const o = world(); o.topology!.observed_edges[0] = { ...o.topology!.observed_edges[0], req_rate: 0.5, err_rate: 0.25 };
  const m = overviewMap(o, '', { showPlatform: true });
  const c = connectionsOf(m, 'shop/Deployment/order');
  assert.deepEqual(c.map(x => [x.direction, x.peer]).sort(), [['in', 'entry'], ['out', 'coredns'], ['out', 'db']]);
  assert.equal(c.find(x => x.peer === 'db')!.text, '0.5/초 · 오류 25% · 흐름 3');
  assert.equal(c.find(x => x.peer === 'coredns')!.text, '흐름 3');
});

test('네임스페이스 묶음은 사용하는 열만 차지하고 서로·자원끼리 겹치지 않으며 넓은 화면 비율로 배치된다', () => {
  const m = overviewMap(world(), '', { showPlatform: true });
  const solo = m.groups.find(g => g.name === 'solo')!, shop = m.groups.find(g => g.name === 'shop')!;
  assert.ok(solo.width < shop.width);
  for (const a of m.groups) for (const b of m.groups) if (a !== b) assert.ok(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y, `${a.name}/${b.name}`);
  const abs = m.nodes.map(x => { const g = m.groups.find(g => g.id === x.groupId)!; return { x: g.x + x.x, y: g.y + x.y, g }; });
  for (const a of abs) { assert.ok(a.x >= a.g.x && a.x + MAP_W <= a.g.x + a.g.width && a.y + MAP_H <= a.g.y + a.g.height); for (const b of abs) if (a !== b) assert.ok(a.x + MAP_W <= b.x || b.x + MAP_W <= a.x || a.y + MAP_H <= b.y || b.y + MAP_H <= a.y); }
  assert.ok(m.width >= m.height * .8, `${m.width}x${m.height}`);
});
