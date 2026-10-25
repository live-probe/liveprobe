// 실제 GitHub 호출·배포 없이 변경 요청 카드의 확인 경계만 검증한다.
import { test, expect } from '@playwright/test';
import { capture, layout, mock, resetMock, tab } from './p15-helpers.mjs';
test.skip(!mock, '모의 API 전용');
test.beforeEach(async ({ request }) => resetMock(request));
test('변경 요청 카드와 승격 확인 취소·진행 및 PR 안내', async ({ page, request }, info) => {
  await request.post('/api/mock/change', { headers: {'X-LiveProbe-Client':'dashboard'}, data: {incident_id:990} });
  await page.goto('/incidents/990'); await tab(page, '대화');
  const card = page.getByRole('article', { name: '변경 요청 1', exact:true });
  await expect(card).toContainText('회귀 시험'); await expect(card).toContainText('GitHub에서 검토·병합');
  await card.getByText('패치 원문 보기').click();
  await expect(card.locator('pre')).toContainText('+++ b/tests/test_fix.py');
  let asked=''; page.once('dialog', async d=>{asked=d.message(); await d.dismiss();});
  await card.getByRole('button',{name:'dev로 승격',exact:true}).click();
  await expect.poll(()=>asked).toContain('정말 진행하시겠습니까?'); expect(asked).toContain('원복');
  expect((await (await request.get('/api/changes/1')).json()).status).toBe('built');
  page.once('dialog',d=>d.accept());
  await card.getByRole('button',{name:'dev로 승격',exact:true}).click();
  await expect(card).toContainText('개발 환경 검증 완료',{timeout:12000});
  await expect(card.getByRole('button',{name:'stg로 승격',exact:true})).toBeVisible();
  expect(await card.evaluate(e=>e.getBoundingClientRect().height)).toBeGreaterThan(250);
  await card.evaluate(e=>e.scrollIntoView({block:'start'})); await layout(page); await capture(page,info,'p18-change-dev-verified');
});
