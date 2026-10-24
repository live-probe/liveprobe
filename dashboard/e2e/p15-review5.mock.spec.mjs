import { test, expect } from '@playwright/test';
import { layout, mock, resetMock, tab } from './p15-helpers.mjs';
// 독립 검토(2026-10-21) 지적의 브라우저 회귀: 기존 자동 조치 승인·거절, 인계 인수, 늦은 목록 응답.
test.skip(!mock,'모의 API에서만 상태를 바꾼다.');
test.beforeEach(async({request})=>resetMock(request));
const H={headers:{'X-LiveProbe-Client':'dashboard'}};
async function pending(request){const {items}=await(await request.get('/api/incidents?limit=200')).json();const i=items.find(i=>i.severity_level!=='HIGH_RISK'&&i.status==='resolved')??items[0];const a=await(await request.post('/api/mock/pending-action',{...H,data:{incident_id:i.id}})).json();return {i,a};}
test('승인 대기 자동 조치는 관제실 대화에서 확인 후 승인할 수 있다',async({page,request})=>{
 const {i,a}=await pending(request);await page.goto(`/incidents/${i.id}`);await tab(page,'대화');
 const card=page.getByRole('article',{name:`조치 승인 요청 ${a.id}`});await expect(card).toBeVisible();await expect(card).toContainText(a.target);await expect(card).toContainText('replicas=2');await expect(card.getByText('매개변수 원문 보기')).toBeVisible();
 let asked='';page.once('dialog',d=>{asked=d.message();void d.accept();});await card.getByRole('button',{name:'승인',exact:true}).click();
 expect(asked).toContain('승인하시겠습니까');await expect(card).toHaveCount(0);await layout(page);
 const d=await(await request.get(`/api/incidents/${i.id}`)).json();expect(d.actions.find(x=>x.id===a.id).status).toBe('succeeded');
});
test('승인 확인을 취소하면 요청을 보내지 않고, 거절은 인계와 인수로 이어진다',async({page,request})=>{
 const {i,a}=await pending(request);let posts=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().includes('/api/actions/'))posts++;});
 await page.goto(`/incidents/${i.id}`);await tab(page,'대화');const card=page.getByRole('article',{name:`조치 승인 요청 ${a.id}`});
 page.once('dialog',d=>void d.dismiss());await card.getByRole('button',{name:'승인',exact:true}).click();await expect(card).toBeVisible();expect(posts).toBe(0);
 page.once('dialog',d=>void d.accept());await card.getByRole('button',{name:'거절',exact:true}).click();await expect(card).toHaveCount(0);expect(posts).toBe(1);
 const h=page.getByRole('article',{name:'운영자 인계'});await expect(h).toBeVisible({timeout:12000});await expect(h).toContainText('kubectl -n liveprobe-target get pods');
 await h.getByRole('button',{name:'인계 인수',exact:true}).click();await expect(h).toContainText('인수: operator');await layout(page);
});
test('필터를 바꾼 뒤 늦게 도착한 이전 목록 응답은 새 목록을 덮어쓰지 않는다',async({page})=>{
 const base={namespace:'liveprobe-target',title:'시험',severity_level:'NORMAL',signal_count:1,created_at:'2026-10-20T00:00:00Z',updated_at:'2026-10-20T00:00:00Z',closed_at:null,total_cost_usd:null,agent_run_count:0};
 const old={...base,id:321,workload:'이전-목록',status:'open'},fresh={...base,id:322,workload:'새-해결목록',status:'resolved'};
 let calls=0,held,saw,resolvedCalls=0;const waiting=new Promise(r=>saw=r);
 // 새 필터의 첫 응답 이후 조회는 보류해, 목록이 바뀔 수 있는 길을 늦은 이전 응답 하나로만 남긴다(재시도 단언의 5초 안에 다음 주기 조회가 끼어들지 않게).
 await page.route('**/api/incidents?*',async route=>{const q=new URL(route.request().url()).searchParams;if(q.get('limit')!=='50')return route.fallback();if(q.get('status')==='resolved'){if(++resolvedCalls>1)return;return route.fulfill({json:{items:[fresh],total:1}});}if(++calls===2){held=route;saw();return;}return route.fulfill({json:{items:[old],total:1}});});
 await page.goto('/incidents');await expect(page.getByText('이전-목록',{exact:true})).toBeVisible();await waiting;
 await page.getByRole('combobox',{name:'상태 필터'}).selectOption('resolved');await expect(page.getByText('새-해결목록',{exact:true})).toBeVisible();
 await held.fulfill({json:{items:[old],total:1}}).catch(()=>undefined);await page.waitForTimeout(1500);
 const text=await page.locator('.incident-list').textContent();expect(text).toContain('새-해결목록');expect(text).not.toContain('이전-목록');
});
