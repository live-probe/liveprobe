// Run: node --test tests/mapModel.test.ts tests/feed.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildMapModel, alertOf, problemsOf, summarize, peersOf, EXTERNAL_ID, NODE_W, NODE_H, edgeWidth, type Topology, type TopoNode } from '../src/mapModel.ts';

const wl = (ns: string, name: string, extra: Partial<TopoNode> = {}): TopoNode =>
  ({ id: `${ns}/Deployment/${name}`, kind: 'Deployment', name, namespace: ns, status: 'ready', pods: 1, ready: 1, ...extra });
const topo = (over: Partial<Topology> = {}): Topology => ({
  generated_at: 1000, freshness: { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 5, error: null },
  nodes: [{ id: 'node/n1', kind: 'Node', name: 'n1', namespace: null, status: 'ready' }, wl('a', 'web'), wl('a', 'db'), wl('kube-system', 'dns'),
    { id: 'ip/1.2.3.4', kind: 'External', name: '1.2.3.4', namespace: null, status: 'observed_only' }],
  structure_edges: [{ from: 'a/Deployment/web', to: 'node/n1', type: 'runs_on' }, { from: 'a/Deployment/web', to: 'a/Deployment/db', type: 'selects' }],
  observed_edges: [{ from: 'a/Deployment/web', to: 'a/Deployment/db', flows: 100, ok: 100, dropped: 0, errors: 0, state: 'ok', last_seen: 995, ports: {}, drop_reasons: {}, via_service: {} }],
  network_alerts: [], ...over });

const svc = (ns: string, name: string, into: string): TopoNode => ({ id: `${ns}/Service/${name}`, kind: 'Service', name, namespace: ns, status: 'ready', role: 'service', folded_into: into, endpoints_ready: 1 });
const w = (ns: string, name: string, role: string, extra: Partial<TopoNode> = {}): TopoNode => ({ id: `${ns}/Deployment/${name}`, kind: 'Deployment', name, namespace: ns, status: 'ready', pods: 1, ready: 1, role, ...extra });
const ing: TopoNode = { id: 'shop/Ingress/web', kind: 'Ingress', name: 'web', namespace: 'shop', status: 'ready', role: 'entry', hosts: ['shop.localhost'], routes: [] };
const shop = (over: Partial<Topology> = {}): Topology => topo({
  nodes: [{ id: 'node/n1', kind: 'Node', name: 'n1', namespace: null, status: 'ready', role: 'node' }, ing,
    w('shop', 'web', 'service'), svc('shop', 'web', 'shop/Deployment/web'), w('shop', 'api', 'service'), svc('shop', 'api', 'shop/Deployment/api'),
    { ...w('shop', 'db', 'data', { data_store: 'postgres' }), id: 'shop/StatefulSet/db', kind: 'StatefulSet' },
    w('kube-system', 'coredns', 'system'), { id: 'ip/9.9.9.9', kind: 'External', name: '9.9.9.9', namespace: null, status: 'observed_only', role: 'external' }],
  structure_edges: [{ from: 'shop/Ingress/web', to: 'shop/Deployment/web', type: 'routes_to', via_service: 'web' }],
  observed_edges: [
    { from: 'shop/Deployment/web', to: 'shop/Service/api', flows: 50, ok: 50, dropped: 0, errors: 0, state: 'ok', last_seen: 995, ports: {}, drop_reasons: {}, via_service: {} },
    { from: 'shop/Deployment/api', to: 'shop/StatefulSet/db', flows: 40, ok: 40, dropped: 0, errors: 0, state: 'ok', last_seen: 995, ports: {}, drop_reasons: {}, via_service: {} },
    { from: 'ip/9.9.9.9', to: 'shop/Service/web', flows: 5, ok: 5, dropped: 0, errors: 0, state: 'ok', last_seen: 995, ports: {}, drop_reasons: {}, via_service: {} },
    { from: 'shop/Deployment/web', to: 'kube-system/Deployment/coredns', flows: 900, ok: 900, dropped: 0, errors: 0, state: 'ok', last_seen: 995, ports: {}, drop_reasons: {}, via_service: {} }],
  ...over });
const col = (m: ReturnType<typeof buildMapModel>, id: string) => m.nodes.find(n => n.id === id)!.layer;

test('flow layers: ingress/external first, services next, data store last; Services are folded away', () => {
  const m = buildMapModel(shop());
  assert.equal(col(m, 'shop/Ingress/web'), 0); assert.equal(col(m, EXTERNAL_ID), 0);
  assert.ok(col(m, 'shop/Deployment/web') < col(m, 'shop/Deployment/api'));
  assert.ok(col(m, 'shop/Deployment/api') < col(m, 'shop/StatefulSet/db'));
  assert.equal(m.layers.at(-1)!.title, '데이터 저장소');
  assert.ok(!m.nodes.some(n => n.node.kind === 'Service' || n.node.kind === 'Node'));
});

test('an edge into a Service is drawn into the workload behind it; external IPs collapse into one node', () => {
  const m = buildMapModel(shop());
  assert.ok(m.edges.some(e => e.source === 'shop/Deployment/web' && e.target === 'shop/Deployment/api'));
  assert.ok(m.edges.some(e => e.source === EXTERNAL_ID && e.target === 'shop/Deployment/web'));
  assert.equal(m.nodes.find(n => n.id === EXTERNAL_ID)!.members, 1);
});

test('ingress route is a structure edge; observed traffic on the same pair turns it into a flow edge', () => {
  const m = buildMapModel(shop());
  const r = m.edges.find(e => e.source === 'shop/Ingress/web' && e.target === 'shop/Deployment/web')!;
  assert.equal(r.observed, false); assert.equal(r.state, 'structure');
  const t = shop(); t.observed_edges.push({ ...t.observed_edges[0], from: 'shop/Ingress/web', to: 'shop/Deployment/web', flows: 7 });
  assert.equal(buildMapModel(t).edges.find(e => e.id === 'o:shop/Ingress/web>shop/Deployment/web')!.flows, 7);
});

test('system workloads are hidden by default and shown on request; hidden count is reported', () => {
  assert.ok(!buildMapModel(shop()).nodes.some(n => n.id === 'kube-system/Deployment/coredns'));
  assert.equal(buildMapModel(shop()).hidden.system, 1);
  assert.ok(buildMapModel(shop(), { showSystem: true }).nodes.some(n => n.id === 'kube-system/Deployment/coredns'));
  assert.ok(!buildMapModel(shop()).edges.some(e => e.target === 'kube-system/Deployment/coredns'));   // no edge to a hidden node
});

test('no node overlaps another and every edge ends on a drawn node', () => {
  const m = buildMapModel(shop(), { showSystem: true });
  for (const a of m.nodes) for (const b of m.nodes) if (a !== b) assert.ok(a.x + NODE_W <= b.x || b.x + NODE_W <= a.x || a.y + NODE_H <= b.y || b.y + NODE_H <= a.y);
  const ids = new Set(m.nodes.map(n => n.id)); for (const e of m.edges) assert.ok(ids.has(e.source) && ids.has(e.target));
});

test('stale flow view dims observed edges only', () => {
  assert.ok(buildMapModel(shop()).edges.filter(e => e.observed).every(e => !e.stale));
  const m = buildMapModel(shop({ freshness: { ...topo().freshness, stale: true } }));
  assert.ok(m.edges.filter(e => e.observed).every(e => e.stale)); assert.ok(m.edges.filter(e => !e.observed).every(e => !e.stale));
});

test('problems are explained in words and sorted danger-first', () => {
  const t = shop();
  t.nodes = t.nodes.map(n => n.id === 'shop/Deployment/api' ? { ...n, no_endpoints: ['api'] } : n.id === 'shop/StatefulSet/db' ? { ...n, status: 'down' } : n.id === 'shop/Ingress/web' ? { ...n, status: 'degraded', broken_routes: ['ghost'] } : n);
  const m = buildMapModel(t);
  assert.deepEqual(m.problems.map(p => p.name), ['db', 'web', 'api']);
  assert.match(m.problems[0].texts[0], /Pod/); assert.match(m.problems[2].texts[0], /엔드포인트/);
  assert.deepEqual(problemsOf(wl('a', 'x', { network_warnings: [{ peer: 'p', dropped: 9, errors: 0 }] })).map(p => p.alert), ['danger']);
});

test('alert rules: completed jobs and observed-only things are quiet, unobserved is never a warning', () => {
  assert.equal(alertOf(wl('a', 'x', { status: 'completed' })), 'none');
  assert.equal(alertOf({ id: 'i', kind: 'External', name: 'i', namespace: null, status: 'observed_only' }), 'none');
  assert.equal(alertOf(wl('a', 'x', { warnings: [{ incident_id: 1, level: 'NORMAL', status: 'open', title: '' }] })), 'warn');
  assert.equal(alertOf(wl('a', 'x', { warnings: [{ incident_id: 1, level: 'HIGH_RISK', status: 'open', title: '' }] })), 'danger');
});

test('summary: counts, freshness label', () => {
  const s = summarize(topo(), 2);
  assert.deepEqual([s.nodesReady, s.nodesTotal, s.openIncidents, s.freshness, s.observedFlows], [1, 1, 2, 'live', 100]);
  assert.equal(summarize(topo({ freshness: { ...topo().freshness, stale: true } }), 0).freshness, 'delayed');
  assert.equal(summarize(topo({ freshness: { ...topo().freshness, state: 'never_connected', last_flow_age_s: null, stale: true } }), 0).freshness, 'never');
});

test('peers sorted by flow count with direction', () => {
  const t = topo(); t.observed_edges.push({ ...t.observed_edges[0], from: 'a/Deployment/db', to: 'kube-system/Deployment/dns', flows: 900 });
  const p = peersOf(t, 'a/Deployment/db');
  assert.deepEqual(p.map(x => [x.peer, x.direction]), [['kube-system/Deployment/dns', 'out'], ['a/Deployment/web', 'in']]);
});

test('a Service with no workload or traffic remains visible, especially when endpoints are missing', () => {
  const t = shop();
  t.nodes.push({ id: 'shop/Service/orphan', kind: 'Service', name: 'orphan', namespace: 'shop', role: 'service', status: 'down', endpoints_ready: 0 });
  const m = buildMapModel(t);
  assert.ok(m.nodes.some(n => n.id === 'shop/Service/orphan'));
  assert.ok(m.problems.some(p => p.id === 'shop/Service/orphan'));
});

test('edge width grows with flows but is bounded', () => {
  assert.ok(edgeWidth(1) < edgeWidth(1000)); assert.ok(edgeWidth(1e12) <= 4); assert.ok(edgeWidth(0) >= 1);
});
