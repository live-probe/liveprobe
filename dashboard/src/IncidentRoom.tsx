import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError, age, clock, money, request, type Command, type FlowNode, type IncidentDetail, type Narrative } from './api';
import type { RunEvent } from './feed';
import { currentIncident, evidenceGroups, flowStatusKo, flowTitle, FLOW_TYPE_KO, isFinished, pendingActions, matchesRef, narrativePollMs, normalizeNarrative, RAW_FILTERS, type RawFilter, type RawGroup } from './narrative';
import { commandPollMs } from './commands';
import { numberKo } from './overviewModel';
import { ACTION_KO, INTENT_KO, KIND_KO, SEVERITY_KO, STAGE_FROM_STATUS, effortKo, roleKo, sourceKo } from './labels';
import { usePolling } from './usePolling';
import { CommandCard } from './CommandCard';
import { ActionApprovalCard, HandoffCard } from './ActionApproval';
import { FlowGraph } from './FlowGraph';
import { ErrorBox, LevelBadge, Spinner, StatusBadge } from './ui';

type Tab = 'flow' | 'evidence' | 'chat';
const EVENT_KO: Record<string, string> = { evidence_collected: '근거 수집 완료', agent_started: '분석 시작', agent_finished: '분석 완료', routing_decided: '장애 수준 판단', group_composed: '분석 그룹 구성', escalated: '상위 모델 승격', action_proposed: '조치 계획', action_executed: '조치 실행 결과', verification_result: '복구 검증', completed: '분석 종료', status: '실행 상태', note: '실행 메모', paused: '일시정지', resumed: '분석 재개', stopped: '분석 중단', command_planned: '지시 계획 수립', command_confirmed: '실행 확인 접수', command_executed: '지시 실행 완료', command_failed: '지시 실행 실패', command_rolled_back: '지시 원복 완료' };
function eventKo(e: RunEvent) {
  const d = e.detail ?? {};
  if (e.kind === 'agent_started' || e.kind === 'agent_finished') return `${roleKo(d.role)} ${e.kind === 'agent_started' ? '시작' : d.status === 'failed' ? '실패' : '완료'}`;
  if (e.kind === 'completed') return STAGE_FROM_STATUS[d.outcome]?.label ?? '분석이 끝났습니다.';
  if (e.kind === 'action_proposed' && d.type) return `${ACTION_KO[d.type] ?? '복구 조치'} 계획을 검토합니다.`;
  return EVENT_KO[e.kind] ?? '실행 기록이 추가되었습니다.';
}
const groupHas = (g: RawGroup, ref: string) => g.items.some(it => matchesRef(it, ref));
/** Korean reading first; the verbatim English lines are one click away and never rewritten. */
function EvidenceGroup({ g, open, onToggle, highlighted, name, chooseRole, interpreting }: { g: RawGroup; open: boolean; onToggle: (open: boolean) => void; highlighted: boolean; name: (r: string) => string; chooseRole: (r: string) => void; interpreting: boolean }) {
  const span = g.first_at && g.last_at && g.first_at !== g.last_at ? `${clock(g.first_at)}–${clock(g.last_at)}` : clock(g.first_at);
  return <article id={`evidence-${g.items[0].id}`} className={`evidence-group sev-${g.severity} ${highlighted ? 'highlighted' : ''}`} data-raw-ids={g.items.map(i => i.id).join(' ')}>
    <div className="raw-meta"><span className={`severity sev-${g.severity}`}>{SEVERITY_KO[g.severity] ?? '정보'}</span><span className="source-tag">{sourceKo(g.source, name)} · {KIND_KO[g.kind] ?? '기록'}</span>{g.items.length > 1 && <span className="badge">{g.items.length}건</span>}<time>{span}</time></div>
    <div className="ko-reading"><p>{g.ko || (interpreting ? '한글 해석을 준비 중입니다. 원문은 아래에서 확인할 수 있습니다.' : '아직 한글 해석이 없습니다. 원문을 펼쳐 확인하세요.')}</p>{g.ko && <span className="reading-label">한글 해석{g.ko_source === 'rules' ? ' · 규칙' : g.ko_source === 'llm' ? ' · AI' : ''}</span>}</div>
    <details className="raw-toggle" open={open} onToggle={e => onToggle((e.currentTarget as HTMLDetailsElement).open)}><summary>영어 원문 보기{g.items.length > 1 ? ` (${g.items.length}건)` : ''}</summary>
      {open && g.items.map(it => <div className="raw-item" key={it.id} data-raw-id={it.id}><div className="raw-ref"><code>{it.id}</code><time>{clock(it.at)}</time></div><pre className="raw-text">{it.raw}</pre></div>)}
    </details>
    {g.cited_by.length > 0 && <div className="cited-by"><small>인용한 역할</small>{g.cited_by.map(r => <button key={r} type="button" onClick={() => chooseRole(r)}>{name(r)}</button>)}</div>}
  </article>;
}
function StepDetail({ n, refs, onClose, onRef }: { n: FlowNode; refs: { ref: string; found: boolean }[]; onClose: () => void; onRef: (ref: string) => void }) {
  const title = flowTitle(n);
  return <section className="agent-detail" aria-label="선택한 에이전트 상세">
    <div className="section-heading"><div><span className="eyebrow">{FLOW_TYPE_KO[n.type] ?? '단계'} · 선택한 단계</span><h3>{title}</h3></div><div className="inline"><span className={`badge ${n.status === 'failed' ? 'danger' : n.status === 'done' ? 'ok' : n.status === 'escalated' ? 'warn' : 'info'}`}>{flowStatusKo(n.status)}</span><button className="btn sm" type="button" onClick={onClose}>상세 닫기</button></div></div>
    <p className="muted">{n.job_ko || (n.label && n.label !== title ? n.label : '이 단계의 실행 상태와 연결된 근거를 확인합니다.')}</p>
    {n.job_ko && n.label && !n.role && n.label !== title && <p>{n.label}</p>}
    <div><h4>한글 결론</h4><p className="conclusion">{n.conclusion_ko || (n.status === 'planned' || n.status === 'running' ? '실행이 끝나면 결론이 표시됩니다.' : n.conclusion_raw ? '한글 해석을 준비 중입니다. 원문을 펼쳐 확인하세요.' : '이 단계에는 별도 결론이 없습니다.')}</p></div>
    {n.conclusion_raw && <details key={n.id}><summary>결론 원문 보기</summary><pre className="raw-text">{n.conclusion_raw}</pre></details>}
    {(n.role || n.model) && <dl className="kv"><dt>모델 · 추론 강도</dt><dd><span className="mono">{n.model || '미기록'}</span> · {effortKo(n.effort)}</dd><dt>입력 / 출력 토큰</dt><dd>{numberKo(n.tokens?.in)} / {numberKo(n.tokens?.out)}</dd><dt>비용 · 소요 시간</dt><dd>{money(n.cost_usd)} · {n.duration_ms == null ? '미기록' : `${numberKo(n.duration_ms / 1000, 1)}초`}</dd></dl>}
    {refs.length > 0 && <div><h4>입력 근거</h4><div className="ref-links">{refs.map(r => <button className={`ref-link ${r.found ? '' : 'missing'}`} type="button" key={r.ref} onClick={() => onRef(r.ref)} title={r.found ? '아래 근거로 이동합니다.' : '이 근거의 원문은 현재 응답에 없습니다.'}>{r.ref}{r.found ? '' : ' · 원문 없음'}</button>)}</div></div>}
  </section>;
}
function EventEntry({ e }: { e: RunEvent }) {
  if (e.kind === 'operator_message') return <div className="operator-bubble"><small>운영자 · {clock(e.at)}</small><p>{e.summary}</p></div>;
  if (e.kind === 'operator_applied') return <p className="chat-status">지시를 다음 단계에 반영했습니다.</p>;
  if (e.kind === 'steering_interpreted') return <div className="ack-bubble"><small>AI 지시 해석 · {clock(e.at)}</small><p>{String(e.detail?.ack_ko || '지시를 접수했습니다. 다음 단계부터 반영합니다.')}</p><span className="badge">{INTENT_KO[e.detail?.intent] ?? '지시 반영'}{e.detail?.target_role ? ` · ${roleKo(e.detail.target_role)}` : ''}</span></div>;
  return <details className="chat-event"><summary><i className={`dot ${e.kind === 'completed' ? 'ok' : e.kind === 'escalated' ? 'warn' : ''}`} /><span>{eventKo(e)}</span><time>{clock(e.at)}</time></summary><pre className="raw-text">{e.summary}</pre><p className="muted">{eventKo(e)} · 실행 기록 원문</p></details>;
}
export function IncidentRoom() { const { id } = useParams(); return <Room key={id} id={id || ''} />; }
function Room({ id }: { id: string }) {
  const narrative = usePolling<Narrative>(`/incidents/${id}/narrative`, d => narrativePollMs(d?.incident.status ?? 'open'));
  const commands = usePolling<{ items: Command[] }>(`/incidents/${id}/commands`, d => commandPollMs(d?.items ?? []));
  // Engine actions and handoff ownership live on the incident detail; poll faster only while an approval is pending.
  const detail = usePolling<IncidentDetail>(`/incidents/${id}`, d => pendingActions(d?.actions).length ? 2000 : 10000);
  const data = useMemo(() => narrative.data ? normalizeNarrative(narrative.data) : null, [narrative.data]);
  const incident = data ? currentIncident(data.incident, detail.data?.incident) : null;
  const [tab, setTab] = useState<Tab>('flow'); const [selected, setSelected] = useState<string | null>(null); const [filter, setFilter] = useState<RawFilter>('all');
  const [focused, setFocused] = useState<string | null>(null); const [opened, setOpened] = useState<Set<string>>(() => new Set());
  const [events, setEvents] = useState<RunEvent[]>([]); const [control, setControl] = useState('running'); const [eventError, setEventError] = useState(false); const [eventRevision, setEventRevision] = useState(0);
  const [text, setText] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [sent, setSent] = useState('');
  const cursor = useRef(0); const finished = useRef(false); const feed = useRef<HTMLDivElement>(null); const scrollNext = useRef(false);
  finished.current = incident ? isFinished(incident.status) : false;
  useEffect(() => {
    let stopped = false; let timer: ReturnType<typeof setTimeout>; const controller = new AbortController();
    async function load() {
      try { const r = await request<{ events: RunEvent[]; control: string }>(`/incidents/${id}/events?since=${cursor.current}`, { signal: controller.signal }); if (!stopped) { setControl(r.control); setEventError(false); if (r.events?.length) { cursor.current = Math.max(cursor.current, ...r.events.map(e => e.seq)); setEvents(old => [...new Map([...old, ...r.events].map(e => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq)); } } }
      catch { if (!stopped) setEventError(true); }
      if (!stopped) timer = setTimeout(load, finished.current ? 10000 : 2000);
    }
    void load(); return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [id, eventRevision]);
  useEffect(() => { if (scrollNext.current && feed.current) { feed.current.scrollTop = feed.current.scrollHeight; scrollNext.current = false; } }, [events, commands.data]);
  const node = data?.flow.nodes.find(n => n.id === selected) ?? null;
  const groups = useMemo(() => data ? evidenceGroups(data.raw, node, filter) : [], [data, node, filter]);
  // A reference jump opens and centres its group; it only runs on an explicit operator action.
  useEffect(() => {
    if (!focused) return;
    const g = groups.find(g => groupHas(g, focused)); if (!g) return;
    setOpened(s => new Set(s).add(g.key));
    requestAnimationFrame(() => document.getElementById(`evidence-${g.items[0].id}`)?.scrollIntoView({ block: 'center', behavior: 'instant' }));
  }, [focused, groups]);
  const reload = useCallback(() => { narrative.refresh(); commands.refresh(); detail.refresh(); setEventRevision(r => r + 1); }, [narrative.refresh, commands.refresh, detail.refresh]);
  const choose = useCallback((nid: string | null) => { setSelected(old => nid && old === nid ? null : nid); setFocused(null); if (nid) setTab('evidence'); }, []);
  const showRef = useCallback((ref: string) => { setFocused(null); setTab('evidence'); requestAnimationFrame(() => setFocused(ref)); }, []);
  async function send(e: FormEvent) {
    e.preventDefault(); const body = text.trim(); if (!body || busy || !data || !incident) return;
    setBusy(true); setError(''); setSent(''); scrollNext.current = true;
    try {
      if (isFinished(incident.status)) await request(`/incidents/${id}/commands`, { method: 'POST', body: JSON.stringify({ instruction: body }) });
      else { try { await request(`/incidents/${id}/messages`, { method: 'POST', body: JSON.stringify({ body }) }); } catch (e) { if (e instanceof ApiError && e.status === 409) await request(`/incidents/${id}/commands`, { method: 'POST', body: JSON.stringify({ instruction: body }) }); else throw e; } }
      setText(''); setSent('지시를 접수했습니다. 실행이 필요한 작업은 계획과 부작용을 확인한 뒤 진행합니다.'); reload();
    } catch { setError('지시를 보내지 못했습니다. 입력 내용은 유지했습니다. 다시 시도하세요.'); }
    finally { setBusy(false); }
  }
  async function ctl(action: 'pause' | 'resume' | 'stop') {
    if (action === 'stop' && !window.confirm('분석을 중단하시겠습니까? 이미 시작된 조치는 검증을 마친 뒤 멈춥니다.')) return;
    setBusy(true); setError(''); try { await request(`/incidents/${id}/control`, { method: 'POST', body: JSON.stringify({ action }) }); reload(); } catch { setError('분석 제어 요청에 실패했습니다. 상태를 확인하고 다시 시도하세요.'); } finally { setBusy(false); }
  }
  if (!data) return <div className="page"><Link to="/incidents">← 장애 목록</Link>{narrative.error ? <><ErrorBox>장애 상황을 불러오지 못했습니다.</ErrorBox><button className="btn" onClick={reload}>다시 불러오기</button></> : <div className="empty" role="status"><Spinner /> 장애 관제실을 준비 중입니다.</div>}</div>;
  const i = incident!, done = isFinished(i.status);
  const sameStatus = i.status === data.incident.status;
  const stage = sameStatus ? data.headline.stage : STAGE_FROM_STATUS[i.status];
  const next = sameStatus ? data.headline.next : done ? '최신 장애 상태를 확인하고 아래 대화에서 후속 작업을 지시하세요.' : '최신 진행 상태에 맞춰 아래 기록과 조치 요청을 확인하세요.';
  const name = (r: string) => roleKo(r, data.flow.nodes.find(n => n.role === r)?.role_ko);
  const chooseRole = (r: string) => { const target = data.flow.nodes.find(n => n.role === r); if (target) { setSelected(target.id); setFocused(null); } };
  const refs = node ? (node.inputs ?? []).map(ref => ({ ref, found: data.raw.some(it => matchesRef(it, ref)) })) : [];
  const rawCount = groups.reduce((a, g) => a + g.items.length, 0);
  const approvals = done ? [] : pendingActions(detail.data?.actions);
  const pending = approvals.length > 0 || (commands.data?.items ?? []).some(c => c.status === 'awaiting_confirmation' || c.status === 'awaiting_secret');
  return <div className={`incident-room tab-${tab}`}>
    <section className="headline" aria-label="장애 상황 요약"><div className="headline-top"><Link to="/incidents">← 장애 관제</Link><span className="mono">#{i.id}</span><LevelBadge value={i.severity_level} /><StatusBadge value={i.status} /><h1>장애 관제실</h1><span className="headline-facts"><span>{age(i.created_at, i.closed_at)} 경과</span><span>추정 비용 <b>{money(i.total_cost_usd)}</b> <small>(해석 포함)</small></span></span></div>
      <h2>{data.headline.what || '장애 상황을 확인 중입니다.'}</h2><p className="headline-impact">{data.headline.impact || '영향 범위를 확인 중입니다.'}</p>
      <div className="headline-next"><span className="stage-label"><i className={`dot ${done ? i.status === 'resolved' ? 'ok' : 'warn' : 'info'}`} />{stage?.label || STAGE_FROM_STATUS[i.status]?.label}</span><p><b>다음</b> {next || '기록을 확인하세요.'}</p></div></section>
    {(narrative.error || commands.error || detail.error || eventError) && <div className="room-error"><ErrorBox>{narrative.error ? '상황 정보' : commands.error ? '지시 목록' : detail.error ? '조치 승인 정보' : '실행 기록'} 갱신에 실패했습니다. 마지막으로 받은 내용을 표시합니다.</ErrorBox><button className="btn sm" onClick={reload}>다시 불러오기</button></div>}
    <nav className="room-tabs" aria-label="관제실 화면 전환">{([['flow', '흐름'], ['evidence', node ? '단계·근거' : '근거·해석'], ['chat', '대화']] as const).map(([key, label]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}{key === 'chat' && pending && <i className="dot warn" />}</button>)}</nav>
    <div className="room-columns">
      <section className="room-pane flow-pane" aria-label="AI 분석 흐름"><header className="pane-heading"><div><span className="pane-index">01</span><h2>AI 분석 흐름</h2></div><span className="badge info">{done ? '실행 기록' : '실시간'}</span></header>
        <FlowGraph flow={data.flow} selected={selected} onSelect={choose} /></section>
      <section className="room-pane evidence-pane" aria-label={node ? '선택한 단계와 근거' : '근거와 한글 해석'}><header className="pane-heading"><div><span className="pane-index">02</span><h2>{node ? '선택한 단계 · 근거' : '근거 · 한글 해석'}</h2></div><span className="faint">{groups.length}묶음 · 원문 {rawCount}건</span></header>
        <div className="evidence-scroll" aria-label="근거 목록">
          {node ? <StepDetail n={node} refs={refs} onClose={() => choose(null)} onRef={showRef} /> : <div className="filters" aria-label="원문 종류 필터">{RAW_FILTERS.map(([key, label]) => <button className="filter" key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>)}</div>}
          {!node && data.flow.nodes.length > 0 && <p className="evidence-hint">흐름의 단계를 선택하면 그 단계의 결론과 입력 근거만 모아 봅니다.</p>}
          {data.interpreting && <div className="interpreting" role="status"><Spinner /> 한글 해석을 준비 중입니다. 원문은 그대로 유지됩니다.</div>}
          {node && <h4 className="evidence-title">이 단계가 본 근거</h4>}
          {focused && !groups.some(g => groupHas(g, focused)) && <div className="notice" role="status">이 입력 근거의 원문은 현재 응답에 없습니다. 근거 ID: {focused}</div>}
          <div className="evidence-list">{groups.length === 0 && <div className="empty">{node ? '이 단계에 연결된 원문 근거가 없습니다.' : '이 종류의 원문이 없습니다.'}</div>}
            {groups.map(g => <EvidenceGroup key={g.key} g={g} open={opened.has(g.key)} onToggle={o => setOpened(s => { const next = new Set(s); if (o) next.add(g.key); else next.delete(g.key); return next; })} highlighted={!!focused && groupHas(g, focused)} name={name} chooseRole={chooseRole} interpreting={data.interpreting} />)}</div>
        </div></section>
      <section className="room-pane chat-pane" aria-label="운영자 대화"><header className="pane-heading"><div><span className="pane-index">03</span><h2>운영자 대화</h2></div><span className="faint">{done ? '후속 작업' : '분석 지시'}</span></header><div className="chat-feed" ref={feed}><div className="chat-intro"><b>{done ? 'AI의 후속 작업을 지시하세요' : '분석에 방향을 더하세요'}</b><p>{done ? '지시 → 계획 → 부작용 확인 → 실행 순서로 진행합니다. 확인 전에는 변경하지 않습니다.' : '지시는 다음 단계부터 반영합니다. 원문 근거와 운영자 지시는 구분하여 기록합니다.'}</p></div>{events.map(e => <EventEntry e={e} key={e.seq} />)}{approvals.map(a => <ActionApprovalCard action={a} key={`action-${a.id}`} onChange={reload} />)}{i.status === 'handed_off' && detail.data && <HandoffCard incident={i} onChange={reload} />}{(commands.data?.items ?? []).map(c => <CommandCard command={c} key={c.id} onChange={reload} />)}</div>
        <div className="composer">{!done && <div className="ctrl"><span className="control-status">{control === 'paused' ? '일시정지됨' : control === 'stopping' ? '중단 중' : '자동 분석 중'}</span><button className="btn sm" disabled={busy || control === 'stopping'} onClick={() => void ctl(control === 'paused' ? 'resume' : 'pause')}>{control === 'paused' ? '재개' : '일시정지'}</button><button className="btn sm danger" disabled={busy || control === 'stopping'} onClick={() => void ctl('stop')}>중단</button></div>}<ErrorBox>{error}</ErrorBox>{sent && <p className="composer-notice" role="status">{sent}</p>}<form onSubmit={send}><textarea aria-label="운영자 메시지" rows={2} value={text} maxLength={done ? 8000 : 2000} placeholder={done ? 'AI에게 작업을 지시하세요 (실행 전 부작용을 확인합니다)' : 'AI 분석에 지시하세요 (다음 단계부터 반영)'} onChange={e => setText(e.target.value)} /><button className="btn primary" type="submit" disabled={busy || !text.trim()}>{busy ? '전송 중' : '보내기'}</button></form><small>비밀값은 대화에 쓰지 말고 전용 입력란을 사용하세요.</small></div>
      </section></div>
  </div>;
}
