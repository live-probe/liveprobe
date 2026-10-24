import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterRaw, citedByNode, matchesRef, layoutFlow, narrativePollMs, flowStatusKo, stageKo } from '../src/narrative.ts';
import { commandControls, commandPollMs, downtimeKo, reversibleKo, secretKeys, secretSizeError, rollbackReviewKo, commandErrorKo } from '../src/commands.ts';
import { sumObserved, numberKo, errorRateKo, overviewKpis, groupWorkloads, incidentFor, overviewMap, handlesFor } from '../src/overviewModel.ts';
import { roleKo, levelKo } from '../src/labels.ts';
import type { Command, FlowNode, Overview, RawItem, Workload } from '../src/api.ts';
const raw = (id: string, kind = 'event', severity = 'error'): RawItem => ({ id, ref: id.split('#')[0], kind, severity, source: 'kubernetes', at: null, raw: 'Error: ImagePullBackOff\n', ko: '이미지 내려받기 실패', ko_source: 'rules', cited_by: ['signal_analyst'] });
const node = (extra = {}): FlowNode => ({ id: 'a', type: 'agent', label: '분석', role: 'signal_analyst', role_ko: '신호 분석', job_ko: '신호를 읽습니다.', status: 'done', model: null, effort: null, tokens: null, cost_usd: null, duration_ms: null, inputs: ['E5'], conclusion_ko: null, conclusion_raw: null, lane: 0, depth: 0, group: null, ...extra });
const command = (status = 'awaiting_confirmation'): Command => ({ id: 1, incident_id: 1, instruction: '수정', status, plan: { summary_ko: '수정', operations: [{ op: 'scale', kind: 'Deployment', namespace: 'ns', name: 'web' }] }, plan_sha256: 'hash', dry_run: [{ op_index: 0, ok: true, diff: [], error: null }], side_effects: { items_ko: [], targets: [], restarts: false, downtime: 'none', reversible: true, rollback_ko: '이전 수량', cluster_wide: false, warnings_ko: [] }, confirm_question: '정말 진행하시겠습니까?', result: null, verification: null, rollback: null, error: null, created_at: '', updated_at: '' });
const overview = (): Overview => ({ generated_at: null, cluster: { nodes: [{ name: 'n', ready: true, cpu_cores: 4, cpu_used_cores: null, mem_bytes: 1024, mem_used_bytes: 512, pods: 2 }], namespaces: [{ name: 'ns', pods_running: 2, pods_pending: null, pods_failed: 0, restarts_1h: 3, cpu_cores: null, mem_bytes: null, workloads: 1, workloads_unhealthy: null }] }, workloads: [], events: [], incidents: { open: [{ id: 1, title: '장애', namespace: 'ns', workload: 'web', severity_level: 'COMPLEX', status: 'analyzing', stage: null, created_at: '' }], today: { total: 1, resolved: 0, handed_off: 0, failed: 0 } }, topology: null, sources: {} });
test('오류율은 비율에서 백분율로 바꾸고 미관측을 보존한다', () => {
 assert.equal(errorRateKo(.125), '12.5%'); assert.equal(errorRateKo(0), '0%'); assert.equal(errorRateKo(1), '100%');
 for (const v of [null, undefined, NaN, Infinity]) assert.equal(errorRateKo(v), '미관측');
});
test('원문 필터는 문자열을 변경하지 않으며 오류에는 경고도 포함한다', () => {
 const xs = [raw('E5#0'), raw('E7#0', 'log', 'warn'), raw('E2#0', 'status', 'info'), raw('agent:1', 'agent', 'info')];
 assert.equal(filterRaw(xs, 'all'), xs); assert.equal(filterRaw(xs, 'error').length, 2); assert.equal(filterRaw(xs, 'log')[0].raw, xs[1].raw); assert.equal(filterRaw(xs, 'status').length, 1); assert.equal(filterRaw(xs, 'agent').length, 1);
});
test('입력 근거와 인용 역할은 선택 Agent와 연결하되 유사 접두사는 연결하지 않는다', () => {
 assert.equal(matchesRef(raw('E5#0'), 'E5'), true); assert.equal(matchesRef(raw('E5#0'), 'E5#1'), false); assert.equal(matchesRef(raw('E10#0'), 'E1'), false);
 assert.equal(citedByNode(raw('E5#0'), node()), true); assert.equal(citedByNode(raw('E9#0'), node()), true); assert.equal(citedByNode(raw('E9#0'), node({ role: 'other' })), false); assert.equal(citedByNode(raw('E5#0'), null), false);
});
test('흐름 배치는 depth(세로)와 lane(가로)을 보존하고 지시용 음수 lane도 겹치지 않는다', () => {
 const ps = layoutFlow([node(), node({ id: 'b', depth: 1 }), node({ id: 'c', lane: -1 }), node({ id: 'd', lane: 1 })]);
 assert.ok(ps[1].y > ps[0].y); assert.ok(ps[2].x < ps[0].x); assert.ok(ps[3].x > ps[0].x); assert.equal(new Set(ps.map(p => `${p.x},${p.y}`)).size, 4);
});
test('진행·종료 조회 간격과 한글 상태', () => { assert.equal(narrativePollMs('analyzing'), 2000); assert.equal(narrativePollMs('handed_off'), 10000); assert.equal(flowStatusKo('escalated'), '상위 모델 승격'); assert.equal(stageKo('verifying'), '결과 확인 중'); assert.equal(roleKo('evidence_curator'), '근거 정리'); assert.equal(levelKo('HIGH_RISK'), '고위험'); });
test('명령 상태별 제어 및 확인 선행 조건', () => {
 const c = command(); assert.equal(commandControls(c).confirmEnabled, true); assert.equal(commandControls({ ...c, plan_sha256: null }).confirmEnabled, false); assert.equal(commandControls({ ...c, side_effects: null }).confirmEnabled, false); assert.equal(commandControls({ ...c, dry_run: [] }).confirmEnabled, false); assert.equal(commandControls({ ...c, dry_run: [{ op_index: 0, ok: false, error: '거부', diff: [] }] }).confirmEnabled, false);
 for (const s of ['planning', 'awaiting_confirmation', 'awaiting_secret']) assert.equal(commandControls(command(s)).cancel, true);
 for (const s of ['executing', 'failed', 'cancelled', 'rejected', 'rolling_back', 'rolled_back']) { const ctl = commandControls(command(s)); assert.equal(ctl.confirm, false); assert.equal(ctl.rollback, false); }
 assert.equal(commandControls(command('succeeded')).rollback, true); assert.equal(commandControls(command('awaiting_secret')).secret, true);
});
test('비밀키는 중복 제거하며 상태 조회와 부작용 문구는 안전하게 표시한다', () => {
 assert.deepEqual(secretKeys([{ op: 'secret_update', kind: 'Secret', name: 'a', keys: ['A', 'B'] }, { op: 'secret_update', kind: 'Secret', name: 'b', keys: ['A'] }]), ['A', 'B']); assert.equal(commandPollMs([command('succeeded')]), 10000); assert.equal(commandPollMs([command('planning')]), 2000); assert.equal(downtimeKo('possible'), '중단 가능성 있음'); assert.equal(reversibleKo(false), '원복 불가');
});
test('미관측 숫자는 0이 아니고 부분 합계도 미관측이다', () => { assert.equal(numberKo(null), '미관측'); assert.equal(numberKo(0), '0'); assert.equal(sumObserved([1, null]), null); assert.equal(sumObserved([]), null); const k = overviewKpis(overview()); assert.equal(k.cpuUsed, null); assert.equal(k.cpuCapacity, 4); assert.equal(k.pending, null); assert.equal(k.failed, 0); assert.equal(k.levels.COMPLEX, 1); });
test('네임스페이스 묶음과 장애 핀은 이름뿐 아니라 네임스페이스도 일치해야 한다', () => {
 const w = (namespace: string, name: string) => ({ namespace, name } as Workload);
 assert.deepEqual(groupWorkloads([w('b', 'c'), w('a', 'z'), w('a', 'a')]).map(g => [g.namespace, g.items.map(w => w.name)]), [['a', ['a', 'z']], ['b', ['c']]]);
 const o = overview(); assert.equal(incidentFor(o, { id: 'ns/Deployment/web', namespace: 'ns', name: 'web' })?.id, 1); assert.equal(incidentFor(o, { id: 'other/Deployment/web', namespace: 'other', name: 'web' }), null); assert.deepEqual(overviewMap(o).nodes, []);
});
test('간선은 항상 서로 마주 보는 면에 붙는다', () => { assert.deepEqual(handlesFor({ x: 0, y: 0 }, { x: 1, y: 0 }), { sourceHandle: 'sr', targetHandle: 'tl' }); assert.deepEqual(handlesFor({ x: 1, y: 0 }, { x: 0, y: 0 }), { sourceHandle: 'sl', targetHandle: 'tr' }); assert.equal(handlesFor({ x: 0, y: 1 }, { x: 0, y: 0 }).sourceHandle, 'st'); });

test('비밀값 크기는 서버와 같은 UTF-8 바이트 기준(키당 2KiB, 본문 40KiB)으로 미리 검사한다', () => {
 assert.equal(secretSizeError({ a: 'x'.repeat(2048) }, 'h'), null);
 assert.match(secretSizeError({ a: '가'.repeat(683) }, 'h') ?? '', /a/);
 const many = Object.fromEntries(Array.from({ length: 25 }, (_, i) => [`k${i}`, 'x'.repeat(2000)]));
 assert.match(secretSizeError(many, 'h') ?? '', /40/);
});
test('원복이 승인 필드만 되돌리고 미승인 변경이 남았으면 검토 필요로 안내한다', () => {
 const c = command('failed');
 assert.equal(rollbackReviewKo(c), null);
 assert.match(rollbackReviewKo({ ...c, rollback: { requires_review: true, verified: false, approved_fields_restored: true } }) ?? '', /검토/);
 assert.match(rollbackReviewKo({ ...c, error: 'rollback_unreviewed_admission_drift', rollback: { requires_review: true, verified: false } }) ?? '', /원복 완료가 아닙니다/);
});

test('재시작 합계는 워크로드가 없는 네임스페이스의 미관측 값 때문에 전체가 미관측이 되지 않는다', () => {
 const o = overview();
 o.cluster.namespaces = [...o.cluster.namespaces, { name: 'default', pods_running: 0, pods_pending: 0, pods_failed: 0, restarts_1h: null, cpu_cores: null, mem_bytes: null, workloads: 0, workloads_unhealthy: 0 }];
 assert.equal(overviewKpis(o).restarts, 3);
 o.cluster.namespaces[0] = { ...o.cluster.namespaces[0], restarts_1h: null };
 assert.equal(overviewKpis(o).restarts, null);
});

test('명령 오류 코드는 한글 사유로 풀어 쓰고 모르는 코드는 원문 확인을 안내한다', () => {
 assert.match(commandErrorKo('plan_has_no_operations'), /대상/);
 assert.match(commandErrorKo('unreviewed_storage_resource'), /저장소/);
 assert.match(commandErrorKo('rollback_unreviewed_admission_drift'), /승인하지 않은/);
 assert.match(commandErrorKo('kubernetes_error:409'), /충돌/);
 assert.match(commandErrorKo('something_new'), /원문/);
});
