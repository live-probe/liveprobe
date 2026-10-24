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
test('실행 전 중단된 단계는 실패·원복으로 꾸미지 않고 변경 없음으로 표시한다', async () => {
  const { stageHeadingKo, rollbackKo } = await import('../src/changes.ts');
  const blocked = { status:'blocked', reverted:false, rollback:{status:'미수행'}, error:'GitHub 병합 확인이 만료되어 실행 전에 중단했습니다.' };
  assert.equal(stageHeadingKo(blocked), '실행 전 중단 · 변경 없음');
  assert.equal(rollbackKo(blocked), '변경이 없어 원복하지 않음');
  assert.equal(rollbackKo({ status:'failed', reverted:false, rollback:{status:'미확정'} }), '원복 미확정 · 사람 확인 필요');
  assert.equal(rollbackKo({ status:'failed', reverted:true, rollback:{status:'통과'} }), '이전 이미지로 원복·검증 완료');
  assert.equal(rollbackKo({ status:'verified', reverted:false }), '해당 없음');
  // 서버는 성공 단계에도 rollback:{}을 남긴다. 빈 객체를 원복 시도로 오인하면 안 된다.
  assert.equal(rollbackKo({ status:'verified', reverted:false, rollback:{} }), '해당 없음');
  assert.equal(rollbackKo({ status:'executing', reverted:false, rollback:{} }), '해당 없음');
  // 재시작 복구로 실패 처리된 단계는 원복 기록이 없어도 사람 확인이 필요하다.
  assert.equal(rollbackKo({ status:'failed', reverted:false }), '원복 미확정 · 사람 확인 필요');
  // 중단 뒤에는 이전 단계로 돌아가 다시 승격할 수 있다.
  const c = cr('stg_verified'); c.stages = { prd: blocked };
  assert.equal(nextStage(c), 'prd');
});
test('같은 오류를 단계와 카드에 두 번 쓰지 않고, 실행 전 중단을 요청 실패로 부르지 않는다', async () => {
  const { cardErrorKo } = await import('../src/changes.ts');
  const message = 'GitHub 병합 확인이 만료되어 실행 전에 중단했습니다.';
  const blocked = cr('stg_verified'); blocked.error = message; blocked.stages = { prd: { status:'blocked', error: message } };
  assert.equal(cardErrorKo(blocked), null);
  const failed = cr('prd_failed'); failed.error = '단계 검증 실패'; failed.stages = { prd: { status:'failed', error:'단계 검증 실패' } };
  assert.equal(cardErrorKo(failed), null);
  const other = cr('patch_failed'); other.error = 'patch_generation_failed';
  assert.equal(cardErrorKo(other), '요청 실패: patch_generation_failed');
  assert.equal(cardErrorKo(cr('built')), null);
});
