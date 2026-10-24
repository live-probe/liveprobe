import './graphControls.css';
import type { ReactNode } from 'react';
import { Handle, Position, useReactFlow, type AriaLabelConfig } from '@xyflow/react';
/** Zoom controls sit in their own row under the canvas so they never cover nodes; the legend shares that row. */
export function GraphControls({ label, children }: { label: string; children?: ReactNode }) {
  const rf = useReactFlow();
  return <div className="graph-toolbar" aria-label={`${label} 조작`}><button type="button" aria-label={`${label} 확대`} onClick={() => void rf.zoomIn()}>＋</button><button type="button" aria-label={`${label} 축소`} onClick={() => void rf.zoomOut()}>−</button><button type="button" aria-label={`${label} 전체 보기`} onClick={() => void rf.fitView({ padding: .06, maxZoom: 1 })}>전체 보기</button><div className="graph-legend">{children}</div></div>;
}
export const GRAPH_ARIA: Partial<AriaLabelConfig> = {
  'node.a11yDescription.default': '엔터 키로 단계 상세를 엽니다.', 'node.a11yDescription.keyboardDisabled': '엔터 키로 상세를 엽니다.',
  'edge.a11yDescription.default': '단계 사이의 연결입니다.', 'controls.ariaLabel': '지도 조작', 'controls.zoomIn.ariaLabel': '확대',
  'controls.zoomOut.ariaLabel': '축소', 'controls.fitView.ariaLabel': '전체 보기', 'controls.interactive.ariaLabel': '지도 조작 전환', 'handle.ariaLabel': '연결점',
};
export function FacingHandles() {
  return <>{[['sr', 'source', Position.Right], ['sl', 'source', Position.Left], ['sb', 'source', Position.Bottom], ['st', 'source', Position.Top], ['tl', 'target', Position.Left], ['tr', 'target', Position.Right], ['tt', 'target', Position.Top], ['tb', 'target', Position.Bottom]].map(([id, type, position]) => <Handle key={id} id={id} type={type as 'source' | 'target'} position={position as Position} isConnectable={false} style={{ opacity: 0 }} />)}</>;
}
/** Bring a focused node into view without changing zoom, only when keyboard focus lands outside the visible canvas. */
export function revealOnFocus(el: HTMLElement, rf: ReturnType<typeof useReactFlow>, id: string, w: number, h: number, offset = { x: 0, y: 0 }) {
  const frame = el.closest('.react-flow')?.getBoundingClientRect(); const r = el.getBoundingClientRect();
  if (!frame || (r.left >= frame.left && r.right <= frame.right && r.top >= frame.top && r.bottom <= frame.bottom)) return;
  const p = rf.getNode(id)?.position; if (p) void rf.setCenter(p.x + offset.x + w / 2, p.y + offset.y + h / 2, { zoom: rf.getZoom() });
}
