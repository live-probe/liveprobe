import type { Action, FlowEdge, FlowNode, Incident, Narrative, RawItem } from './api.ts';
import { roleKo } from './labels.ts';

export const RAW_FILTERS = [['all', '전체'], ['error', '오류'], ['event', '이벤트'], ['log', '로그'], ['status', '상태'], ['agent', '에이전트']] as const;
export type RawFilter = typeof RAW_FILTERS[number][0];
export const FLOW_STATUS_KO: Record<string, string> = { planned: '예정', running: '실행 중', done: '완료', failed: '실패', skipped: '건너뜀', escalated: '상위 모델 승격' };
/** Short status for the two-line node; the full wording stays in the accessible name and the step detail. */
export const FLOW_STATUS_SHORT: Record<string, string> = { planned: '예정', running: '실행 중', done: '완료', failed: '실패', skipped: '건너뜀', escalated: '승격됨' };
export const FLOW_TYPE_KO: Record<string, string> = { signal: '신호', evidence: '근거', router: '분류', group: '그룹', agent: '에이전트', verify: '검증', action: '조치', outcome: '결과', steer: '운영자 지시', command: '지시 실행', interpret: '한글 해석' };
export const EDGE_KIND_KO: Record<string, string> = { flow: '진행', escalate: '상위 모델 승격', reroute: '경로 변경', steer: '지시 반영', verify: '검증' };
export const STAGE_KO: Record<string, string> = { routing: '사건 수준 판단 중', collecting: '근거 수집 중', analyzing: '에이전트 분석 중', verifying: '결과 확인 중', acting: '조치 실행 중', approval: '운영자 승인 대기', resolved: '자동 복구 완료', handed_off: '운영자 인계', failed: '처리 실패' };
export const flowStatusKo = (v: string) => FLOW_STATUS_KO[v] ?? '상태 미확인';
export const stageKo = (key: string, label?: string | null) => label || STAGE_KO[key] || '단계 미확인';

export function filterRaw(items: RawItem[], filter: RawFilter): RawItem[] {
  if (filter === 'all') return items;
  if (filter === 'error') return items.filter(it => it.severity === 'error' || it.severity === 'warn');
  if (filter === 'status') return items.filter(it => ['status', 'metric', 'config', 'node'].includes(it.kind));
  return items.filter(it => it.kind === filter);
}

/** An input may refer to the entire source or a specific row, never a prefix lookalike (E1 != E10). */
export function matchesRef(item: Pick<RawItem, 'id' | 'ref'>, ref: string): boolean {
  return ref === item.id || ref === item.ref;
}
export function citedByNode(item: RawItem, node: FlowNode | null | undefined): boolean {
  if (!node) return false;
  return (node.inputs ?? []).some(ref => matchesRef(item, ref)) || (!!node.role && (item.cited_by ?? []).includes(node.role));
}
export const rawForRef = (items: RawItem[], ref: string) => items.filter(it => matchesRef(it, ref));

/** One Korean reading for many identical-meaning raw lines. Raw strings and their order are kept untouched. */
export type RawGroup = { key: string; kind: string; source: string; severity: string; ko: string | null; ko_source: RawItem['ko_source']; items: RawItem[]; cited_by: string[]; first_at: string | null; last_at: string | null };
const SEVERITY_RANK: Record<string, number> = { error: 2, warn: 1, info: 0 };
export function groupRaw(items: RawItem[]): RawGroup[] {
  const out: RawGroup[] = []; const by = new Map<string, RawGroup>();
  for (const it of items) {
    // Without a reading there is nothing to merge on: every unread line stays its own group.
    const k = it.ko ? `${it.kind}\u0000${it.source}\u0000${it.ko}` : `raw\u0000${it.id}`;
    let g = by.get(k);
    if (!g) { g = { key: k, kind: it.kind, source: it.source, severity: it.severity, ko: it.ko, ko_source: it.ko_source, items: [], cited_by: [], first_at: it.at, last_at: it.at }; by.set(k, g); out.push(g); }
    g.items.push(it);
    if ((SEVERITY_RANK[it.severity] ?? 0) > (SEVERITY_RANK[g.severity] ?? 0)) g.severity = it.severity;
    for (const r of it.cited_by ?? []) if (!g.cited_by.includes(r)) g.cited_by.push(r);
    if (it.at && (!g.first_at || it.at < g.first_at)) g.first_at = it.at;
    if (it.at && (!g.last_at || it.at > g.last_at)) g.last_at = it.at;
  }
  return out;
}
/** Evidence pane groups. With a step selected, rows are filtered before grouping so a shared Korean reading never pulls in raw lines the step did not cite. */
export function evidenceGroups(items: RawItem[], node: FlowNode | null, filter: RawFilter): RawGroup[] {
  return groupRaw(node ? items.filter(it => citedByNode(it, node)) : filterRaw(items, filter));
}

/** Preserve the API datetime's sub-millisecond precision while comparing timezone-normalized milliseconds. */
function incidentTime(value: string) {
  const ms = Date.parse(value);
  const fraction = /\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? '';
  return { ms, subMs: fraction.slice(3).padEnd(6, '0') };
}
/** Operational state follows the newer incident snapshot, not a stale interpretation. Detail wins ties and retains ownership fields when the status agrees. */
export function currentIncident(narrative: Incident, detail?: Incident | null): Incident {
  if (!detail) return narrative;
  const n = incidentTime(narrative.updated_at), d = incidentTime(detail.updated_at);
  const newer = n.ms > d.ms || (n.ms === d.ms && n.subMs > d.subMs);
  if (Number.isFinite(n.ms) && (!Number.isFinite(d.ms) || newer)) return { ...detail, ...narrative,
    handoff: narrative.handoff ?? (narrative.status === detail.status ? detail.handoff : null),
    status_reason: narrative.status_reason ?? (narrative.status === detail.status ? detail.status_reason : null) };
  return { ...narrative, ...detail };
}

/** Engine actions still waiting for the operator, in proposal order. */
export const pendingActions = (actions: Action[] | null | undefined) => (actions ?? []).filter(a => a.status === 'pending_approval').sort((a, b) => a.id - b.id);

/** The requested values only (`args`, or flat scalar params); the full params incl. snapshots stay in the raw view. */
export function actionChanges(params: unknown): string[] {
  if (!params || typeof params !== 'object') return [];
  const p = params as Record<string, unknown>;
  const src = p.args && typeof p.args === 'object' ? p.args as Record<string, unknown> : p;
  return Object.entries(src).filter(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v)).map(([k, v]) => `${k}=${String(v)}`).slice(0, 6);
}

export const FLOW_W = 216;
export const FLOW_H = 56;
export const FLOW_X_GAP = 40;
export const FLOW_Y_GAP = 24;
/** Vertical flow: depth runs top to bottom and lane left to right, keeping negative lanes for steering. */
export function layoutFlow(nodes: FlowNode[]): { id: string; x: number; y: number }[] {
  const finite = (n: number) => Number.isFinite(n) ? n : 0;
  const minLane = Math.min(0, ...nodes.map(n => finite(n.lane)));
  return nodes.map(n => ({ id: n.id, x: (finite(n.lane) - minLane) * (FLOW_W + FLOW_X_GAP), y: finite(n.depth) * (FLOW_H + FLOW_Y_GAP) }));
}
/** Vertical flows connect bottom to top; same-row links use the facing left/right sides. */
export function flowHandles(a: { x: number; y: number }, b: { x: number; y: number }) {
  return a.y < b.y ? { sourceHandle: 'sb', targetHandle: 'tt' } : a.y > b.y ? { sourceHandle: 'st', targetHandle: 'tb' } : a.x <= b.x ? { sourceHandle: 'sr', targetHandle: 'tl' } : { sourceHandle: 'sl', targetHandle: 'tr' };
}
/** Two-line node text. The full label stays in the accessible name and in the step detail. */
export function flowTitle(n: Pick<FlowNode, 'role' | 'role_ko' | 'label' | 'type'>): string {
  if (n.role) return roleKo(n.role, n.role_ko);
  const head = (n.label ?? '').split(': ')[0].trim();
  return head || FLOW_TYPE_KO[n.type] || '단계';
}
export function flowSub(n: Pick<FlowNode, 'role' | 'label' | 'type' | 'model'>): string {
  const kind = FLOW_TYPE_KO[n.type] ?? '단계';
  const label = n.label ?? '';
  const rest = n.role ? (n.model || '모델 미기록') : label.includes(': ') ? label.slice(label.indexOf(': ') + 2) : '';
  return rest ? `${kind} · ${rest}` : kind;
}
export function validFlowEdges(nodes: FlowNode[], edges: FlowEdge[]): FlowEdge[] {
  const ids = new Set(nodes.map(n => n.id));
  return edges.filter(e => ids.has(e.source) && ids.has(e.target));
}
export const narrativePollMs = (status: string) => ['resolved', 'handed_off', 'failed'].includes(status) ? 10000 : 2000;
export const isFinished = (status: string) => ['resolved', 'handed_off', 'failed'].includes(status);
/** Missing collection arrays are empty, not a crash and never a fabricated healthy result. */
export function normalizeNarrative(data: Narrative): Narrative {
  return { ...data, raw: data.raw ?? [], flow: { nodes: data.flow?.nodes ?? [], edges: data.flow?.edges ?? [] } };
}
