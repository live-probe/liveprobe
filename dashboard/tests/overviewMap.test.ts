import { test } from 'node:test';
import assert from 'node:assert/strict';
import { overviewMap, incidentFor } from '../src/overviewModel.ts';
import { buildMapModel, type Topology } from '../src/mapModel.ts';
import type { Overview } from '../src/api.ts';
const topology = (): Topology => ({ generated_at: 100, freshness: { state: 'connected', last_flow_age_s: 1, stale: false, stale_after_s: 60, window_s: 300, received_total: 1, error: null }, nodes: [{ id: 'ns/Ingress/entry', name: 'entry', kind: 'Ingress', namespace: 'ns', status: 'ready', role: 'entry' }, { id: 'ns/Deployment/web', name: 'web', kind: 'Deployment', namespace: 'ns', status: 'ready', role: 'service' }, { id: 'ns/Service/web-service', name: 'web-service', kind: 'Service', namespace: 'ns', status: 'unknown', folded_into: 'ns/Deployment/web', ports: [8080], endpoints_ready: 0 }], structure_edges: [{ from: 'ns/Ingress/entry', to: 'ns/Service/web-service', type: 'routes_to' }], observed_edges: [], network_alerts: [] });
const overview = (): Overview => ({ generated_at: null, cluster: { nodes: [], namespaces: [] }, workloads: [], events: [], incidents: { open: [], today: { total: null, resolved: null, handed_off: null, failed: null } }, topology: topology(), sources: {} });
test('네임스페이스 지도는 접힌 서비스의 이름·포트·준비 엔드포인트 0을 보존한다', () => {
 const m = overviewMap(overview()); const n = m.nodes.find(n => n.node.name === 'web')!;
 assert.deepEqual(n.node.services, [{ name: 'web-service', ports: [8080], endpoints_ready: 0 }]); assert.equal(m.groups[0].name, 'ns'); assert.equal(m.edges[0].observed, false);
 for (const a of m.nodes) for (const b of m.nodes) if (a !== b) assert.ok(a.x + 236 <= b.x || b.x + 236 <= a.x || a.y + 142 <= b.y || b.y + 142 <= a.y);
});
test('흐름이 없어도 Envoy에서 관측한 요청률 0은 유효한 관측값이다', () => {
 const t = topology(); t.observed_edges.push({ from: 'ns/Ingress/entry', to: 'ns/Service/web-service', flows: 0, ok: 0, dropped: 0, errors: 0, state: 'ok', last_seen: 100, ports: {}, drop_reasons: {}, via_service: {}, req_rate: 0, err_rate: 0 });
 const e = buildMapModel(t).edges[0]; assert.equal(e.observed, true); assert.equal(e.raw?.req_rate, 0); assert.equal(e.target, 'ns/Deployment/web');
});
test('지도 장애 핀은 워크로드의 명시된 연결을 우선하고 없는 자원을 만들지 않는다', () => {
 const o = overview(); o.workloads = [{ id: 'ns/Deployment/web', namespace: 'ns', name: 'web', kind: 'Deployment', ready: 1, desired: 1, restarts_1h: null, cpu: null, mem: null, req_rate: null, err_rate: null, p95_ms: null, incident: { id: 42, level: 'HIGH_RISK', status: 'analyzing', stage: null } }];
 assert.equal(incidentFor(o, o.topology!.nodes[1])?.id, 42); assert.equal(overviewMap(o, 'other').nodes.length, 0);
});
