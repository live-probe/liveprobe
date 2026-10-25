// 독립 검토 후 추가된 단계 표시: 실행 전 중단(변경 없음)과 원복 미확정을 실제 화면에서 구분한다.
import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, resetMock, tab } from './p15-helpers.mjs';
test.skip(!mock, '모의 API 전용');
test.beforeEach(async ({ request }) => resetMock(request));
const H = { headers: { 'X-LiveProbe-Client': 'dashboard' } };

test('운영 승격이 실행 전에 중단되면 실패·원복이 아니라 변경 없음으로 보이고 다시 승격할 수 있다', async ({ page, request }, info) => {
  const pageErrors = errors(page);
  await request.post('/api/mock/change', { ...H, data: { incident_id: 990, fixture: 'prd_blocked' } });
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '변경 요청 1', exact: true });
  const prd = card.getByRole('region', { name: 'prd 검증 결과' });
  await expect(prd.getByRole('heading')).toHaveText('prd 단계 · 실행 전 중단 · 변경 없음');
  await expect(prd).toContainText('변경이 없어 원복하지 않음');
  await expect(prd).not.toContainText('검증 실패');
  await expect(prd).not.toContainText('원복 미확정');
  await expect(prd.getByRole('alert')).toContainText('GitHub 병합 확인이 만료되어 실행 전에 중단했습니다');
  await expect(prd.getByRole('alert')).toHaveClass(/notice/);
  // 같은 사유를 카드 아래에 '요청 실패'로 반복하지 않는다.
  await expect(card.getByText('요청 실패', { exact: false })).toHaveCount(0);
  await expect(card.getByText('GitHub 병합 확인이 만료되어', { exact: false })).toHaveCount(1);
  // 좁은 폭에서도 항목 이름이 한 글자씩 끊기지 않는다.
  for (const dt of await prd.locator('dt').all()) { const h = await dt.evaluate(e => e.getBoundingClientRect().height); expect(h).toBeLessThan(26); }
  // 상태 배지는 실패(위험)가 아니라 마지막 검증 단계다.
  await expect(card.getByRole('status')).toHaveText('검증 환경 검증 완료');
  await expect(card.getByRole('status')).not.toHaveClass(/danger/);
  // 다시 승격 가능: 확인창을 닫으면 아무것도 바뀌지 않는다.
  const again = card.getByRole('button', { name: 'prd로 승격', exact: true });
  await expect(again).toBeEnabled();
  let asked = ''; page.once('dialog', async d => { asked = d.message(); await d.dismiss(); });
  await again.click();
  await expect.poll(() => asked).toContain('정말 진행하시겠습니까?');
  expect((await (await request.get('/api/changes/1')).json()).status).toBe('stg_verified');
  // 키보드로 버튼에 닿고 초점이 보인다.
  await again.focus(); await expect(again).toBeFocused();
  const box = await prd.boundingBox(); expect(box && box.width).toBeGreaterThan(200);
  await prd.scrollIntoViewIfNeeded(); await layout(page); await capture(page, info, 'p18-change-prd-blocked');
  expect(pageErrors).toEqual([]);
});

test('운영 검증 실패 뒤 원복을 확정하지 못하면 사람 확인 필요로 보인다', async ({ page, request }, info) => {
  const pageErrors = errors(page);
  await request.post('/api/mock/change', { ...H, data: { incident_id: 990, fixture: 'prd_failed_unconfirmed' } });
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '변경 요청 1', exact: true });
  const prd = card.getByRole('region', { name: 'prd 검증 결과' });
  await expect(prd.getByRole('heading')).toHaveText('prd 단계 · 검증 실패');
  await expect(prd).toContainText('원복 미확정 · 사람 확인 필요');
  await expect(prd).toContainText('파드 준비실패');
  await expect(prd.getByRole('alert')).toHaveClass(/error/);
  await expect(card.getByText('단계 검증 실패', { exact: true })).toHaveCount(1);
  // 성공한 dev·stg 단계를 원복 미확정으로 잘못 표시하지 않는다.
  for (const s of ['dev', 'stg']) await expect(card.getByRole('region', { name: `${s} 검증 결과` })).toContainText('자동 원복해당 없음');
  await expect(card.getByRole('status')).toHaveClass(/danger/);
  await expect(card.getByRole('button', { name: 'prd로 승격', exact: true })).toHaveCount(0);
  await prd.scrollIntoViewIfNeeded(); await layout(page); await capture(page, info, 'p18-change-prd-failed-unconfirmed');
  expect(pageErrors).toEqual([]);
});
