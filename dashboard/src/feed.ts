// Pure alert and feed logic (no React).
import type { Incident } from './api';
import { levelKo } from './labels.ts';

export type AlertEvent = { id: number; kind: 'new' | 'escalated' | 'resolved' | 'closed'; title: string; level: Incident['severity_level']; workload: string; namespace: string };
const OPEN = new Set(['open', 'analyzing', 'action_pending', 'acting', 'handed_off']);
const RANK: Record<string, number> = { SIMPLE: 0, NORMAL: 1, COMPLEX: 2, HIGH_RISK: 3 };

/** Compare two snapshots of incidents. The first snapshot (prev === null) never notifies: that would replay history. */
export function diffAlerts(prev: Incident[] | null, next: Incident[]): AlertEvent[] {
  if (prev === null) return [];
  const before = new Map(prev.map(i => [i.id, i]));
  const out: AlertEvent[] = [];
  for (const i of next) {
    const was = before.get(i.id);
    const base = { id: i.id, title: i.title, level: i.severity_level, workload: i.workload, namespace: i.namespace };
    if (!was) { if (OPEN.has(i.status)) out.push({ ...base, kind: 'new' }); continue; }
    if (OPEN.has(was.status) && !OPEN.has(i.status)) out.push({ ...base, kind: i.status === 'closed' ? 'closed' : 'resolved' });
    else if (OPEN.has(i.status) && (RANK[i.severity_level ?? ''] ?? 0) > (RANK[was.severity_level ?? ''] ?? 0)) out.push({ ...base, kind: 'escalated' });
  }
  return out;
}

export function alertText(a: AlertEvent): string {
  const lv = a.level ? ` · ${levelKo(a.level)}` : '';
  return a.kind === 'new' ? `새 사건: ${a.workload}${lv}` : a.kind === 'escalated' ? `사건 수준 상승: ${a.workload}${lv}` : a.kind === 'closed' ? `운영자 종료: ${a.workload}` : `해결됨: ${a.workload}`;
}

// ---------------------------------------------------------------- run feed
export type RunEvent = { seq: number; kind: string; actor: string; summary: string; detail: Record<string, any>; at: string };
export type FeedItem =
  | { type: 'operator'; seq: number; at: string; text: string; applied: boolean }
  | { type: 'agent'; seq: number; at: string; role: string; summary: string; ok: boolean; model?: string; effort?: string | null; tokens?: string; confidence?: number | null; causes: { cause: string; confidence: number; refs: string[] }[] }
  | { type: 'step'; seq: number; at: string; kind: string; title: string; body?: string; tone: 'info' | 'ok' | 'warn' | 'danger' }
  | { type: 'status'; seq: number; at: string; text: string };

const STEP_TITLE: Record<string, string> = {
  evidence_collected: '근거 수집', routing_decided: '사건 수준 판단', group_composed: 'Agents Group 구성', escalated: '단계 상승',
  action_proposed: '조치 실행', action_executed: '조치 결과', verification_result: '결과 확인', review_recommended: '검토 권장',
  account_switched: 'Codex 계정 전환', waiting_quota: '사용 한도 대기', completed: '종료',
};
const TONE: Record<string, 'info' | 'ok' | 'warn' | 'danger'> = { escalated: 'warn', waiting_quota: 'warn', review_recommended: 'warn' };

export function buildFeed(events: RunEvent[]): FeedItem[] {
  const applied = new Set<number>();
  for (const e of events) if (e.kind === 'operator_applied') for (const id of e.detail.message_ids ?? []) applied.add(id);
  const out: FeedItem[] = [];
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    const d = e.detail ?? {};
    switch (e.kind) {
      case 'operator_message': out.push({ type: 'operator', seq: e.seq, at: e.at, text: e.summary, applied: applied.has(d.message_id) }); break;
      case 'operator_applied': break; // folded into the operator bubble
      case 'agent_started': break;    // the finished card carries the same facts
      case 'agent_finished': out.push({ type: 'agent', seq: e.seq, at: e.at, role: d.role ?? '에이전트', summary: e.summary, ok: d.status !== 'failed',
        model: d.model, effort: d.effort, tokens: d.input_tokens != null ? `${d.input_tokens} / ${d.output_tokens}` : undefined, confidence: d.confidence,
        causes: (d.root_cause_candidates ?? []).map((c: any) => ({ cause: String(c.cause ?? ''), confidence: Number(c.confidence ?? 0), refs: c.evidence_refs ?? [] })) }); break;
      case 'paused': case 'resumed': case 'stopped': case 'note': case 'status': out.push({ type: 'status', seq: e.seq, at: e.at, text: e.summary }); break;
      case 'completed': out.push({ type: 'step', seq: e.seq, at: e.at, kind: e.kind, title: STEP_TITLE.completed, body: e.summary,
        tone: d.outcome === 'resolved' || d.outcome === 'closed' ? 'ok' : d.outcome === 'failed' ? 'danger' : 'warn' }); break;
      case 'action_executed': out.push({ type: 'step', seq: e.seq, at: e.at, kind: e.kind, title: STEP_TITLE[e.kind], body: e.summary, tone: d.status === 'succeeded' ? 'ok' : 'danger' }); break;
      case 'verification_result': out.push({ type: 'step', seq: e.seq, at: e.at, kind: e.kind, title: STEP_TITLE[e.kind], body: e.summary, tone: d.ok ? 'ok' : 'danger' }); break;
      default: out.push({ type: 'step', seq: e.seq, at: e.at, kind: e.kind, title: STEP_TITLE[e.kind] ?? e.kind, body: e.summary, tone: TONE[e.kind] ?? 'info' });
    }
  }
  return out;
}

export const FINISHED = new Set(['resolved', 'handed_off', 'failed', 'closed']);
export function canSteer(status: string): boolean { return !FINISHED.has(status); }
