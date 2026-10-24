import { test } from 'node:test';
import assert from 'node:assert/strict';
import { changePollMs, nextStage, promotionConfirmText, changeStatusKo, safePrUrl, type ChangeRequest, type PromotionPlan } from '../src/changes.ts';
const cr = (status: string): ChangeRequest => ({ id: 8, incident_id: 990, system: 'payment', service: 'payment', namespace_origin: 'prd', status, patch_summary_ko: '', diff: '', tests: {}, build: {}, stages: {}, error: null });
test('승격 순서는 개발·검증·운영이며 중간 실패를 건너뛰지 않는다', () => {
  assert.equal(nextStage(cr('built')), 'dev'); assert.equal(nextStage(cr('dev_verified')), 'stg');
  assert.equal(nextStage(cr('stg_verified')), 'prd');
  for (const status of ['dev_failed','stg_failed','prd_verified','cancelled','building']) assert.equal(nextStage(cr(status)), null);
});
test('단계 진행은 완료·현재·실패를 구분하고 실패 뒤를 완료로 꾸미지 않는다', async () => {
  const { changeProgress } = await import('../src/changes.ts');
  assert.equal(changeProgress(cr('built')).filter(s=>s.state==='완료').length,3);
  const failed=changeProgress(cr('dev_failed'));
  assert.equal(failed[3].state,'실패'); assert.equal(failed[4].state,'대기');
});
test('진행 중에만 2초 간격으로 조회한다', () => {
  assert.equal(changePollMs([cr('building')]), 2000); assert.equal(changePollMs([cr('dev_deploying')]), 2000);
  assert.equal(changePollMs([cr('built')]), 10000); assert.equal(changePollMs([cr('prd_verified')]), 10000); assert.equal(changePollMs([]), 10000);
});
test('승격 확인에는 부작용·원복 방법과 마지막 질문이 모두 들어간다', () => {
  const p: PromotionPlan = { stage:'dev', namespace:'dev', deployment:'payment', container:'payment', previous_image:'old',new_image:'new',side_effects_ko:['파드 재시작과 중단 가능'],rollback_ko:'old로 원복',plan_sha256:'a'.repeat(64) };
  const text = promotionConfirmText(p); assert.ok(text.includes(p.side_effects_ko[0])); assert.ok(text.includes(p.rollback_ko)); assert.ok(text.endsWith('정말 진행하시겠습니까?'));
});
test('표시용 상태는 한국어이며 PR 링크는 해당 비공개 저장소만 허용한다', () => {
  assert.equal(changeStatusKo('patch_failed'), '패치 생성 실패');
  assert.equal(safePrUrl({repository:'live-probe/payment',pr_number:4,pr_url:'https://github.com/live-probe/payment/pull/4'}), 'https://github.com/live-probe/payment/pull/4');
  assert.equal(safePrUrl({repository:'live-probe/payment',pr_number:4,pr_url:'javascript:alert(1)'}), null);
});
