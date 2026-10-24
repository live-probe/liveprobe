import type { Overview, Workload, WorkloadIncident } from './api.ts';
import { buildMapModel, type TopoNode } from './mapModel.ts';

export const observed = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v);
export const numberKo = (v: number | null | undefined, digits = 0) => observed(v) ? v.toLocaleString('ko-KR', { maximumFractionDigits: digits }) : '미관측';
// API의 오류율은 0~1 비율이며 백분율 변환은 표시 경계에서만 한다.
export const errorRateKo = (v: number | null | undefined) => observed(v) ? `${numberKo(v * 100, 1)}%` : '미관측';
export const cpuKo = (v: number | null | undefined) => observed(v) ? `${numberKo(v, 2)} 코어` : '미관측';
export function bytesKo(v: number | null | undefined) {
  if (!observed(v)) return '미관측';
  return v >= 1024 ** 3 ? `${numberKo(v / 1024 ** 3, 1)} GiB` : `${numberKo(v / 1024 ** 2, 1)} MiB`;
}
/** Any missing member makes the aggregate unobserved; partial sums must not look like complete capacity. */
export function sumObserved(values: (number | null | undefined)[]): number | null {
  return values.length && values.every(observed) ? values.reduce<number>((a, b) => a + b!, 0) : null;
}
export const percent = (used: number | null, capacity: number | null) => observed(used) && observed(capacity) && capacity > 0 ? Math.max(0, Math.min(100, used / capacity * 100)) : null;
export function overviewKpis(o: Overview) {
  const nodes = o.cluster?.nodes ?? [], ns = o.cluster?.namespaces ?? [];
  return {
    nodesTotal: nodes.length || null, nodesReady: nodes.length && nodes.every(n => n.ready != null) ? nodes.filter(n => n.ready === true).length : null,
    cpuUsed: sumObserved(nodes.map(n => n.cpu_used_cores)), cpuCapacity: sumObserved(nodes.map(n => n.cpu_cores)),
    memUsed: sumObserved(nodes.map(n => n.mem_used_bytes)), memCapacity: sumObserved(nodes.map(n => n.mem_bytes)),
    running: sumObserved(ns.map(n => n.pods_running)), pending: sumObserved(ns.map(n => n.pods_pending)), failed: sumObserved(ns.map(n => n.pods_failed)),
    restarts: sumObserved(ns.filter(n => (n.workloads ?? 1) > 0).map(n => n.restarts_1h)),
    levels: Object.fromEntries(['SIMPLE', 'NORMAL', 'COMPLEX', 'HIGH_RISK'].map(l => [l, (o.incidents?.open ?? []).filter(i => i.severity_level === l).length])),
  };
}
export function groupWorkloads(workloads: Workload[]): { namespace: string; items: Workload[] }[] {
  const groups = new Map<string, Workload[]>();
  for (const w of workloads) groups.set(w.namespace, [...(groups.get(w.namespace) ?? []), w]);
  return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([namespace, items]) => ({ namespace, items: items.sort((a, b) => a.name.localeCompare(b.name)) }));
}
export function incidentFor(o: Overview, node: Pick<TopoNode, 'id' | 'namespace' | 'name'>): WorkloadIncident | null {
  const w = (o.workloads ?? []).find(w => w.id === node.id);
  if (w?.incident) return w.incident;
  const i = (o.incidents?.open ?? []).find(i => i.namespace === node.namespace && i.workload === node.name);
  return i ? { id: i.id, level: i.severity_level, status: i.status, stage: i.stage } : null;
}
export const resourceHealth = (n: TopoNode, w?: Workload) => {
  if (w) return !observed(w.ready) || !observed(w.desired) ? '미관측' : w.desired === 0 ? '중지됨' : w.ready >= w.desired ? '준비됨' : '준비 부족';
  return ({ ready: '준비됨', degraded: '일부 비정상', down: '중단', completed: '완료', no_endpoints: '엔드포인트 없음', unknown: '미관측', observed_only: '흐름만 관측' } as Record<string, string>)[n.status] ?? '미관측';
};
export const MAP_W = 220, MAP_H = 124;
// 열 간격은 연결선 문구(초당 요청·오류율)가 자원 카드에 가려지지 않는 폭이다.
const COL_GAP = 136, ROW_GAP = 20, PAD = 16, HEAD = 40, GROUP_GAP = 32, MAP_ASPECT = 1.25;
/** Cluster plumbing (control plane, mesh, observability stack). Hidden by default so the business services stay readable. */
const PLATFORM_NS = new Set(['kube-system', 'kube-public', 'kube-node-lease', 'liveprobe-observability', 'istio-system']);
export const isPlatform = (n: Pick<TopoNode, 'role' | 'namespace' | 'name'>) =>
  n.role === 'system' || n.role === 'job' || PLATFORM_NS.has(n.namespace ?? '') || (n.namespace === 'default' && n.name === 'kubernetes');
type MapGroup = { id: string; name: string; x: number; y: number; width: number; height: number };
type MapResource = { id: string; x: number; y: number; node: TopoNode; workload?: Workload; incident: WorkloadIncident | null; groupId: string; alert: string };
/** Shelf-pack namespace boxes (tallest first) and keep the arrangement whose bounding box best fits a wide canvas. */
function packGroups(groups: MapGroup[]): { width: number; height: number } {
  if (!groups.length) return { width: 0, height: 0 };
  const order = [...groups].sort((a, b) => b.height - a.height || b.width - a.width || a.name.localeCompare(b.name));
  const widest = Math.max(...order.map(g => g.width));
  const candidates = new Set<number>([widest]); let run = 0;
  for (const g of [...order].sort((a, b) => b.width - a.width)) { run += (run ? GROUP_GAP : 0) + g.width; if (run >= widest) candidates.add(run); }
  let best: { score: number; width: number; height: number; at: Map<string, { x: number; y: number }> } | null = null;
  for (const limit of candidates) {
    const shelves: { y: number; height: number; used: number }[] = []; const at = new Map<string, { x: number; y: number }>();
    for (const g of order) {
      let s = shelves.find(s => s.used + GROUP_GAP + g.width <= limit && g.height <= s.height);
      if (!s) { const y = shelves.length ? shelves[shelves.length - 1].y + shelves[shelves.length - 1].height + GROUP_GAP : 0; s = { y, height: g.height, used: -GROUP_GAP }; shelves.push(s); }
      at.set(g.id, { x: s.used + GROUP_GAP, y: s.y }); s.used += GROUP_GAP + g.width;
    }
    const width = Math.max(...shelves.map(s => s.used)), height = shelves[shelves.length - 1].y + shelves[shelves.length - 1].height;
    const score = Math.min(MAP_ASPECT / width, 1 / height);
    if (!best || score > best.score) best = { score, width, height, at };
  }
  for (const g of groups) Object.assign(g, best!.at.get(g.id));
  return { width: best!.width, height: best!.height };
}
export function overviewMap(o: Overview, namespace = '', opts: { showPlatform?: boolean } = {}) {
  if (!o.topology) return { nodes: [] as MapResource[], edges: [], groups: [] as MapGroup[], hiddenPlatform: 0, width: 0, height: 0 };
  const base = buildMapModel(o.topology, { showSystem: true });
  const inNs = base.nodes.filter(n => !namespace || n.node.namespace === namespace);
  // A platform component with a problem or an open incident is never hidden: failure location wins over tidiness.
  const visible = (n: typeof base.nodes[number]) => !!namespace || opts.showPlatform || !isPlatform(n.node) || n.alert !== 'none' || !!incidentFor(o, n.node);
  const kept = inNs.filter(visible);
  const names = [...new Set(kept.map(n => n.node.namespace ?? '클러스터 외부'))].sort((a, b) => a.localeCompare(b));
  const nodes: MapResource[] = [];
  const groups: MapGroup[] = [];
  for (const name of names) {
    const items = kept.filter(n => (n.node.namespace ?? '클러스터 외부') === name);
    const cols = [...new Set(items.map(n => n.layer))].sort((a, b) => a - b), col = new Map(cols.map((l, i) => [l, i]));
    const rows = new Map<number, number>();
    const groupId = `namespace:${name}`;
    for (const n of items) {
      const row = rows.get(n.layer) ?? 0; rows.set(n.layer, row + 1);
      // Keep all Service identity/port/endpoint fields even if the source forgot to pre-enrich a folded workload.
      const services = [...(n.node.services ?? [])];
      for (const s of o.topology.nodes.filter(s => s.folded_into === n.id)) if (!services.some(x => x.name === s.name)) services.push({ name: s.name, ports: s.ports ?? [], endpoints_ready: s.endpoints_ready ?? null });
      nodes.push({ id: n.id, x: PAD + col.get(n.layer)! * (MAP_W + COL_GAP), y: HEAD + row * (MAP_H + ROW_GAP), node: { ...n.node, services }, workload: o.workloads?.find(w => w.id === n.id), incident: incidentFor(o, n.node), groupId, alert: n.alert });
    }
    const maxRows = Math.max(1, ...rows.values());
    groups.push({ id: groupId, name, x: 0, y: 0, width: PAD * 2 + cols.length * (MAP_W + COL_GAP) - COL_GAP, height: HEAD + maxRows * (MAP_H + ROW_GAP) - ROW_GAP + PAD });
  }
  const size = packGroups(groups);
  const ids = new Set(nodes.map(n => n.id));
  return { nodes, groups, edges: base.edges.filter(e => ids.has(e.source) && ids.has(e.target)), hiddenPlatform: inNs.length - kept.length, ...size };
}
type Placed = { x: number; y: number; group: string };
/** A label sits at the edge midpoint. It is readable only in the empty gap between neighbouring columns or rows of one
 * namespace; a skipped resource, a namespace crossing or a reverse edge would put it on a card or on another label. */
export function edgeLabelFits(a: Placed, b: Placed, reverse: boolean): boolean {
  if (reverse || a.group !== b.group) return false;
  const dx = Math.abs(a.x - b.x), dy = Math.abs(a.y - b.y);
  return (dy === 0 && dx === MAP_W + COL_GAP) || (dx === 0 && dy === MAP_H + ROW_GAP);
}
/** Edge numbers from the source: request rate and HTTP error rate (Envoy), flow count, then blocked/error flow counts (Hubble). */
export function edgeText(e: { raw?: { req_rate?: number | null; err_rate?: number | null } | null; observed: boolean; flows: number; dropped?: number; errors?: number }, { compact = false } = {}) {
  if (!e.observed) return '트래픽 미관측';
  const hasRate = e.raw?.req_rate != null;
  const parts = hasRate ? [`${numberKo(e.raw!.req_rate, 1)}/초`, `오류 ${errorRateKo(e.raw!.err_rate)}`] : [`흐름 ${numberKo(e.flows)}`];
  if (hasRate && e.flows > 0 && !compact) parts.push(`흐름 ${numberKo(e.flows)}`);
  if (e.dropped) parts.push(`차단 ${numberKo(e.dropped)}`);
  if (e.errors) parts.push(`오류 ${numberKo(e.errors)}`);
  return parts.join(' · ');
}
/** Every connection of one resource with its source numbers, so a label hidden on the map is still readable in the detail. */
export function connectionsOf(m: ReturnType<typeof overviewMap>, id: string) {
  const name = (x: string) => m.nodes.find(n => n.id === x)?.node.name ?? x;
  return m.edges.filter(e => e.source === id || e.target === id).map(e => ({ id: e.id, direction: e.source === id ? 'out' as const : 'in' as const, peer: name(e.source === id ? e.target : e.source), text: edgeText(e), state: e.state }));
}
/** Attach edges only to facing sides, including same-column and reverse edges. */
export function handlesFor(a: { x: number; y: number }, b: { x: number; y: number }) {
  return a.x < b.x ? { sourceHandle: 'sr', targetHandle: 'tl' } : a.x > b.x ? { sourceHandle: 'sl', targetHandle: 'tr' } : a.y <= b.y ? { sourceHandle: 'sb', targetHandle: 'tt' } : { sourceHandle: 'st', targetHandle: 'tb' };
}
