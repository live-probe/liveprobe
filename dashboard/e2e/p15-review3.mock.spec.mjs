import { test, expect } from '@playwright/test';
import { mock, resetMock, tab, layout } from './p15-helpers.mjs';
test.skip(!mock, '조회 경합과 서버 실패는 모의 API에서만 재현한다.');
test.beforeEach(async ({ request }) => resetMock(request));

test('같은 필터의 오래된 실패가 최신 목록 성공 뒤 오류 경고를 만들지 않는다', async ({ page, request }) => {
 const {items}=await(await request.get('/api/incidents?limit=1')).json();const base=items[0];
 let calls=0,held,announce;const waiting=new Promise(r=>announce=r);
 await page.route('**/api/incidents?*',async route=>{
  if(new URL(route.request().url()).searchParams.get('limit')!=='50')return route.fallback();
  const call=++calls;if(call===2){held=route;announce();return;}
  return route.fulfill({json:{items:[{...base,id:320+call,workload:call===1?'초기 목록':'최신 목록'}],total:1}});
 });
 await page.goto('/incidents');await expect(page.getByText('초기 목록',{exact:true})).toBeVisible();await waiting;
 await expect(page.getByText('최신 목록',{exact:true})).toBeVisible({timeout:7000});
 await held.fulfill({status:503,json:{detail:'이전 주기 요청 실패'}});await page.waitForTimeout(600);
 expect(await page.getByRole('alert').count()).toBe(0);await expect(page.locator('.incident-list')).toContainText('최신 목록');
});

test('혼합 연결의 숨긴 문구는 자원 상세에서 요청률과 전체 흐름·차단·오류를 모두 보존한다',async({page,request})=>{
 const o=await(await request.get('/api/overview')).json();
 o.workloads=[];o.incidents.open=[];o.topology={generated_at:10000,nodes:['a','b'].map((id,j)=>({id,name:id,namespace:j?'other':'shop',kind:'Deployment',status:'ready'})),structure_edges:[],observed_edges:[{from:'a',to:'b',flows:26,ok:12,dropped:13,errors:1,req_rate:10,err_rate:0.1,last_seen:10000,state:'degraded',ports:{},drop_reasons:{},via_service:{}}],freshness:{state:'connected',stale:false,stale_after_s:60},network_alerts:[]};
 await page.route('**/api/overview',r=>r.fulfill({json:o}));await page.goto('/');await page.getByRole('combobox',{name:'지도 자원 선택'}).selectOption('a');
 const d=page.getByRole('complementary',{name:'자원 상세'});await expect(d).toBeVisible();await expect(page.locator('.react-flow__edge-text')).toHaveCount(0);
 for(const value of ['10/초','오류 10%','흐름 26','차단 13','오류 1'])await expect(d).toContainText(value);await layout(page);
});

test('해석 조회 실패 중에도 최신 상세가 인계되면 인수 카드와 종료 후 지시 경로를 쓴다',async({page,request})=>{
 const {items}=await(await request.get('/api/incidents?limit=200')).json();const i=items.find(x=>x.status==='resolved');
 const a=await(await request.post('/api/mock/pending-action',{headers:{'X-LiveProbe-Client':'dashboard'},data:{incident_id:i.id}})).json();
 const d=await(await request.get(`/api/incidents/${i.id}`)).json();let changed=false;const sent=[];
 await page.route(`**/api/incidents/${i.id}/narrative`,r=>changed?r.fulfill({status:503,json:{detail:'해석 조회 실패'}}):r.fallback());
 await page.route(`**/api/incidents/${i.id}`,r=>changed?r.fulfill({json:{...d,incident:{...d.incident,status:'handed_off',handoff:{},updated_at:'2099-01-01T00:00:00Z'},actions:[]}}):r.fallback());
 await page.route(`**/api/actions/${a.id}/approve`,r=>r.fulfill({status:409,json:{detail:'이미 인계된 조치'}}));
 await page.goto(`/incidents/${i.id}`);await tab(page,'대화');const card=page.getByRole('article',{name:`조치 승인 요청 ${a.id}`});await expect(card).toBeVisible();
 changed=true;page.once('dialog',dialog=>void dialog.accept());await card.getByRole('button',{name:'승인',exact:true}).click();
 await expect(page.locator('.room-error')).toContainText('상황 정보');await expect(card).toHaveCount(0);
 await expect(page.getByRole('article',{name:'운영자 인계'})).toBeVisible();await expect(page.getByRole('textbox',{name:'운영자 메시지'})).toHaveAttribute('maxlength','8000');
 await expect(page.getByRole('button',{name:'일시정지',exact:true})).toHaveCount(0);await layout(page);
 await page.route(`**/api/incidents/${i.id}/messages`,r=>{sent.push('messages');return r.fulfill({json:{}});});
 await page.route(`**/api/incidents/${i.id}/commands`,r=>{if(r.request().method()==='POST'){sent.push('commands');return r.fulfill({status:201,json:{}});}return r.fallback();});
 await page.getByRole('textbox',{name:'운영자 메시지'}).fill('후속 상태를 확인하세요');await page.getByRole('button',{name:'보내기',exact:true}).click();await expect.poll(()=>sent).toEqual(['commands']);
});
