import type { EnvironmentStat, Overview, Workload, WorkloadIncident } from './api.ts';
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
export function environmentOptions(o: Overview) {
  return [...(o.cluster?.environments ?? [])].sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}
export function environmentSummary(e: EnvironmentStat): string {
  if (e.missing) return '미관측';
  return `파드 ${numberKo(e.pods_ready)}/${numberKo(e.pods_total)} · 진행 중 장애 ${numberKo(e.open_incidents)}${observed(e.open_incidents) ? '건' : ''}`;
}
export function defaultMapScope(o: Overview): string {
  const envs = environmentOptions(o);
  return envs.some(e => e.name === 'prd') ? 'prd' : envs[0]?.name ?? '';
}
export const MAP_W = 220, MAP_H = 124;
// 열 간격은 연결선 문구(초당 요청·오류율)가 자원 카드에 가려지지 않는 폭이다.
const COL_GAP = 136, ROW_GAP = 20, PAD = 16, HEAD = 40, GROUP_GAP = 32, MAP_ASPECT = 1.25;
/** Cluster plumbing (control plane, mesh, observability stack). Hidden by default so the business services stay readable. */
const PLATFORM_NS = new Set(['liveprobe-system', 'kube-system', 'kube-public', 'kube-node-lease', 'liveprobe-observability', 'istio-system']);
export const isPlatform = (n: Pick<TopoNode, 'role' | 'namespace' | 'name'>) =>
  n.role === 'system' || n.role === 'job' || PLATFORM_NS.has(n.namespace ?? '') || (n.namespace === 'default' && n.name === 'kubernetes');
type MapGroup = { id: string; name: string; kind?: 'system' | 'environment' | 'namespace'; x: number; y: number; width: number; height: number };
type MapResource = { id: string; x: number; y: number; node: TopoNode; workload?: Workload; incident: WorkloadIncident | null; groupId: string; alert: string };
/** Shelf-pack namespace boxes (tallest first) and keep the arrangement whose bounding box best fits a wide canvas. */
function packGroups(groups: MapGroup[], preserveOrder = false): { width: number; height: number } {
  if (!groups.length) return { width: 0, height: 0 };
  const order = preserveOrder ? [...groups] : [...groups].sort((a, b) => b.height - a.height || b.width - a.width || a.name.localeCompare(b.name));
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
  const envs = environmentOptions(o), envMode = envs.length > 0;
  const envNames = new Set(envs.map(e => e.name));
  const base = buildMapModel(o.topology, { showSystem: true });
  const resources = base.nodes.map(n => {
    const w = o.workloads?.find(w => w.id === n.id);
    return { ...n, node: { ...n.node, system: n.node.system ?? w?.system, environment: n.node.environment ?? w?.environment } };
  });
  // Environment filtering retains platform warnings and the explicit platform toggle, but excludes retired target namespaces.
  const inNs = resources.filter(n => envMode
    ? (envNames.has(n.node.environment ?? n.node.namespace ?? '') && (!namespace || (n.node.environment ?? n.node.namespace) === namespace)) || isPlatform(n.node)
    : !namespace || n.node.namespace === namespace);
  const visible = (n: typeof base.nodes[number]) => (!envMode && !!namespace) || opts.showPlatform || !isPlatform(n.node) || n.alert !== 'none' || !!incidentFor(o, n.node);
  const kept = inNs.filter(visible);
  const groupOf = (n: typeof kept[number]) => envMode && namespace && !isPlatform(n.node)
    ? { id: `${namespace}/system/${n.node.system ?? '__unassigned__'}`, name: n.node.system || '시스템 미지정', kind: 'system' as const }
    : { id: `namespace:${n.node.namespace ?? '외부'}`, name: n.node.namespace ?? '클러스터 외부', kind: envMode && !isPlatform(n.node) ? 'environment' as const : 'namespace' as const };
  const buckets = new Map<string, { group: ReturnType<typeof groupOf>; items: typeof kept }>();
  for (const n of kept) {
    const group = groupOf(n);
    if (!buckets.has(group.id)) buckets.set(group.id, { group, items: [] });
    buckets.get(group.id)!.items.push(n);
  }
  const rank = (items: typeof kept) => items.some(n => incidentFor(o, n.node)) ? 0 : 1;
  const ordered = [...buckets.values()].sort((a, b) => {
    if (!envMode) return a.group.name.localeCompare(b.group.name);
    if (a.group.kind !== b.group.kind) return a.group.kind === 'namespace' ? 1 : -1;
    return namespace ? rank(a.items) - rank(b.items) || a.group.name.localeCompare(b.group.name)
      : (envs.findIndex(e => e.name === a.group.name) - envs.findIndex(e => e.name === b.group.name)) || a.group.name.localeCompare(b.group.name);
  });
  const nodes: MapResource[] = [], groups: MapGroup[] = [];
  for (const { group, items } of ordered) {
    const compact = envMode && group.kind !== 'namespace';
    if (compact) items.sort((a, b) => (a.node.system ?? '').localeCompare(b.node.system ?? '') || a.layer - b.layer || a.node.name.localeCompare(b.node.name));
    const cols = [...new Set(items.map(n => n.layer))].sort((a, b) => a - b), col = new Map(cols.map((l, i) => [l, i]));
    const rows = new Map<number, number>();
    const compactCols = group.kind === 'system' ? 1 : 2;
    for (const [index, n] of items.entries()) {
      const column = compact ? index % compactCols : col.get(n.layer)!;
      const row = compact ? Math.floor(index / compactCols) : rows.get(n.layer) ?? 0;
      rows.set(compact ? column : n.layer, row + 1);
      const services = [...(n.node.services ?? [])];
      for (const s of o.topology.nodes.filter(s => s.folded_into === n.id)) if (!services.some(x => x.name === s.name)) services.push({ name: s.name, ports: s.ports ?? [], endpoints_ready: s.endpoints_ready ?? null });
      nodes.push({ id: n.id, x: PAD + column * (MAP_W + COL_GAP), y: HEAD + row * (MAP_H + ROW_GAP), node: { ...n.node, services }, workload: o.workloads?.find(w => w.id === n.id), incident: incidentFor(o, n.node), groupId: group.id, alert: n.alert });
    }
    const maxRows = Math.max(1, ...rows.values()), columns = compact ? Math.min(items.length, compactCols) : cols.length;
    groups.push({ ...group, x: 0, y: 0, width: PAD * 2 + columns * (MAP_W + COL_GAP) - COL_GAP, height: HEAD + maxRows * (MAP_H + ROW_GAP) - ROW_GAP + PAD });
  }
  const size = packGroups(groups, envMode);
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
