import { useCallback, useEffect, useMemo, useState } from 'react';
import { latestOnly } from './latestOnly';
import { Link, useSearchParams } from 'react-router-dom';
import { age, dateTime, money, request, type Incident, type Status } from './api';
import { INCIDENT_STATUS_KO, STAGE_FROM_STATUS, placeKo } from './labels';
import { StatusBadge, LevelBadge, ErrorBox } from './ui';
import { numberKo } from './overviewModel';
const statuses: Status[] = ['open', 'analyzing', 'action_pending', 'acting', 'resolved', 'handed_off', 'failed', 'closed'];
export function Incidents() {
  const [params, setParams] = useSearchParams();
  const filter = statuses.includes(params.get('status') as Status) ? params.get('status')! : '';
  const query = params.get('search')?.slice(0, 120) ?? '';
  const environment = ['dev', 'stg', 'prd'].includes(params.get('environment') ?? '') ? params.get('environment')! : '';
  const n = Number(params.get('offset')); const offset = Number.isSafeInteger(n) && n >= 0 ? n : 0;
  const [search, setSearch] = useState(query);
  const [items, setItems] = useState<Incident[]>([]); const [total, setTotal] = useState(0); const [error, setError] = useState(''); const [loading, setLoading] = useState(true);
  useEffect(() => setSearch(query), [query]);
  const set = (next: { status?: string; search?: string; offset?: number; environment?: string }) => {
    const u = new URLSearchParams(); const s = next.status ?? filter, q = next.search ?? query, o = next.offset ?? offset;
    const env = next.environment ?? environment; if (env) u.set('environment', env);
    if (s) u.set('status', s); if (q) u.set('search', q); if (o) u.set('offset', String(o)); setParams(u);
  };
  // Every request (first load and each poll) shares one abort signal per filter, and only the newest may apply its result:
  // a late poll response for an old filter can never overwrite the list of the current filter.
  const gate = useMemo(() => latestOnly(), []);
  const load = useCallback((signal: AbortSignal) => gate.run(
    () => request<{ items: Incident[]; total: number }>(`/incidents?limit=50&offset=${offset}${filter ? `&status=${encodeURIComponent(filter)}` : ''}${query ? `&search=${encodeURIComponent(query)}` : ''}`, { signal }),
    d => { if (signal.aborted) return; setItems(d.items ?? []); setTotal(d.total); setError(''); setLoading(false); },
    e => { if (!signal.aborted && !(e instanceof Error && e.name === 'AbortError')) { setError('장애 목록을 불러오지 못했습니다. 다시 시도하세요.'); setLoading(false); } },
  ), [gate, filter, offset, query]);
  useEffect(() => { const controller = new AbortController(); setLoading(true); void load(controller.signal); const t = window.setInterval(() => void load(controller.signal), 5000); return () => { controller.abort(); clearInterval(t); }; }, [load]);
  const shown = environment ? items.filter(i => (i.environment ?? i.namespace) === environment) : items;
  return <div className="page incidents-page">
    <div className="page-head"><div><div className="eyebrow">장애 대응</div><h1>장애 관제</h1><p>감지부터 복구까지, AI가 수행한 단계와 결과를 확인합니다.</p></div><span className="badge">전체 {total}건</span></div>
    <form className="list-filters" onSubmit={e => { e.preventDefault(); set({ offset: 0, search: search.trim() }); }}>
      <select className="select" aria-label="장애 환경 필터" value={environment} onChange={e => set({ environment: e.target.value, offset: 0 })}><option value="">전체 환경</option>{['dev', 'stg', 'prd'].map(env => <option key={env}>{env}</option>)}</select>
      <input className="input" aria-label="장애 검색" placeholder="제목 또는 워크로드 검색" maxLength={120} value={search} onChange={e => setSearch(e.target.value)} />
      <select className="select" aria-label="상태 필터" value={filter} onChange={e => set({ status: e.target.value, offset: 0 })}><option value="">모든 상태</option>{statuses.map(s => <option key={s} value={s}>{INCIDENT_STATUS_KO[s]}</option>)}</select><button className="btn" type="submit">검색</button>
    </form>
    <p className="environment-filter-note" role="status">환경 필터는 서버에서 조회한 현재 페이지(최대 50건)에만 적용됩니다. 전체 {total}건 중 현재 페이지 {items.length}건{environment ? `에서 ${shown.length}건 표시` : ' 표시'}. 다른 페이지의 장애는 이전·다음으로 확인하세요.</p>
    <ErrorBox>{error}</ErrorBox>
    <div className="incident-list card">{loading ? <div className="empty" role="status">불러오는 중…</div> : shown.length === 0 ? <div className="empty">{environment ? '현재 페이지에서 일치하는 장애가 없습니다. 다른 페이지도 확인하세요.' : '표시할 장애가 없습니다.'}</div> : shown.map(i => <Link className="incident-row" key={i.id} to={`/incidents/${i.id}`} aria-label={`장애 ${i.id} ${i.workload} 관제실 열기`}>
      <div className="incident-identity"><span className="faint mono">#{i.id}</span><strong>{i.workload}</strong><small className="muted">{placeKo(i.environment, i.system, i.workload, i.namespace)}</small><span className="raw-title mono">{i.title}</span></div>
      <div className="incident-badges"><LevelBadge value={i.severity_level} /><StatusBadge value={i.status} /></div>
      <div className="incident-stage"><small className="faint">AI 처리 단계</small><b>{i.stage?.label || STAGE_FROM_STATUS[i.status]?.label || '단계 미확인'}</b></div>
      <div className="incident-time"><span>{age(i.created_at, i.closed_at)} 경과</span><small>{dateTime(i.created_at)}</small><span className="mono">{money(i.total_cost_usd)} · {numberKo(i.agent_run_count)}회 실행</span></div><span className="row-arrow" aria-hidden>↗</span>
    </Link>)}
      <div className="pager"><span>{total ? `${offset + 1}–${Math.min(offset + 50, total)} / ${total}` : '0건'}</span><div className="inline"><button className="btn" disabled={!offset} onClick={() => set({ offset: Math.max(0, offset - 50) })}>이전</button><button className="btn" disabled={offset + 50 >= total} onClick={() => set({ offset: offset + 50 })}>다음</button></div></div>
    </div>
  </div>;
}
