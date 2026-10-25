import type { Topology } from './mapModel';

export type Status = 'open' | 'analyzing' | 'action_pending' | 'acting' | 'resolved' | 'handed_off' | 'failed' | 'closed';
export type Level = 'SIMPLE' | 'NORMAL' | 'COMPLEX' | 'HIGH_RISK';
export type Citation = { chunk_id: number; doc: string; source: string; chunk_ix: number; score: number; content: string; source_uri?: string | null; locator?: string | null };
export type Incident = { id: number; title: string; namespace: string; system?: string | null; environment?: string | null; workload: string; status: Status; severity_level: Level | null; signal_count: number; created_at: string; updated_at: string; closed_at: string | null; total_cost_usd: number | null; agent_run_count: number; summary?: string | null; status_reason?: string | null; signal_reasons?: string[]; handoff?: Record<string, unknown> | null; family_hint?: string | null; stage?: Stage | null };

// ---------------------------------------------------------------- P15 incident war room (GET /incidents/{id}/narrative)
export type Stage = { key: string; label: string };
export type RawItem = {
  id: string; ref: string; kind: 'event' | 'log' | 'status' | 'metric' | 'config' | 'node' | 'agent' | string;
  source: string; at: string | null; raw: string; severity: 'info' | 'warn' | 'error' | string;
  ko: string | null; ko_source: 'rules' | 'llm' | null; cited_by: string[];
};
export type FlowNodeType = 'signal' | 'evidence' | 'router' | 'group' | 'agent' | 'verify' | 'action' | 'outcome' | 'steer' | 'command' | 'interpret';
export type FlowNodeStatus = 'planned' | 'running' | 'done' | 'failed' | 'skipped' | 'escalated';
export type FlowNode = {
  id: string; type: FlowNodeType | string; label: string; role: string | null; role_ko: string; job_ko: string;
  status: FlowNodeStatus | string; model: string | null; effort: string | null; tokens: { in: number | null; out: number | null } | null;
  cost_usd: number | null; duration_ms: number | null; inputs: string[]; conclusion_ko: string | null; conclusion_raw: string | null;
  lane: number; depth: number; group: Level | null;
};
export type FlowEdge = { id: string; source: string; target: string; kind: 'flow' | 'escalate' | 'reroute' | 'steer' | 'verify' | string; label: string | null };
export type Narrative = {
  incident: Incident;
  headline: { what: string; impact: string; stage: Stage; next: string; source: string };
  raw: RawItem[];
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
  interpreting: boolean;
};

// ---------------------------------------------------------------- P15 operator commands
export type CommandStatus = 'planning' | 'awaiting_confirmation' | 'awaiting_secret' | 'executing' | 'succeeded' | 'failed' | 'cancelled' | 'rejected' | 'rolling_back' | 'rolled_back';
export type Operation = { op: string; apiVersion?: string; kind: string; namespace?: string | null; name: string; manifest?: unknown; patch?: unknown; replicas?: number; keys?: string[] };
export type DryRun = { op_index: number; ok: boolean; diff: { path: string; before: unknown; after: unknown }[] | null; error: string | null };
export type SideEffects = { items_ko: string[]; targets: string[]; restarts: boolean; downtime: string; reversible: boolean; rollback_ko: string; cluster_wide: boolean; warnings_ko: string[] };
export type Command = {
  id: number; incident_id: number; instruction: string; status: CommandStatus | string;
  plan: { summary_ko: string; operations: Operation[] } | null; plan_sha256: string | null; dry_run: DryRun[] | null;
  side_effects: SideEffects | null; confirm_question: string; result: unknown; verification: unknown; rollback: unknown;
  error: string | null; created_at: string; updated_at: string;
};

// ---------------------------------------------------------------- P15 operations overview (GET /overview)
export type ClusterNode = { name: string; ready: boolean | null; cpu_cores: number | null; cpu_used_cores: number | null; mem_bytes: number | null; mem_used_bytes: number | null; pods: number | null };
export type EnvironmentStat = { name: string; order: number; workloads: number | null; pods_ready: number | null; pods_total: number | null; open_incidents: number | null; missing?: true };
export type NamespaceStat = { name: string; pods_running: number | null; pods_pending: number | null; pods_failed: number | null; restarts_1h: number | null; cpu_cores: number | null; mem_bytes: number | null; workloads: number | null; workloads_unhealthy: number | null };
export type WorkloadIncident = { id: number; level: Level | string | null; status: string; stage: Stage | null };
export type Workload = { id: string; namespace: string; system?: string | null; environment?: string | null; name: string; kind: string; ready: number | null; desired: number | null; restarts_1h: number | null; cpu: number | null; mem: number | null; req_rate: number | null; err_rate: number | null; p95_ms: number | null; incident: WorkloadIncident | null };
export type ClusterEvent = { at: string | null; namespace: string | null; object: string; reason: string; type: string; message: string; count: number | null };
export type OpenIncident = { id: number; title: string; namespace: string; system?: string | null; environment?: string | null; workload: string; severity_level: Level | string | null; status: string; stage: Stage | null; created_at: string };
export type Overview = {
  generated_at: string | null;
  cluster: { nodes: ClusterNode[]; namespaces: NamespaceStat[]; environments?: EnvironmentStat[] };
  workloads: Workload[];
  events: ClusterEvent[];
  incidents: { open: OpenIncident[]; today: { total: number | null; resolved: number | null; handed_off: number | null; failed: number | null; closed?: number | null } };
  topology: Topology | null;
  sources: Record<string, string>;
};

export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin', headers: { 'X-LiveProbe-Client': 'dashboard', ...(options?.body ? { 'Content-Type': 'application/json' } : {}), ...options?.headers } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
// Existing unattended-engine actions (GET /incidents/{id}); approval goes through POST /actions/{id}/approve|reject.
export type Action = { id: number; type: string; target: string; params: unknown; policy_mode: string; status: string; reason: string | null; executed: boolean; created_at: string | null; updated_at: string | null };
export type IncidentDetail = { incident: Incident; actions: Action[] };
export const money = (value: number | null | undefined) => value == null ? '추정 불가' : `$${value.toFixed(4)}`;
export const dateTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString('ko-KR') : '시각 미기록';
export const clock = (value: string | null | undefined) => value ? new Date(value).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '시각 미기록';
/** Elapsed time in Korean; `until` freezes it for finished incidents. */
export const age = (value: string, until?: string | null, now: number = Date.now()) => {
  const end = until ? new Date(until).getTime() : now;
  const mins = Math.max(0, Math.floor((end - new Date(value).getTime()) / 60000));
  return mins < 60 ? `${mins}분` : mins < 1440 ? `${Math.floor(mins / 60)}시간 ${mins % 60}분` : `${Math.floor(mins / 1440)}일`;
};
export const display = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
