import { memo, useEffect, useMemo } from 'react';
import { Background, MarkerType, ReactFlow, ReactFlowProvider, useReactFlow, useStore, type NodeProps, type Node, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { Narrative } from './api';
import type { FlowNode } from './api';
import { FLOW_H, FLOW_W, FLOW_Y_GAP, EDGE_KIND_KO, FLOW_STATUS_SHORT, FLOW_TYPE_KO, flowHandles, flowStatusKo, flowSub, flowTitle, layoutFlow, validFlowEdges } from './narrative';
import { GRAPH_ARIA, FacingHandles, GraphControls, revealOnFocus } from './graphShared';
import { DiagramIcon } from './DiagramIcon';
import './diagramTheme.css';
import './analysisFlow.css';
const StageNode = memo(function StageNode({ id, data }: NodeProps) {
  const d = data as { node: FlowNode; selected: boolean; choose: (id: string) => void };
  const n = d.node; const rf = useReactFlow();
  const kind = FLOW_TYPE_KO[n.type] ?? '단계';
  // Depth identifies a graph level, not the execution order of parallel agents.
  const depth = Number.isFinite(n.depth) ? n.depth + 1 : 1;
  const sub = flowSub(n);
  const detail = n.model || (sub.startsWith(`${kind} · `) ? sub.slice(kind.length + 3) : '');
  const handoff = n.type === 'outcome' && /인계/.test(n.label);
  return <div className={`flow-step ${n.status} type-${n.type} ${handoff ? 'is-handoff' : ''} ${d.selected ? 'selected' : ''}`}><FacingHandles /><button type="button" className="nodrag nopan" aria-label={`${kind} · ${flowTitle(n)} · ${flowStatusKo(n.status)} · ${n.label}${n.model ? ` · ${n.model}` : ''} · 깊이 ${depth} · 상세 보기`} aria-pressed={d.selected} onClick={() => d.choose(n.id)} onFocus={e => revealOnFocus(e.currentTarget, rf, id, FLOW_W, FLOW_H)}>
    <span className="flow-role-icon"><DiagramIcon kind={handoff ? 'steer' : n.type} /></span>
    <span className="flow-step-top"><strong title={flowTitle(n)}>{flowTitle(n)}</strong><span className={`flow-state ${n.status}`}>{n.status === 'running' && <i aria-hidden="true" />}{FLOW_STATUS_SHORT[n.status] ?? flowStatusKo(n.status)}</span></span>
    <span className="flow-step-meta"><span className="flow-depth" title={`흐름 깊이 ${depth} · 실행 순서가 아닌 같은 깊이의 단계`}>{String(depth).padStart(2, '0')}</span><span className="flow-kind">{kind}</span>{detail && <small className={`flow-sub${n.model ? ' flow-model' : ''}`} title={detail}>{detail}</small>}</span>
  </button></div>;
});
const nodeTypes = { step: StageNode };
const MIN_READABLE = .6;
const edgeColors: Record<string, string> = { flow: '#a0b2ca', escalate: '#ffd08a', reroute: '#d0b1ff', steer: '#a9baff', verify: '#85dfb5' };
/** Labels only occupy the gap between adjacent rows; skipped/parallel paths retain their full accessible description. */
function analysisFlowEdges(flow: Narrative['flow'], positions = new Map(layoutFlow(flow.nodes).map(p => [p.id, p]))): Edge[] {
  const byId = new Map(flow.nodes.map(n => [n.id, n]));
  return validFlowEdges(flow.nodes, flow.edges).map(e => {
    const color = edgeColors[e.kind] ?? edgeColors.flow;
    const planned = byId.get(e.target)?.status === 'planned';
    const a = positions.get(e.source)!, b = positions.get(e.target)!;
    const adjacent = Math.abs(a.y - b.y) === FLOW_H + FLOW_Y_GAP;
    const label = adjacent ? (e.kind === 'flow' ? e.label || undefined : e.label || EDGE_KIND_KO[e.kind]) : undefined;
    return { ...e, ...flowHandles(a, b), ariaLabel: `${EDGE_KIND_KO[e.kind] ?? '연결'}${e.label ? ` · ${e.label}` : ''}: ${byId.get(e.source)?.label} → ${byId.get(e.target)?.label}`, type: 'smoothstep', animated: byId.get(e.target)?.status === 'running', label,
      style: { stroke: color, strokeWidth: e.kind === 'flow' ? 1.5 : 2, strokeDasharray: planned ? '5 5' : e.kind === 'reroute' ? '9 4' : e.kind === 'steer' ? '3 3' : undefined },
      labelStyle: { fill: color, fontSize: 12, fontWeight: 500 }, labelShowBg: true, labelBgPadding: [4, 2], labelBgBorderRadius: 4, labelBgStyle: { fill: '#0c1420' }, markerEnd: { type: MarkerType.ArrowClosed, color }, focusable: false };
  });
}
function Canvas({ flow, selected, choose }: { flow: Narrative['flow']; selected: string | null; choose: (id: string) => void }) {
  const rf = useReactFlow();
  const width = useStore(s => s.width), height = useStore(s => s.height);
  // The whole analysis must be visible: fit on first render, on new steps and on every canvas resize.
  // On short screens a full fit would shrink text below reading size; then keep a readable zoom, top-aligned, and let the operator pan.
  const shape = flow.nodes.map(n => `${n.id}:${n.depth}:${n.lane}`).join('|');
  useEffect(() => {
    if (!(width > 0 && height > 0 && flow.nodes.length)) return;
    const t = requestAnimationFrame(() => void rf.fitView({ padding: .06, maxZoom: 1 }).then(() => {
      if (rf.getZoom() >= MIN_READABLE) return;
      const gw = Math.max(...layoutFlow(flow.nodes).map(p => p.x)) + FLOW_W;
      void rf.setViewport({ x: Math.max(8, (width - gw * MIN_READABLE) / 2), y: 12, zoom: MIN_READABLE });
    }));
    return () => cancelAnimationFrame(t);
  }, [shape, width, height, rf]);
  const converted = useMemo(() => {
    const positions = new Map(layoutFlow(flow.nodes).map(p => [p.id, p]));
    const nodes: Node[] = flow.nodes.map(n => ({ id: n.id, type: 'step', position: positions.get(n.id)!, width: FLOW_W, height: FLOW_H, data: { node: n, selected: n.id === selected, choose }, style: { width: FLOW_W, height: FLOW_H }, focusable: false, draggable: false, selectable: false }));
    return { nodes, edges: analysisFlowEdges(flow, positions) };
  }, [flow, selected, choose]);
  return <div className="flow-canvas" aria-label="실시간 에이전트 흐름도"><ReactFlow nodes={converted.nodes} edges={converted.edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: .06, maxZoom: 1 }} minZoom={.15} maxZoom={1.5} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null} ariaLabelConfig={GRAPH_ARIA} proOptions={{ hideAttribution: true }}><Background gap={20} size={1} color="#28384d" /></ReactFlow>
    <GraphControls label="흐름"><span>실선 진행</span><span className="warn-text">주황 승격</span><span className="reroute-text">보라 경로 변경</span><span className="accent-text">파란 점선 지시</span><span className="ok-text">초록 검증</span></GraphControls></div>;
}
/** Vertical analysis flow (signal at the top, outcome at the bottom). Every node is a button: the keyboard entry to each step. */
export function FlowGraph({ flow, selected, onSelect }: { flow: Narrative['flow']; selected: string | null; onSelect: (id: string | null) => void }) {
  if (flow.nodes.length === 0) return <div className="flow-canvas flow-empty" role="status"><span className="flow-empty-icon"><DiagramIcon kind="group" /></span><strong>아직 기록된 흐름이 없습니다.</strong><span>분석 단계가 기록되면 여기에 표시됩니다.</span></div>;
  return <ReactFlowProvider><Canvas flow={flow} selected={selected} choose={onSelect} /></ReactFlowProvider>;
}
