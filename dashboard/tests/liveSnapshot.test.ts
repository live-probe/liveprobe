import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadOpenIncidents, OPEN_STATES } from '../src/liveSnapshot.ts';
import type { Incident } from '../src/api.ts';
const row = (id: number, status: Incident['status'] = 'action_pending') => ({ id, status }) as Incident;

test('진행 중 네 상태를 모두 조회하고 500건 경계를 넘겨도 누락하지 않는다', async () => {
  const rows = Array.from({ length: 503 }, (_, i) => row(i + 1));
  const queried = new Set<string>();
  const out = await loadOpenIncidents(async path => {
    const q = new URL(path, 'http://local').searchParams;const status = q.get('status')!;queried.add(status);
    const items = status === 'action_pending' ? rows : [];const offset = Number(q.get('offset'));
    return { items: items.slice(offset, offset + 500), total: items.length };
  });
  assert.deepEqual([...queried].sort(), [...OPEN_STATES].sort());
  assert.equal(out.length, 503);assert.equal(out[0].id, 503);assert.equal(out.at(-1)?.id, 1);
});
test('조회 중 상태가 바뀌어 중복된 사건은 한 번만 표시하며 종료 상태는 제외한다', async () => {
  const out = await loadOpenIncidents(async () => ({ items: [row(5),row(5),row(2,'resolved')], total: 3 }));
  assert.deepEqual(out.map(x => x.id), [5]);
});
test('진행 상태 한 개라도 조회 실패하면 부분 목록으로 성공하지 않는다', async () => {
  await assert.rejects(loadOpenIncidents(async path => {
    if (path.includes('status=acting')) throw new Error('조회 실패');
    return { items: [row(5)], total: 1 };
  }), /조회 실패/);
});
test('총건수보다 적은 빈 페이지는 장애 없음이 아니라 불완전한 조회 오류다', async () => {
  await assert.rejects(loadOpenIncidents(async () => ({ items: [], total: 1 })), /일부만 조회/);
});
