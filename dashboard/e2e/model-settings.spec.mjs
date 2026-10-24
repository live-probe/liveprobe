import { test, expect } from '@playwright/test';

// Isolated browser contract tests; deployment is exercised separately with real persistence.
const role = (model = 'gpt-6.1-sol', effort = 'medium') => ({ model, effort });
const groups = {
  SIMPLE: { primary_mode: 'router', roles: { signal_analyst: role('gpt-6-luna', 'low'), result_verifier: role() } },
  NORMAL: { primary_mode: 'router', roles: { coordinator: role(), k8s_analyst: role('gpt-6-astra', 'high'), log_summarizer: role('gpt-6-luna', 'low'), group_verifier: role() } },
  COMPLEX: { primary_mode: 'router', roles: { k8s_log_analyst: role(), cause_reasoner: role('gpt-6-astra', 'high'), challenger: role('gpt-6-astra', 'high'), recovery_planner: role(), group_verifier: role() } },
  HIGH_RISK: { primary_mode: 'router', roles: { evidence_curator: role(), safety_verifier: role('gpt-6-astra', 'xhigh') } },
};
const initial = { revision: 0, source: 'config', provider: 'codex_cli', editable: true, router: role('gpt-6-luna', 'low'), groups, defaults: { router: role('gpt-6-luna', 'low'), groups }, catalog: ['gpt-6-luna', 'gpt-6.1-sol', 'gpt-6-astra'].map(model => ({ model, efforts: ['low', 'medium', 'high', 'xhigh'] })), limits: { model_ladder: ['gpt-6-luna', 'gpt-6.1-sol', 'gpt-6-astra'], max_cost_usd: { NORMAL: 1.3, COMPLEX: 2.3 } } };

test('group settings preserve draft across groups and save explicit payload', async ({ page }) => {
  let state = structuredClone(initial); let writes = [];
  await page.route('**/api/model-settings', async route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON(); writes.push(body);
      state = { ...state, ...body, revision: state.revision + 1, source: 'database' };
    }
    await route.fulfill({ json: state });
  });
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: '그룹별 모델' })).toBeVisible();
  await page.getByRole('button', { name: '보통 그룹', exact: true }).click();
  await page.getByLabel('로그 요약 추론 강도', { exact: true }).selectOption('medium');
  await page.getByRole('button', { name: '복잡 그룹', exact: true }).click();
  await page.getByRole('button', { name: '보통 그룹', exact: true }).click();
  await expect(page.getByLabel('로그 요약 추론 강도', { exact: true })).toHaveValue('medium');
  await page.getByRole('button', { name: '모델 설정 저장', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '버전 1' })).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].expected_revision).toBe(0);
  expect(writes[0].groups.NORMAL.roles.log_summarizer.effort).toBe('medium');
  await page.reload();
  await page.getByRole('button', { name: '보통 그룹', exact: true }).click();
  await expect(page.getByLabel('로그 요약 추론 강도', { exact: true })).toHaveValue('medium');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('mobile menu keeps settings and incident navigation identifiable', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.route('**/api/model-settings', route => route.fulfill({ json: initial }));
  await page.goto('/settings');
  await expect(page.getByRole('navigation', { name: '주 메뉴' }).getByRole('link', { name: '설정', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '주 메뉴' }).getByText('관제', { exact: true })).toBeVisible();
});

test('role rows keep workflow order even when database JSON keys are sorted', async ({ page }) => {
  const response = structuredClone(initial);
  for (const group of Object.values(response.groups)) group.roles = Object.fromEntries(Object.entries(group.roles).sort(([a], [b]) => a.localeCompare(b)));
  await page.route('**/api/model-settings', route => route.fulfill({ json: response }));
  await page.goto('/settings');
  await page.getByRole('button', { name: '복잡 그룹', exact: true }).click();
  await expect(page.locator('.model-role b')).toHaveText(['사건 분류', '쿠버네티스·로그 분석', '원인 추론', '반론 검토', '복구 계획', '독립 결과 확인']);
});

test('stale save preserves the draft and does not show success', async ({ page }) => {
  await page.route('**/api/model-settings', route => route.request().method() === 'PUT'
    ? route.fulfill({ status: 409, json: { detail: '다른 창에서 설정을 변경했습니다.' } }) : route.fulfill({ json: initial }));
  await page.goto('/settings');
  await page.getByRole('button', { name: '보통 그룹', exact: true }).click();
  await page.getByLabel('로그 요약 추론 강도', { exact: true }).selectOption('high');
  await page.getByRole('button', { name: '모델 설정 저장', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: '다른 창' })).toBeVisible();
  await expect(page.getByLabel('로그 요약 추론 강도', { exact: true })).toHaveValue('high');
  await expect(page.getByRole('button', { name: '모델 설정 저장', exact: true })).toBeEnabled();
});

test('load failure retries and switching primary mode enables a bounded model choice', async ({ page }) => {
  let reads = 0;
  await page.route('**/api/model-settings', route => ++reads === 1
    ? route.fulfill({ status: 503, json: { detail: '일시적으로 읽을 수 없습니다.' } }) : route.fulfill({ json: initial }));
  await page.goto('/settings');
  await expect(page.getByRole('alert').filter({ hasText: '일시적으로' })).toBeVisible();
  await page.getByRole('button', { name: '모델 설정 다시 불러오기', exact: true }).click();
  await expect(page.getByLabel('신호 분석 모델', { exact: true })).toBeDisabled();
  await page.getByLabel('주 모델 선택 방식', { exact: true }).selectOption('fixed');
  await expect(page.getByLabel('신호 분석 모델', { exact: true })).toBeEnabled();
  await page.getByLabel('신호 분석 모델', { exact: true }).selectOption('gpt-6.1-sol');
  await page.getByRole('button', { name: '고위험 그룹', exact: true }).click();
  await page.getByLabel('주 모델 선택 방식', { exact: true }).selectOption('fixed');
  await expect(page.getByLabel('근거 정리 모델', { exact: true })).toHaveValue('gpt-6-astra');
  await expect(page.getByLabel('안전 검증 모델', { exact: true }).locator('option')).toHaveCount(1);
  await expect(page.getByLabel('안전 검증 추론 강도', { exact: true }).locator('option')).toHaveCount(2);
  await page.getByRole('button', { name: '기본값 불러오기', exact: true }).click();
  await expect(page.getByLabel('주 모델 선택 방식', { exact: true })).toHaveValue('router');
  await expect(page.getByRole('button', { name: '모델 설정 저장', exact: true })).toBeDisabled();
});
