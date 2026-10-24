import { memo, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Background, MarkerType, ReactFlow, ReactFlowProvider, useReactFlow, useStore, type NodeProps, type Node, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { Overview, Workload, WorkloadIncident } from './api';
import { problemsOf, type TopoNode } from './mapModel';
import { bytesKo, connectionsOf, cpuKo, edgeLabelFits, edgeText, errorRateKo, handlesFor, MAP_H, MAP_W, numberKo, overviewMap, resourceHealth } from './overviewModel';
import { levelKo } from './labels';
import { GRAPH_ARIA, FacingHandles, GraphControls, revealOnFocus } from './graphShared';
const Resource = memo(function Resource({ id, data }: NodeProps) {
  const d = data as { node: TopoNode; workload?: Workload; incident: WorkloadIncident | null; open: (id: string) => void; selected: boolean; alert: string; offset: { x: number; y: number } };
  const n = d.node, w = d.workload; const health = resourceHealth(n, w); const rf = useReactFlow();
  return <div className="topology-resource"><FacingHandles /><button className={`resource-button nodrag nopan ${d.incident ? 'has-incident' : d.alert !== 'none' ? 'has-warning' : ''} ${d.selected ? 'selected' : ''}`} aria-pressed={d.incident ? undefined : d.selected} aria-label={`${n.name} ${d.incident ? `장애 ${d.incident.id} 관제실 열기` : '자원 상세 열기'}`} onClick={() => d.open(n.id)} onFocus={e => revealOnFocus(e.currentTarget, rf, id, MAP_W, MAP_H, d.offset)}>
    <div className="resource-top"><span className={`resource-glyph ${n.role === 'data' ? 'data' : ''}`} aria-hidden>{n.role === 'data' ? '▤' : n.role === 'entry' ? '⇥' : '◇'}</span><div><strong>{n.name}</strong><small>{n.kind}</small></div><span className={`health-dot ${d.alert !== 'none' || d.incident ? 'unhealthy' : health === '준비됨' ? 'ready' : health === '미관측' || health === '흐름만 관측' ? 'unknown' : 'unhealthy'}`} aria-label={health} /></div>
    <div className="resource-health">{health}{w && w.ready != null && w.desired != null ? ` · ${w.ready}/${w.desired}` : ''}{!d.incident && d.alert !== 'none' && <span className="observation-warning">관측 경고</span>}</div>
    <div className="resource-metrics"><span>요청 {numberKo(w?.req_rate, 1)}{w?.req_rate != null ? '/초' : ''}</span><span>오류 {errorRateKo(w?.err_rate)}</span><span>지연 {numberKo(w?.p95_ms, 0)}{w?.p95_ms != null ? 'ms' : ''}</span><span>CPU {cpuKo(w?.cpu)}</span><span>메모리 {bytesKo(w?.mem)}</span></div>
    {d.incident && <span className="incident-pin"><i aria-hidden>!</i>{levelKo(d.incident.level)} 장애 ↗</span>}
  </button></div>;
});
function Namespace({ data }: NodeProps) { return <div className="namespace-box"><span>네임스페이스</span><b>{String(data.name)}</b></div>; }
const types = { resource: Resource, namespace: Namespace };
type MapData = ReturnType<typeof overviewMap>;
function Canvas({ m, open, selected, fitKey }: { m: MapData; open: (id: string) => void; selected: string | null; fitKey: string }) {
  const rf = useReactFlow(); const width = useStore(s => s.width), height = useStore(s => s.height);
  // Fit the whole map whenever the visible set or the canvas size changes; ordinary data refreshes keep the operator's pan/zoom.
  useEffect(() => { if (width > 0 && height > 0) { const t = requestAnimationFrame(() => void rf.fitView({ padding: .05, maxZoom: 1 })); return () => cancelAnimationFrame(t); } }, [fitKey, width, height, rf]);
  const flow = useMemo(() => {
    const groups = new Map(m.groups.map(g => [g.id, g]));
    const nodes: Node[] = [...m.groups.map(g => ({ id: g.id, type: 'namespace', position: { x: g.x, y: g.y }, width: g.width, height: g.height, data: { name: g.name }, style: { width: g.width, height: g.height }, selectable: false, focusable: false, draggable: false })),
      ...m.nodes.map(n => { const g = groups.get(n.groupId); return { id: n.id, type: 'resource', parentId: n.groupId, extent: 'parent' as const, position: { x: n.x, y: n.y }, width: MAP_W, height: MAP_H, data: { ...n, open, selected: n.id === selected, offset: { x: g?.x ?? 0, y: g?.y ?? 0 } }, style: { width: MAP_W, height: MAP_H }, focusable: false, selectable: false, draggable: false }; })];
    const abs = new Map(m.nodes.map(n => { const g = groups.get(n.groupId); return [n.id, { x: n.x + (g?.x ?? 0), y: n.y + (g?.y ?? 0), group: n.groupId }]; }));
    const pairs = new Set(m.edges.map(e => `${e.source}>${e.target}`));
    const edges: Edge[] = m.edges.map(e => {
      const color = e.state === 'blocked' ? '#b4232c' : e.state === 'degraded' ? '#a64b08' : '#75869b';
      const a = abs.get(e.source)!, b = abs.get(e.target)!;
      // Numbers are drawn only where they cannot land on a card or another label; the resource detail lists every connection.
      const label = edgeLabelFits(a, b, pairs.has(`${e.target}>${e.source}`)) ? edgeText(e, { compact: true }) : undefined;
      const ariaLabel = e.raw?.req_rate != null ? `초당 요청 ${numberKo(e.raw.req_rate, 1)} · 오류율 ${errorRateKo(e.raw.err_rate)}` : e.observed ? `관측 흐름 ${numberKo(e.flows)}건` : '트래픽 미관측 연결';
      return { id: e.id, source: e.source, target: e.target, ...handlesFor(a, b), type: 'smoothstep', ariaLabel, label, labelStyle: { fontSize: 11, fill: '#526174' }, labelBgStyle: { fill: '#ffffff' }, style: { stroke: color, strokeWidth: e.observed ? 1.8 : 1.2, strokeDasharray: e.observed ? undefined : '5 5', opacity: e.stale ? .35 : .8 }, markerEnd: { type: MarkerType.ArrowClosed, color }, focusable: false };
    });
    return { nodes, edges };
  }, [m, open, selected]);
  return <><ReactFlow nodes={flow.nodes} edges={flow.edges} nodeTypes={types} fitView fitViewOptions={{ padding: .05, maxZoom: 1 }} minZoom={.15} maxZoom={1.6} nodesConnectable={false} nodesDraggable={false} elementsSelectable={false} edgesFocusable={false} nodesFocusable={false} deleteKeyCode={null} ariaLabelConfig={GRAPH_ARIA} proOptions={{ hideAttribution: true }}><Background gap={22} color="#e1e7ed" /></ReactFlow>
    <GraphControls label="지도"><span><i className="legend-line" />관측된 트래픽</span><span><i className="legend-line dashed" />미관측 연결</span><span><i className="dot danger" />장애 위치</span><span>선 위 숫자: 초당 요청 · 오류율(전체는 자원 상세)</span><span>지연: 상위 95% 응답 시간</span></GraphControls></>;
}
/** The operations map: the dominant surface. Selecting a failed resource opens its incident room; others open the side detail. */
export function TopologyMap({ overview: o, selected, onSelect }: { overview: Overview; selected: string | null; onSelect: (id: string | null) => void }) {
  const go = useNavigate(); const [namespace, setNamespace] = useState(''); const [showPlatform, setShowPlatform] = useState(false);
  const m = useMemo(() => overviewMap(o, namespace, { showPlatform }), [o, namespace, showPlatform]);
  const all = useMemo(() => overviewMap(o, namespace, { showPlatform: true }), [o, namespace]);
  const names = [...new Set((o.topology?.nodes ?? []).flatMap(n => n.namespace ? [n.namespace] : []))].sort();
  const open = useMemo(() => (id: string) => { const n = all.nodes.find(n => n.id === id); if (n?.incident) go(`/incidents/${n.incident.id}`); else onSelect(id); }, [all, go, onSelect]);
  const shown = new Set(m.nodes.map(n => n.id));
  const fitKey = `${namespace}|${showPlatform}|${m.nodes.length}|${m.groups.length}`;
  return <section className="ops-map" aria-label="클러스터 연결 지도"><div className="map-toolbar"><h2>서비스 연결 지도</h2>
      <select className="select" aria-label="지도 네임스페이스" value={namespace} onChange={e => setNamespace(e.target.value)}><option value="">전체 네임스페이스</option>{names.map(n => <option key={n}>{n}</option>)}</select>
      <select className="select" aria-label="지도 자원 선택" value="" onChange={e => e.target.value && open(e.target.value)}><option value="">자원 상세 또는 장애 열기</option>{all.nodes.map(n => <option value={n.id} key={n.id}>{n.node.name} · {n.node.namespace ?? '외부'}{n.incident ? ' · 장애' : ''}{shown.has(n.id) ? '' : ' · 지도에서 숨김'}</option>)}</select>
      {!namespace && (m.hiddenPlatform > 0 || showPlatform) && <button type="button" className="btn sm" aria-pressed={showPlatform} onClick={() => setShowPlatform(v => !v)}>{showPlatform ? '시스템 구성요소 숨기기' : `시스템 구성요소 ${m.hiddenPlatform}개 표시`}</button>}
      <span className="badge">{m.nodes.length}개 자원</span></div>
    {!o.topology && <div className="notice">토폴로지 미관측 · 연결이 없다는 뜻은 아닙니다.</div>}
    {o.topology?.freshness?.stale && <div className="notice">네트워크 흐름 관측이 지연되었습니다. 흐린 연결선은 현재 상태를 보장하지 않습니다.</div>}
    <div className="topology-canvas" aria-label="네임스페이스별 토폴로지"><ReactFlowProvider><Canvas m={m} open={open} selected={selected} fitKey={fitKey} /></ReactFlowProvider></div>
  </section>;
}
/** Collected fields of one resource, shown in the side area. Unobserved never reads as healthy. */
export function ResourceDetail({ overview: o, id, onClose }: { overview: Overview; id: string; onClose: () => void }) {
  const m = overviewMap(o, '', { showPlatform: true }); const d = m.nodes.find(n => n.id === id);
  if (!d) return null;
  const links = connectionsOf(m, id);
  return <aside className="resource-detail" aria-label="자원 상세"><div className="section-heading"><div><span className="eyebrow">{d.node.kind} · {d.node.namespace ?? '외부'}</span><h3>{d.node.name}</h3></div><button className="btn sm" onClick={onClose}>자원 상세 닫기</button></div>
    <p>{resourceHealth(d.node, d.workload)}{d.workload?.ready != null && d.workload?.desired != null ? ` · 준비 ${d.workload.ready}/${d.workload.desired}` : ''}</p>
    {problemsOf(d.node).length > 0 && <div className="notice"><b>관측 경고</b><ul>{problemsOf(d.node).map((p, i) => <li key={i}>{p.text}</li>)}</ul><small>수집된 관측 창의 값이며 자원 준비 상태와 별개입니다.</small></div>}
    <dl className="kv"><dt>요청</dt><dd>{numberKo(d.workload?.req_rate, 1)}{d.workload?.req_rate != null ? '/초' : ''}</dd><dt>오류율</dt><dd>{errorRateKo(d.workload?.err_rate)}</dd><dt>상위 95% 지연</dt><dd>{numberKo(d.workload?.p95_ms, 0)}{d.workload?.p95_ms != null ? 'ms' : ''}</dd><dt>CPU · 메모리</dt><dd>{cpuKo(d.workload?.cpu)} · {bytesKo(d.workload?.mem)}</dd><dt>재시작 · 1시간</dt><dd>{numberKo(d.workload?.restarts_1h)}</dd></dl>
    {(d.node.services ?? []).map(s => <div className="service-detail" key={s.name}><b>Service · {s.name}</b><span>포트 {(s.ports ?? []).join(', ') || '미관측'}</span><span>준비된 엔드포인트 {numberKo(s.endpoints_ready)}</span></div>)}
    {links.length > 0 && <div className="service-detail"><b>연결 · {links.length}개</b>{links.map(l => <span key={l.id}>{l.direction === 'out' ? '→' : '←'} {l.peer} · {l.text}{l.state === 'blocked' ? ' · 차단' : l.state === 'degraded' ? ' · 저하' : ''}</span>)}</div>}
    <details><summary>수집 필드 원문 보기</summary><pre className="raw-text">{JSON.stringify({ resource: d.node, workload: d.workload ?? null }, null, 2)}</pre></details><p className="muted">미관측은 정상을 뜻하지 않습니다.</p>
  </aside>;
}
