import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, resetMock } from './p15-helpers.mjs';
test.skip(!mock, '다중 환경 계약 표본은 모의 API에서 검증합니다.');
test.beforeEach(async ({ request }) => resetMock(request));
const allInside = page => page.locator('.resource-button').evaluateAll(es => {
  const f = document.querySelector('.topology-canvas .react-flow').getBoundingClientRect();
  return es.every(e => { const r = e.getBoundingClientRect(); return r.left >= f.left - 1 && r.right <= f.right + 1 && r.top >= f.top - 1 && r.bottom <= f.bottom + 1; });
});
test('환경 선택·시스템 여섯 묶음·장애 위치·관제실 이동은 세 화면 폭에서 유지된다', async ({ page }, info) => {
  if (info.project.name === 'width-1440') await page.setViewportSize({ width: 1440, height: 900 });
  const es = errors(page); await page.goto('/');
  const scope = page.getByRole('combobox', { name: '지도 환경' });
  await expect(scope).toHaveValue('prd');
  await expect(scope.locator('option')).toHaveText(['dev', 'stg', 'prd', '전체 환경']);
  await expect(page.locator('.system-box')).toHaveCount(6);
  await expect(page.locator('.resource-button')).toHaveCount(7);
  await expect(page.locator('.system-box').first()).toContainText('order · 2개 서비스');
  await expect(page.getByLabel('환경별 관측 요약')).toContainText('진행 중 장애 1건');
  await expect.poll(() => allInside(page)).toBe(true);
  await layout(page); await capture(page, info, 'p18-envs-prd');
  const before = await page.locator('.react-flow__viewport').getAttribute('style');
  await page.getByRole('button', { name: '지도 확대', exact: true }).click();
  await expect(page.locator('.react-flow__viewport')).not.toHaveAttribute('style', before);
  for (const env of ['dev', 'stg']) {
    await scope.selectOption(env);
    await expect(page.locator('.system-box')).toHaveCount(6);
    await expect(page.locator('.incident-pin')).toHaveCount(0);
    expect(await page.locator('.react-flow__node-resource').evaluateAll(es => es.every(e => e.dataset.id.startsWith(`${document.querySelector('[aria-label="지도 환경"]').value}/`)))).toBe(true);
    await expect.poll(() => allInside(page)).toBe(true); await layout(page);
  }
  await scope.selectOption('');
  await expect(page.locator('.environment-box')).toHaveCount(3);
  await expect(page.locator('.resource-button')).toHaveCount(21);
  await expect(page.locator('.resource-top small').first()).toHaveText('catalog');
  await expect.poll(() => allInside(page)).toBe(true); await layout(page); await capture(page, info, 'p18-envs-all');
  await scope.selectOption('prd');
  await expect(page.locator('.resource-place')).toHaveText('prd · order · inventory');
  await page.getByRole('button', { name: '진행 중 장애 1건', exact: true }).click();
  await expect(page.locator('.alert-item')).toContainText('prd · order · inventory');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'inventory 장애 1976 관제실 열기', exact: true }).click();
  await expect(page).toHaveURL(/\/incidents\/1976$/);
  await expect(page.locator('.headline-place')).toHaveText('prd · order · inventory');
  await layout(page); await capture(page, info, 'p18-envs-room'); expect(es).toEqual([]);
});
test('장애 목록의 환경 필터는 현재 페이지 범위임을 알린다', async ({ page }, info) => {
  await page.goto('/incidents');
  const scope = page.getByRole('combobox', { name: '장애 환경 필터' });
  await expect(scope.locator('option')).toHaveText(['전체 환경', 'dev', 'stg', 'prd']);
  await scope.selectOption('prd');
  await expect(page.locator('.incident-row')).toHaveCount(1);
  await expect(page.locator('.incident-row')).toContainText('prd · order · inventory');
  await expect(page.getByRole('status')).toContainText('현재 페이지');
  await scope.selectOption('dev'); await expect(page.locator('.incident-list')).toContainText('현재 페이지에서 일치하는 장애가 없습니다');
  await layout(page); await capture(page, info, 'p18-envs-list');
});
