import { test, expect } from '@playwright/test';
import { mkdirSync, appendFileSync, readFileSync, readdirSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { errors, layout, resetMock, tab } from './p15-helpers.mjs';

const mock = process.env.LIVEPROBE_MOCK === '1';
const sourceFiles = ['index.html', 'vite.config.ts', ...readdirSync('src').filter(f => /\.(ts|tsx|css)$/.test(f)).sort().map(f => `src/${f}`)];
const sourceHash = createHash('sha256').update(sourceFiles.map(f => `${f}\n${readFileSync(f, 'utf8')}`).join('\n')).digest('hex');
const dir = `${process.env.LIVEPROBE_EVIDENCE_ROOT ?? '../docs/evidence/P16'}/${process.env.P16_CAPTURE_SCOPE ?? (mock ? 'mock' : 'live')}`;
async function capture(page, info, name) {
  mkdirSync(dir, { recursive: true });
  const path = `${dir}/${name}-${info.project.name}.png`;
  await page.screenshot({ path, fullPage: true });
  appendFileSync(`${dir}/captures.jsonl`, JSON.stringify({ path, viewport: page.viewportSize(), url: page.url(), repository: realpathSync('..'), source_sha256: sourceHash, at: new Date().toISOString() }) + '\n');
}
async function incident(request) {
  const response = await request.get('/api/incidents?limit=200');
  expect(response.ok()).toBe(true);
  const { items } = await response.json();
  const row = items.find(i => i.severity_level === 'COMPLEX') ?? items[0];
  expect(row, '단계가 있는 실제 또는 계약 표본').toBeTruthy();
  return row;
}
async function readableTokens(page, selector) {
  const pairs = await page.locator(selector).evaluate(e => {
    const css = getComputedStyle(e);
    const lum = hex => hex.trim().slice(1).match(/../g).map(x => parseInt(x, 16) / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4).reduce((a, x, i) => a + x * [.2126, .7152, .0722][i], 0);
    return ['text', 'muted', 'meta', 'accent', 'ok', 'warn', 'danger', 'reroute'].flatMap(fg => ['bg', 'group', 'node', 'selected'].map(bg => {
      const a = lum(css.getPropertyValue(`--diagram-${fg}`)), b = lum(css.getPropertyValue(`--diagram-${bg}`));
      return { fg, bg, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) };
    }));
  });
  for (const pair of pairs) expect(pair.ratio, JSON.stringify(pair)).toBeGreaterThanOrEqual(4.5);
}
test.beforeEach(async ({ request }) => resetMock(request));

test('같은 자원 수라도 연결 변경으로 지도 배치가 바뀌면 전체 맞춤을 갱신한다', async ({ page, request }) => {
  test.skip(!mock, '연결 관측을 바꾸는 재현은 모의 API에서만 실행한다');
  // 연결 변경으로 열·행이 달라지는 구 응답도 계속 지원한다.
  const o = JSON.parse(readFileSync('e2e/fixtures/overview.json', 'utf8'));
  const ingress = o.topology.nodes.find(n => n.kind === 'Ingress');
  const targets = ['order', 'inventory', 'loadgen'].map(name => o.topology.nodes.find(n => n.name === name && n.kind === 'Deployment'));
  expect(ingress).toBeTruthy(); expect(targets.every(Boolean)).toBe(true);
  const seed = o.topology.observed_edges[0];
  o.topology.observed_edges = [ingress, ...targets.slice(0, -1)].map((source, i) => ({ ...seed, from: source.id, to: targets[i].id, flows: 1, dropped: 0, errors: 0, state: 'ok' }));
  let payload = o;
  await page.route('**/api/overview', route => route.fulfill({ json: payload }));
  await page.goto('/'); await page.locator('.resource-button').first().waitFor();
  const allInside = () => page.locator('.resource-button').evaluateAll(es => {
    const f = document.querySelector('.topology-canvas .react-flow').getBoundingClientRect();
    return es.every(e => { const r = e.getBoundingClientRect(); return r.left >= f.left - 1 && r.right <= f.right + 1 && r.top >= f.top - 1 && r.bottom <= f.bottom + 1; });
  });
  await expect.poll(allInside).toBe(true);
  const positions = () => page.locator('.react-flow__node-resource').evaluateAll(es => es.map(e => `${e.dataset.id}:${e.style.transform}`).join('|'));
  const beforePositions = await positions();
  const viewport = page.locator('.topology-canvas .react-flow__viewport');
  const beforeViewport = await viewport.getAttribute('style');
  const count = await page.locator('.resource-button').count();
  payload = structuredClone(o); payload.topology.observed_edges = [];
  await page.getByRole('button', { name: '새로고침', exact: true }).click();
  await expect.poll(positions).not.toBe(beforePositions);
  await expect(page.locator('.resource-button')).toHaveCount(count);
  await expect(viewport).not.toHaveAttribute('style', beforeViewport);
  await expect.poll(allInside).toBe(true);
  // 수치만 달라지는 갱신은 운영자가 조절한 시점을 되돌리지 않는다.
  const beforeZoom = await viewport.getAttribute('style');
  await page.getByRole('button', { name: '지도 확대', exact: true }).click();
  await expect(viewport).not.toHaveAttribute('style', beforeZoom);
  const zoomed = await viewport.getAttribute('style');
  payload = structuredClone(payload); payload.generated_at = new Date().toISOString();
  payload.workloads.forEach(w => { w.cpu = (w.cpu ?? 0) + .1; });
  const refreshed = page.waitForResponse(r => r.url().endsWith('/api/overview'));
  await page.getByRole('button', { name: '새로고침', exact: true }).click();
  await refreshed;
  await expect(viewport).toHaveAttribute('style', zoomed);
});

test('정교화한 서비스 지도는 원천 상태·선택·명도 위계와 확대 제어를 보존한다', async ({ page }, info) => {
  const es = errors(page);
  await page.goto('/');
  const nodes = page.locator('.topology-canvas .resource-button');
  await expect(nodes.first()).toBeVisible();
  await expect(page.locator('.resource-glyph svg')).toHaveCount(await nodes.count());
  await expect(page.locator('.namespace-heading')).toHaveCount(await page.locator('.namespace-box').count());
  await expect(page.locator('.map-connection-count')).toContainText('실측 연결');
  await expect(page.locator('.topology-canvas')).toHaveCSS('background-color', 'rgb(12, 20, 32)');
  await readableTokens(page, '.topology-canvas');
  await layout(page); await capture(page, info, 'map');
  const plain = nodes.filter({ hasNot: page.locator('.incident-pin') }).first();
  if (await plain.count()) {
    await plain.focus(); await page.keyboard.press('Enter');
    await expect(page.getByRole('complementary', { name: '자원 상세' })).toBeVisible();
    await expect(plain).toHaveAttribute('aria-pressed', 'true');
    await capture(page, info, 'map-selection');
    await page.getByRole('button', { name: '자원 상세 닫기' }).click();
  }
  await page.getByRole('button', { name: '지도 전체 보기', exact: true }).click();
  await layout(page);
  expect(es).toEqual([]);
});

test('정교화한 AI 흐름은 모든 단계·역할·선택·밝은 근거 영역을 보존한다', async ({ page, request }, info) => {
  const row = await incident(request);
  const response = await request.get(`/api/incidents/${row.id}/narrative`);
  expect(response.ok()).toBe(true);
  const n = await response.json(); const es = errors(page);
  await page.goto(`/incidents/${row.id}`); await tab(page, '흐름');
  const nodes = page.locator('.flow-step button');
  await expect(nodes).toHaveCount(n.flow.nodes.length);
  await expect(page.locator('.flow-canvas')).toHaveCSS('background-color', 'rgb(12, 20, 32)');
  await expect(page.locator('.flow-step svg')).toHaveCount(n.flow.nodes.length);
  await readableTokens(page, '.flow-canvas');
  await layout(page); await capture(page, info, 'flow');
  await nodes.first().focus(); await page.keyboard.press('Enter');
  await expect(page.getByLabel('선택한 에이전트 상세')).toBeVisible();
  await tab(page, '흐름');
  await expect(nodes.first()).toHaveAttribute('aria-pressed', 'true');
  await layout(page); await capture(page, info, 'flow-selection');
  await tab(page, '대화');
  await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toBeVisible();
  await layout(page);
  expect(es).toEqual([]);
});

test('움직임 감소 설정은 두 다이어그램의 장식 애니메이션을 끈다', async ({ page, request }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const row = await incident(request);
  for (const [url, selector] of [['/', '.resource-button'], [`/incidents/${row.id}`, '.flow-step']]) {
    await page.goto(url); await page.locator(selector).first().waitFor();
    const motion = await page.locator(selector).evaluateAll(es => es.map(e => ({ animation: getComputedStyle(e).animationName, transition: getComputedStyle(e).transitionDuration })));
    expect(motion.every(x => x.animation === 'none' && x.transition === '0s')).toBe(true);
  }
});
