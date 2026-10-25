import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ReactFlowProvider } from '@xyflow/react';
import ts from 'typescript';
import type { FlowNode } from '../src/api.ts';

// Exercise the real TSX without a production build or a new test/runtime dependency.
const src = new URL('../src/', import.meta.url).href;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith('.') && context.parentURL?.startsWith(src) && !/\.[a-z]+$/i.test(specifier)) {
      for (const ext of ['.tsx', '.ts']) {
        const url = new URL(`${specifier}${ext}`, context.parentURL);
        try { readFileSync(url); return { url: url.href, shortCircuit: true }; } catch { /* Try the other source extension. */ }
      }
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url.endsWith('.css')) return { format: 'module', source: 'export {};', shortCircuit: true };
    if (url.startsWith(src) && /\.tsx?$/.test(url)) {
      // SSR has no canvas measurements. Expose the unchanged private node renderer only in this test loader.
      const original = readFileSync(fileURLToPath(url), 'utf8');
      const input = original + (url.endsWith('/FlowGraph.tsx') ? `\nexport { StageNode${original.includes('function analysisFlowEdges(') ? ', analysisFlowEdges' : ''} };` : '');
      const source = ts.transpileModule(input, { compilerOptions: { module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
      return { format: 'module', source, shortCircuit: true };
    }
    return next(url, context);
  },
});
type FlowModule = typeof import('../src/FlowGraph.tsx') & { StageNode: ComponentType<{ id: string; data: { node: FlowNode; selected: boolean; choose: (id: string) => void } }>; analysisFlowEdges: (flow: { nodes: FlowNode[]; edges: import('../src/api.ts').FlowEdge[] }) => import('@xyflow/react').Edge[] };
const flowModule = await import('../src/FlowGraph.tsx') as FlowModule;
const { StageNode, FlowGraph } = flowModule;
const node = (extra: Partial<FlowNode> = {}): FlowNode => ({ id: 'a', type: 'agent', label: '분석', role: 'signal_analyst', role_ko: '신호 분석', job_ko: '', status: 'done', model: 'gpt-6.1-sol', effort: null, tokens: null, cost_usd: null, duration_ms: null, inputs: [], conclusion_ko: null, conclusion_raw: null, lane: 0, depth: 2, group: null, ...extra });
const render = (nodes: FlowNode[], selected: string | null = null) => renderToStaticMarkup(createElement(ReactFlowProvider, null, nodes.map(n => createElement(StageNode, { key: n.id, id: n.id, data: { node: n, selected: n.id === selected, choose() {} } }))));

test('역할 아이콘·동일 depth 번호·별도 상태·모델 하단을 실제 흐름 노드에 표시한다', () => {
  const html = render([node(), node({ id: 'b', lane: 1 })], 'a');
  assert.match(html, /class="flow-role-icon"/);
  assert.equal((html.match(/class="flow-depth"[^>]*>03</g) ?? []).length, 2, '병렬 노드는 실행 순번이 아니라 같은 깊이를 표시한다');
  assert.match(html, /class="flow-state done"/);
  assert.match(html, /class="flow-sub flow-model"[^>]*>gpt-6\.1-sol</);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /aria-label="[^"]*gpt-6\.1-sol[^"]*상세 보기"/);
});

test('연결 문구는 인접 깊이의 빈 공간에만 보이고 어두운 바탕·전체 접근 이름을 유지한다', () => {
  assert.equal(typeof flowModule.analysisFlowEdges, 'function', '흐름 선의 시각 계약을 검증할 변환이 필요하다');
  const nodes = [node({ id: 'a', depth: 0 }), node({ id: 'b', depth: 1 }), node({ id: 'c', depth: 1, lane: 1 }), node({ id: 'd', depth: 2 }), node({ id: 'e', depth: 3, lane: -1 })];
  const edges = flowModule.analysisFlowEdges({ nodes, edges: [
    { id: 'near', source: 'a', target: 'b', kind: 'verify', label: '검증 근거' },
    { id: 'parallel', source: 'b', target: 'c', kind: 'escalate', label: '병렬 승격' },
    { id: 'skip', source: 'a', target: 'd', kind: 'reroute', label: '분석 경로 변경' },
    { id: 'back', source: 'e', target: 'd', kind: 'steer', label: '지시 반영' },
  ] });
  assert.equal(edges[0].label, '검증 근거');
  assert.equal(edges[1].label, undefined);
  assert.equal(edges[2].label, undefined, '중간 노드를 지나는 연결 문구를 카드 뒤에 놓지 않는다');
  assert.match(edges[2].ariaLabel ?? '', /분석 경로 변경/);
  assert.equal(edges[3].label, '지시 반영');
  assert.equal(edges[3].sourceHandle, 'st');
  assert.equal(edges[3].targetHandle, 'tb');
  for (const edge of edges) {
    assert.equal(edge.labelBgStyle?.fill, '#0c1420');
    assert.ok(Number(edge.labelStyle?.fontSize) >= 12);
  }
});

test('빈 흐름은 같은 다이어그램 표면에서 아직 미기록임을 알린다', () => {
  const html = renderToStaticMarkup(createElement(FlowGraph, { flow: { nodes: [], edges: [] }, selected: null, onSelect() {} }));
  assert.match(html, /class="flow-canvas flow-empty"/);
  assert.match(html, /role="status"/);
  assert.match(html, /아직 기록된 흐름이 없습니다/);
  assert.doesNotMatch(html, /정상|복구 완료/);
});

test('완료된 운영자 인계는 복구 성공으로 바꾸지 않으며 미확인 상태도 그대로 알린다', () => {
  const html = render([node({ type: 'outcome', role: null, model: null, label: '운영자 인계', status: 'done' })]);
  assert.match(html, /type-outcome is-handoff/);
  assert.match(html, />운영자 인계</);
  assert.match(html, /class="flow-state done">완료/);
  assert.doesNotMatch(html, /자동 복구|복구 성공/);
  const unknown = render([node({ status: 'unobserved', model: null })]);
  assert.match(unknown, /상태 미확인/);
  assert.match(unknown, /모델 미기록/);
});

test('예정·실행·실패·건너뜀·승격과 모든 역할은 문구와 아이콘으로 구분한다', () => {
  for (const status of ['planned', 'running', 'failed', 'skipped', 'escalated']) {
    const html = render([node({ status })]);
    assert.match(html, new RegExp(`class="flow-state ${status}"`));
    assert.match(html, /예정|실행 중|실패|건너뜀|승격됨/);
  }
  for (const type of ['signal', 'evidence', 'router', 'group', 'agent', 'verify', 'action', 'outcome', 'steer', 'command', 'interpret']) {
    const html = render([node({ type })]);
    assert.match(html, /class="flow-role-icon"><svg[^>]*aria-hidden="true"/);
    assert.match(html, new RegExp(`type-${type}`));
  }
});

test('전용 카드 스타일은 완료의 녹색 상자·축소 본문을 피하고 선택과 포커스를 분리한다', () => {
  const css = readFileSync(new URL('../src/analysisFlow.css', import.meta.url), 'utf8');
  assert.match(css, /\.flow-canvas \.flow-step\.done\s*\{\s*border-color:\s*var\(--diagram-border\)/);
  assert.match(css, /\.flow-canvas \.flow-state\.done\s*\{\s*color:\s*var\(--diagram-ok\)/);
  assert.match(css, /\.flow-canvas \.flow-step > button:focus-visible\s*\{\s*outline:\s*3px/);
  assert.match(css, /\.flow-canvas \.flow-step\.selected\s*\{/);
  assert.match(css, /\.flow-canvas \.flow-model\s*\{\s*font-family:\s*var\(--mono\)/);
  assert.doesNotMatch(css, /font-size:\s*(?:[0-9]|1[01])px/);
});
