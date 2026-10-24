// Pure map model: Topology API response -> groups, nodes and edges for the canvas.
// No React here so the rules (what is drawn, how, and when it is called a warning) are unit-tested.

export type TopoNode = {
  id: string; kind: string; name: string; namespace: string | null;
  status: 'ready' | 'degraded' | 'down' | 'completed' | 'unknown' | 'observed_only' | string;
  pods?: number; ready?: number; completed?: number; node?: string | null;
  cluster_ip?: string | null; ports?: number[]; endpoints_ready?: number;
  role?: 'entry' | 'service' | 'data' | 'system' | 'job' | 'node' | 'external' | string;
  data_store?: string; folded_into?: string; no_endpoints?: string[]; broken_routes?: string[];
  services?: { name: string; ports: number[]; endpoints_ready: number | null }[];
  hosts?: string[]; routes?: { host: string | null; path: string; service: string; port: number | string | null; workload: string | null; backend_ready: boolean; backend_found: boolean }[];
  ingress_class?: string | null;
  warnings?: { incident_id: number; level: string | null; status: string; title: string }[];
  network_warnings?: { peer: string; dropped: number; errors: number }[];
};
export type StructureEdge = { from: string; to: string; type: string; observed?: boolean; via_service?: string };
export type ObservedEdge = {
  from: string; to: string; flows: number; ok: number; dropped: number; errors: number;
  state: 'ok' | 'degraded' | 'blocked' | string; last_seen: number | null; ports: Record<string, number>;  // null: Envoy-only edge
  req_rate?: number | null; err_rate?: number | null;
  drop_reasons: Record<string, number>; via_service: Record<string, number>; via_controller?: string;
};
export type Freshness = { state: string; last_flow_age_s: number | null; stale: boolean; stale_after_s: number; window_s: number; received_total: number; error: string | null };
export type Topology = {
  generated_at: number; freshness: Freshness; nodes: TopoNode[];
  structure_edges: StructureEdge[]; observed_edges: ObservedEdge[];
  network_alerts: { kind: string; from: string; to: string; dropped: number; errors: number; drop_reasons: Record<string, number>; last_seen: number }[];
};

export const NODE_W = 220;
export const NODE_H = 76;
const COL_GAP = 60;
const ROW_GAP = 20;
export const EXTERNAL_ID = '__external__';
export const COLUMN_TITLES = ['진입', '서비스', '서비스', '서비스', '서비스'];

export type Alert = 'none' | 'warn' | 'danger';

/** Why a resource is flagged, in words an operator can act on. Empty = nothing observed wrong. Unobserved is never "ok". */
export function problemsOf(n: TopoNode): { text: string; alert: Alert }[] {
  const out: { text: string; alert: Alert }[] = [];
  const ws = n.warnings ?? [];
  if (ws.length) {   // one line per workload, not one per incident
    const worst = ws.some(w => w.level === 'COMPLEX' || w.level === 'HIGH_RISK');
    out.push({ text: ws.length === 1 ? `사건 #${ws[0].incident_id} ${ws[0].level ?? ''}`.trim() : `열린 사건 ${ws.length}건`, alert: worst ? 'danger' : 'warn' });
  }
  for (const w of n.network_warnings ?? []) out.push({ text: `네트워크 ${w.dropped ? `차단 ${w.dropped}` : `오류 ${w.errors}`}`, alert: w.dropped > 0 ? 'danger' : 'warn' });
  if (n.status === 'down') out.push({ text: n.kind === 'Ingress' ? '연결된 백엔드 없음' : 'Pod 실행 안 됨', alert: 'danger' });
  else if (n.status === 'degraded') out.push({ text: n.kind === 'Ingress' ? '일부 경로 백엔드 없음' : '일부 Pod 비정상', alert: 'warn' });
  if (n.kind === 'Service' && n.status === 'no_endpoints') out.push({ text: 'Service 엔드포인트 없음', alert: 'warn' });
  if (n.no_endpoints?.length) out.push({ text: `Service 엔드포인트 없음 (${n.no_endpoints.join(', ')})`, alert: 'warn' });
  if (n.broken_routes?.length && n.kind === 'Ingress') out.push({ text: `끊긴 경로: ${n.broken_routes.join(', ')}`, alert: n.status === 'down' ? 'danger' : 'warn' });
  return out;
}
export function alertOf(n: TopoNode): Alert {
  const p = problemsOf(n);
  return p.some(x => x.alert === 'danger') ? 'danger' : p.length ? 'warn' : 'none';
}

export type MapNode = { id: string; x: number; y: number; layer: number; node: TopoNode; alert: Alert; members?: number };
export type MapEdge = {
  id: string; source: string; target: string; observed: boolean; state: string;
  flows: number; dropped: number; errors: number; stale: boolean; fresh: boolean; label?: string; raw?: ObservedEdge;
};
export type MapModel = {
  nodes: MapNode[]; edges: MapEdge[]; layers: { index: number; x: number; title: string; count: number }[];
  problems: { id: string; name: string; alert: Alert; texts: string[] }[];
  width: number; height: number; hidden: { system: number; jobs: number; unconnected: number };
};
export type BuildOpts = { showSystem?: boolean; showUnconnected?: boolean; now?: number };

const ROLE_ORDER: Record<string, number> = { entry: 0, service: 1, data: 2, system: 1, job: 3, external: 0 };

/** Resolve a node id to what is drawn: a Service becomes the workload behind it; unknown outsiders become one node. */
function makeResolver(topo: Topology, shown: Set<string>) {
  const byId = new Map(topo.nodes.map(n => [n.id, n]));
  return (id: string): string | null => {
    const n = byId.get(id);
    if (!n) return null;
    if (n.folded_into) return shown.has(n.folded_into) ? n.folded_into : null;
    if (n.role === 'external') return shown.has(EXTERNAL_ID) ? EXTERNAL_ID : null;
    return shown.has(id) ? id : null;
  };
}

export function buildMapModel(topo: Topology, opts: BuildOpts = {}): MapModel {
  const now = opts.now ?? topo.generated_at;
  // Fold backed Services into their workload, but never hide unbacked Services for lack of traffic.
  const drawable = topo.nodes.filter(n => n.kind !== 'Node' && n.role !== 'external' && !n.folded_into);
  const hiddenSystem = drawable.filter(n => n.role === 'system' && !opts.showSystem);
  const hiddenJobs = drawable.filter(n => n.role === 'job' && !opts.showSystem);
  const kept = drawable.filter(n => !(n.role === 'system' && !opts.showSystem) && !(n.role === 'job' && !opts.showSystem));
  const externals = topo.nodes.filter(n => n.role === 'external');
  const shown = new Set(kept.map(n => n.id));
  if (externals.length) shown.add(EXTERNAL_ID);
  const resolve = makeResolver(topo, shown);

  // edges between drawn things, merged per (source,target)
  // `last` is the newest Hubble flow time; null when only Envoy (Prometheus, current scrape) observed the pair.
  type Acc = { s: string; d: string; flows: number; ok: number; dropped: number; errors: number; last: number | null; raw: ObservedEdge[]; routed: boolean };
  const acc = new Map<string, Acc>();
  const add = (s: string | null, d: string | null) => {
    if (!s || !d || s === d) return null;
    const k = `${s}>${d}`; let a = acc.get(k);
    if (!a) acc.set(k, a = { s, d, flows: 0, ok: 0, dropped: 0, errors: 0, last: null, raw: [], routed: false });
    return a;
  };
  for (const e of topo.observed_edges) {
    const a = add(resolve(e.from), resolve(e.to));
    if (!a) continue;
    a.flows += e.flows; a.ok += e.ok; a.dropped += e.dropped; a.errors += e.errors; a.raw.push(e);
    if (typeof e.last_seen === 'number' && Number.isFinite(e.last_seen)) a.last = Math.max(a.last ?? e.last_seen, e.last_seen);
  }
  for (const e of topo.structure_edges) if (e.type === 'routes_to') { const a = add(resolve(e.from), resolve(e.to)); if (a) a.routed = true; }
  const edges: MapEdge[] = [...acc.values()].map(a => {
    const observed = a.flows > 0 || a.raw.some(e => e.req_rate != null);
    // A measured HTTP error rate or an API-side degraded state is never reclassified as healthy by zero flow counters.
    const httpErrors = a.raw.some(e => (e.err_rate ?? 0) > 0 || e.state === 'degraded');
    const state = a.dropped && !a.ok ? 'blocked' : a.dropped || a.errors || httpErrors ? 'degraded' : observed ? 'ok' : 'structure';
    // Hubble flow age only applies to edges Hubble saw; an Envoy-only edge comes from the current scrape of this response.
    const flowAge = a.last == null ? null : now - a.last;
    return { id: `${observed ? 'o' : 's'}:${a.s}>${a.d}`, source: a.s, target: a.d, observed, state, flows: a.flows, dropped: a.dropped, errors: a.errors,
      stale: observed && flowAge != null && (topo.freshness.stale || flowAge > topo.freshness.stale_after_s), fresh: observed && (flowAge == null || flowAge <= 10),
      label: a.dropped ? `차단 ${a.dropped}` : a.errors ? `오류 ${a.errors}` : observed ? `${a.flows.toLocaleString()}` : undefined,
      raw: a.raw.find(e => e.req_rate != null) ?? a.raw[0] };
  });

  // ---- layers: entry on the left, data stores on the right, services by call depth in between
  const nodeById = new Map<string, TopoNode>(kept.map(n => [n.id, n]));
  if (externals.length) nodeById.set(EXTERNAL_ID, { id: EXTERNAL_ID, kind: 'External', name: `외부·미확인 ${externals.length}곳`, namespace: null, status: 'observed_only', role: 'external' });
  const incoming = new Map<string, number>(); const out = new Map<string, string[]>();
  for (const e of edges) { incoming.set(e.target, (incoming.get(e.target) ?? 0) + 1); (out.get(e.source) ?? out.set(e.source, []).get(e.source)!).push(e.target); }
  const layer = new Map<string, number>();
  const roots = [...nodeById.values()].filter(n => n.role === 'entry' || n.role === 'external' || n.kind === 'Ingress');
  const queue: string[] = [];
  for (const r of roots) { layer.set(r.id, 0); queue.push(r.id); }
  for (const n of nodeById.values()) if (!layer.has(n.id) && !(incoming.get(n.id) ?? 0) && (out.get(n.id) ?? []).length) { layer.set(n.id, 1); queue.push(n.id); }
  while (queue.length) {
    const id = queue.shift()!; const l = layer.get(id)!;
    for (const to of out.get(id) ?? []) if (!layer.has(to) && to !== id) { layer.set(to, l + 1); queue.push(to); }
  }
  let maxSvc = 1;
  for (const [id, l] of layer) if (nodeById.get(id)?.role !== 'data') maxSvc = Math.max(maxSvc, l);
  const connected = new Set<string>([...layer.keys()]);
  for (const e of edges) { connected.add(e.source); connected.add(e.target); }
  const unconnected = [...nodeById.values()].filter(n => !connected.has(n.id));
  const showUnc = opts.showUnconnected ?? true;
  for (const n of nodeById.values()) {
    if (nodeById.get(n.id)?.role === 'data') layer.set(n.id, maxSvc + 1);
    else if (!layer.has(n.id)) layer.set(n.id, n.role === 'entry' ? 0 : 1);
  }
  // keep a service that has a Ingress/External parent from sitting left of it
  const placed = [...nodeById.values()].filter(n => connected.has(n.id) || showUnc);
  const used = [...new Set(placed.map(n => layer.get(n.id)!))].sort((a, b) => a - b);
  const col = new Map(used.map((l, i) => [l, i]));
  const byCol: MapNode[][] = used.map(() => []);
  for (const n of placed) byCol[col.get(layer.get(n.id)!)!].push({ id: n.id, node: n, alert: alertOf(n), layer: col.get(layer.get(n.id)!)!, x: 0, y: 0 });

  const rank = (n: TopoNode) => [ROLE_ORDER[n.role ?? 'service'] ?? 1, n.namespace ?? '~', n.name] as const;
  const cmp = (a: MapNode, b: MapNode) => { const x = rank(a.node), y = rank(b.node); return x[0] - y[0] || x[1].localeCompare(y[1]) || x[2].localeCompare(y[2]); };
  byCol.forEach(c => c.sort(cmp));
  // two barycenter sweeps to cut line crossings
  const pos = new Map<string, number>();
  const settle = () => byCol.forEach(c => c.forEach((m, i) => pos.set(m.id, i)));
  settle();
  const neighbours = (id: string) => edges.flatMap(e => e.source === id ? [e.target] : e.target === id ? [e.source] : []);
  for (let pass = 0; pass < 2; pass++) {
    for (const c of pass === 0 ? byCol.slice(1) : [...byCol].reverse().slice(1)) {
      const score = new Map(c.map(m => { const ns = neighbours(m.id).filter(x => pos.has(x)); return [m.id, ns.length ? ns.reduce((s, x) => s + pos.get(x)!, 0) / ns.length : pos.get(m.id)!] as const; }));
      c.sort((a, b) => score.get(a.id)! - score.get(b.id)! || cmp(a, b));
      settle();
    }
  }
  const colH = byCol.map(c => c.length * NODE_H + Math.max(0, c.length - 1) * ROW_GAP);
  const height = Math.max(0, ...colH);
  const nodes: MapNode[] = [];
  byCol.forEach((c, ci) => {
    const off = (height - colH[ci]) / 2;
    c.forEach((m, i) => nodes.push({ ...m, x: ci * (NODE_W + COL_GAP), y: off + i * (NODE_H + ROW_GAP), ...(m.id === EXTERNAL_ID ? { members: externals.length } : {}) }));
  });
  const titles = used.map((_l, i) => i === 0 ? '진입' : nodeById.size && byCol[i].every(m => m.node.role === 'data') ? '데이터 저장소' : '서비스');
  const placedIds = new Set(nodes.map(n => n.id));
  const finalEdges = edges.filter(e => placedIds.has(e.source) && placedIds.has(e.target));
  const problems = nodes.filter(n => n.alert !== 'none').map(n => ({ id: n.id, name: n.node.name, alert: n.alert, texts: problemsOf(n.node).map(p => p.text) }))
    .sort((a, b) => (a.alert === 'danger' ? 0 : 1) - (b.alert === 'danger' ? 0 : 1));
  return { nodes, edges: finalEdges, layers: used.map((_l, i) => ({ index: i, x: i * (NODE_W + COL_GAP), title: titles[i], count: byCol[i].length })), problems,
    width: Math.max(0, used.length * (NODE_W + COL_GAP) - COL_GAP), height,
    hidden: { system: hiddenSystem.length, jobs: hiddenJobs.length, unconnected: showUnc ? 0 : unconnected.length } };
}

export const EDGE_COLOR: Record<string, string> = { ok: '#3e8635', degraded: '#d97706', blocked: '#dc2626', structure: '#a3a3a3' };
export function edgeWidth(flows: number): number { return Math.min(4, 1 + Math.log10(Math.max(1, flows)) * 0.9); }

export type ClusterSummary = { nodesReady: number; nodesTotal: number; openIncidents: number; alerts: number; observedFlows: number; freshness: 'live' | 'delayed' | 'never' };
export function summarize(topo: Topology, openIncidents: number): ClusterSummary {
  const nodes = topo.nodes.filter(n => n.kind === 'Node');
  const f = topo.freshness;
  return { nodesReady: nodes.filter(n => n.status === 'ready').length, nodesTotal: nodes.length, openIncidents,
    alerts: topo.nodes.filter(n => alertOf(n) !== 'none' && n.kind !== 'Node').length,
    observedFlows: topo.observed_edges.reduce((a, e) => a + e.flows, 0),
    freshness: f.state === 'never_connected' || f.last_flow_age_s == null ? 'never' : f.stale ? 'delayed' : 'live' };
}

/** Peers of one resource: strongest observed neighbours first, for the side panel and the mini view. */
export function peersOf(topo: Topology, id: string) {
  return topo.observed_edges.filter(e => e.from === id || e.to === id)
    .map(e => ({ peer: e.from === id ? e.to : e.from, direction: e.from === id ? 'out' as const : 'in' as const, edge: e }))
    .sort((a, b) => b.edge.flows - a.edge.flows);
}
