// P17 운영자 종료: 모의 API에서만 상태를 바꾼다. 실배포에서는 건너뛴다(실제 장애 기록 보호).
import { test, expect } from '@playwright/test';
import { capture, layout, mock, resetMock, tab } from './p15-helpers.mjs';

test.beforeEach(async ({ request }) => { await resetMock(request); });

test('인계 장애를 사유와 부작용 확인 후 종료하면 기록만 남고 지시를 받지 않는다', async ({ page }, info) => {
  test.skip(!mock, '실제 장애 상태를 바꾸므로 모의 API에서만 실행');
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '장애 종료' });
  await expect(card).toContainText('클러스터는 변경하지 않습니다');
  await expect(card.getByRole('button', { name: '장애 종료' })).toBeDisabled();
  await card.getByRole('textbox').fill('대상 정상 확인, 메모리 한도 변경 불필요');
  await card.scrollIntoViewIfNeeded(); await layout(page); await capture(page, info, 'p17-close-ready');
  let asked = '';
  page.once('dialog', d => { asked = d.message(); void d.dismiss(); });
  await card.getByRole('button', { name: '장애 종료' }).click();
  expect(asked).toContain('정말 진행하시겠습니까?'); expect(asked).toContain('클러스터는 변경하지 않습니다');
  await expect(card).toBeVisible(); // 취소하면 아무것도 바뀌지 않는다
  page.once('dialog', d => void d.accept());
  await card.getByRole('button', { name: '장애 종료' }).click();
  const record = page.getByRole('article', { name: '운영자 종료 기록' });
  await expect(record).toContainText('대상 정상 확인, 메모리 한도 변경 불필요');
  await expect(page.getByRole('article', { name: '장애 종료' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toHaveCount(0);
  await expect(page.locator('.headline')).toContainText('운영자 종료');
  await layout(page); await capture(page, info, 'p17-closed');
});

test('승인 대기 조치가 남아 있으면 종료 버튼을 막고 이유를 보여 준다', async ({ page, request }) => {
  test.skip(!mock, '모의 API 전용');
  await request.post('/api/mock/pending-action', { headers: { 'X-LiveProbe-Client': 'dashboard' }, data: { incident_id: 990 } });
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '장애 종료' });
  await card.getByRole('textbox').fill('정리');
  await expect(card).toContainText('승인 대기 중인 조치가 있습니다');
  await expect(card.getByRole('button', { name: '장애 종료' })).toBeDisabled();
});

test('진행 중이거나 자동 복구된 장애에는 종료 카드가 없다', async ({ page }) => {
  test.skip(!mock, '모의 API 전용');
  for (const id of [1976, 971]) {
    await page.goto(`/incidents/${id}`); await tab(page, '대화');
    await expect(page.getByRole('textbox', { name: '운영자 메시지' })).toBeVisible();
    await expect(page.getByRole('article', { name: '장애 종료' })).toHaveCount(0);
  }
});
