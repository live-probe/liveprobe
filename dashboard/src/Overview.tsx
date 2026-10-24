import { useEffect, useState } from 'react';
import { clock, type Overview } from './api';
import { bytesKo, cpuKo, numberKo, overviewKpis, percent } from './overviewModel';
import { eventReasonKo, levelKo } from './labels';
import { ResourceDetail, TopologyMap } from './TopologyMap';
import { usePolling } from './usePolling';
import { ErrorBox, Spinner } from './ui';
function Meter({ used, capacity }: { used: number | null; capacity: number | null }) { const p = percent(used, capacity); return <div className={`meter ${p == null ? 'unobserved' : ''}`} aria-label={p == null ? '사용률 미관측' : `사용률 ${Math.round(p)}%`}><span style={{ width: `${p ?? 0}%` }} /></div>; }
const SOURCES: Record<string, string> = { prometheus: 'Prometheus', kubernetes: 'Kubernetes', hubble: 'Hubble', envoy: 'Envoy' };
type SideTab = 'events' | 'namespaces' | 'resource';
/** Compact cluster status for the side area: each figure is a source value, null stays 미관측. */
function Stats({ o }: { o: Overview }) {
  const k = overviewKpis(o); const today = o.incidents?.today; const open = o.incidents?.open ?? [];
  return <section className="ops-stats" aria-label="클러스터 건강도 요약"><dl>
    <div><dt>준비된 노드</dt><dd><b>{numberKo(k.nodesReady)}</b> / {numberKo(k.nodesTotal)}</dd><Meter used={k.nodesReady} capacity={k.nodesTotal} /></div>
    <div><dt>CPU 사용 / 전체</dt><dd><b>{numberKo(k.cpuUsed, 2)}</b> / {numberKo(k.cpuCapacity, 1)} 코어</dd><Meter used={k.cpuUsed} capacity={k.cpuCapacity} /></div>
    <div><dt>메모리 사용 / 전체</dt><dd><b>{bytesKo(k.memUsed)}</b> / {bytesKo(k.memCapacity)}</dd><Meter used={k.memUsed} capacity={k.memCapacity} /></div>
    <div><dt>Pod 실행 · 대기 · 실패</dt><dd><b>{numberKo(k.running)}</b> · <span className="warn-text">{numberKo(k.pending)}</span> · <span className="danger-text">{numberKo(k.failed)}</span></dd></div>
    <div><dt>재시작 · 최근 1시간</dt><dd><b>{numberKo(k.restarts)}</b>회</dd></div>
    <div><dt>진행 중 장애</dt><dd><b>{open.length}</b>건{open.length > 0 && <small> ({Object.entries(k.levels).filter(([, c]) => c > 0).map(([l, c]) => `${levelKo(l)} ${c}`).join(' · ')})</small>}</dd></div>
    <div><dt>오늘 처리</dt><dd>자동 복구 <b className="ok-text">{numberKo(today?.resolved)}</b> · 인계 {numberKo(today?.handed_off)} · 실패 {numberKo(today?.failed)} <small>/ 감지 {numberKo(today?.total)}</small></dd></div>
  </dl></section>;
}
function Events({ o }: { o: Overview }) {
  const events = o.events ?? [];
  if (!events.length) return <div className="empty">관측된 경고 이벤트가 없습니다.</div>;
  return <>{events.map((e, i) => <article className="warning-event" key={`${e.at}-${e.object}-${i}`}><div><b>{eventReasonKo(e.reason)}</b><span className="badge warn">{e.count == null ? '횟수 미관측' : `${e.count}회`}</span></div><small>{clock(e.at)} · {e.namespace ?? '클러스터 범위'} · {e.object}</small><code className="mono">{e.reason}</code><pre className="raw-text">{e.message}</pre></article>)}</>;
}
function Namespaces({ o }: { o: Overview }) {
  const ns = o.cluster?.namespaces ?? [];
  if (!ns.length) return <div className="empty">네임스페이스 정보가 아직 없습니다.</div>;
  return <>{ns.map(n => <article className="ns-row" key={n.name}><div><b className="mono">{n.name}</b><span className={`badge ${n.workloads_unhealthy ? 'danger' : ''}`}>비정상 {numberKo(n.workloads_unhealthy)} / {numberKo(n.workloads)}</span></div>
    <dl><dt>Pod 실행 / 대기 / 실패</dt><dd>{numberKo(n.pods_running)} / {numberKo(n.pods_pending)} / {numberKo(n.pods_failed)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(n.restarts_1h)}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(n.cpu_cores)} · {bytesKo(n.mem_bytes)}</dd></dl></article>)}</>;
}
export function OverviewPage() {
  const { data: o, error, refresh } = usePolling<Overview>('/overview', 10000);
  const [selected, setSelected] = useState<string | null>(null); const [tab, setTab] = useState<SideTab>('events');
  useEffect(() => { if (selected) setTab('resource'); else setTab(t => t === 'resource' ? 'events' : t); }, [selected]);
  if (!o) return <div className="page"><h1>운영 관제</h1>{error ? <><ErrorBox>운영 정보를 불러오지 못했습니다.</ErrorBox><button className="btn" onClick={refresh}>다시 불러오기</button></> : <div className="empty" role="status"><Spinner /> 클러스터 관측 정보를 불러오는 중입니다.</div>}</div>;
  const tabs: [SideTab, string][] = [['events', `경고 이벤트 ${(o.events ?? []).length}`], ['namespaces', `네임스페이스 ${(o.cluster?.namespaces ?? []).length}`], ...(selected ? [['resource', '자원 상세'] as [SideTab, string]] : [])];
  return <div className="ops"><h1 className="sr-only">운영 관제</h1>
    <TopologyMap overview={o} selected={selected} onSelect={setSelected} />
    <aside className="ops-side" aria-label="클러스터 상태"><header className="ops-side-head"><b>클러스터 상태</b><span className="faint">{clock(o.generated_at)} 기준</span><button className="btn sm" onClick={refresh}>새로고침</button></header>
      {error && <ErrorBox>갱신에 실패했습니다. 마지막으로 관측한 정보를 표시합니다.</ErrorBox>}
      <div className="source-chips" aria-label="데이터 원천 상태">{['kubernetes', 'prometheus', 'hubble', 'envoy'].map(key => { const s = o.sources?.[key]; const ok = s === 'ok' || s === 'connected'; return <div className={`source-chip ${ok ? 'connected' : 'missing'}`} key={key}><i className={`dot ${ok ? 'ok' : 'warn'}`} /><b>{SOURCES[key]}</b><span>{ok ? '연결됨' : s ? '확인 필요' : '미관측'}</span>{s && !ok && <details><summary>원인</summary><pre className="raw-text">{s}</pre><p>원천 수집 상태를 확인하세요.</p></details>}</div>; })}</div>
      <Stats o={o} />
      <nav className="side-tabs" aria-label="보조 정보 전환">{tabs.map(([key, label]) => <button key={key} type="button" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      <div className="side-scroll" aria-label={tab === 'events' ? '최근 경고 이벤트' : tab === 'namespaces' ? '네임스페이스별 자원' : '선택한 자원'}>{tab === 'events' ? <Events o={o} /> : tab === 'namespaces' ? <Namespaces o={o} /> : selected && <ResourceDetail overview={o} id={selected} onClose={() => setSelected(null)} />}</div>
    </aside>
  </div>;
}
