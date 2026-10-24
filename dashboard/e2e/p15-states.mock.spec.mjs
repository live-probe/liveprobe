import { test, expect } from '@playwright/test';
import { capture, evidenceTab, layout, mock, resetMock, tab } from './p15-helpers.mjs';
test.skip(!mock,'로컬 표본 상태 시험입니다.');
test.beforeEach(async({request})=>resetMock(request));
test('원천과 수치가 없으면 미관측으로 남고 빈 원문·흐름도 안전하게 보인다',async({page,request},info)=>{
 const o=await(await request.get('/api/overview')).json();o.cluster={nodes:[],namespaces:[]};o.workloads=[];o.events=[];o.incidents={open:[],today:{total:null,resolved:null,handed_off:null,failed:null}};o.topology=null;o.sources={};await page.route('**/api/overview',r=>r.fulfill({json:o}));await page.goto('/');await expect(page.getByRole('region',{name:'클러스터 건강도 요약'})).toContainText('미관측');await expect(page.locator('.source-chips .connected')).toHaveCount(0);await expect(page.getByText('토폴로지 미관측 · 연결이 없다는 뜻은 아닙니다.')).toBeVisible();await layout(page);await capture(page,info,'overview-empty');
 const {items}=await(await request.get('/api/incidents?limit=200')).json();const n=await(await request.get(`/api/incidents/${items[0].id}/narrative`)).json();n.raw=[];n.flow={nodes:[],edges:[]};n.interpreting=true;await page.route('**/api/incidents/*/narrative',r=>r.fulfill({json:n}));await page.goto(`/incidents/${items[0].id}`);await evidenceTab(page);await expect(page.getByText('이 종류의 원문이 없습니다.')).toBeVisible();await tab(page,'흐름');await expect(page.getByText('아직 기록된 흐름이 없습니다.')).toBeVisible();await layout(page);
});
test('알림 조회 실패를 0건으로 꾸미지 않고 마지막 목록을 유지하다 복구한다',async({page,request})=>{
 const {items}=await(await request.get('/api/incidents?status=analyzing&limit=500')).json();const active=items[0];let failed=false;
 await page.route('**/api/incidents?*',r=>new URL(r.request().url()).searchParams.has('status')&&failed?r.fulfill({status:503,json:{detail:'모의 조회 실패'}}):r.continue());
 await page.goto('/');await page.getByRole('button',{name:'진행 중 장애 1건',exact:true}).click();const panel=page.getByRole('region',{name:'진행 중 장애 목록'});await expect(panel.getByRole('link',{name:`${active.workload} 장애 ${active.id} 관제실 열기`})).toBeVisible();
 failed=true;await expect(page.getByRole('button',{name:'진행 중 장애 조회 실패',exact:true})).toBeVisible({timeout:10000});await expect(panel).toContainText('마지막 조회 기준');await expect(panel.getByRole('link',{name:`${active.workload} 장애 ${active.id} 관제실 열기`})).toBeVisible();await expect(panel.getByText('진행 중인 장애가 없습니다.',{exact:true})).toHaveCount(0);
 failed=false;await expect(page.getByRole('button',{name:'진행 중 장애 1건',exact:true})).toBeVisible({timeout:10000});
});
test('첫 조회 실패는 재시도로 복구한다',async({page},info)=>{
 let reads=0;await page.route('**/api/overview',async r=>{if(++reads===1)await r.fulfill({status:503,json:{detail:'모의 오류'}});else await r.continue();});await page.goto('/');await expect(page.getByRole('alert')).toContainText('불러오지 못했습니다');await layout(page);await capture(page,info,'overview-error');await page.getByRole('button',{name:'다시 불러오기',exact:true}).click();await expect(page.getByRole('heading',{name:'서비스 연결 지도',exact:true})).toBeVisible();await layout(page);
});
test('실패한 사전 검사나 누락된 부작용은 진행 버튼을 막는다',async({page,request},info)=>{
 const {items}=await(await request.get('/api/incidents?limit=200')).json();const i=items.find(i=>i.status==='handed_off');const template=await(await request.post(`/api/incidents/${i.id}/commands`,{headers:{'X-LiveProbe-Client':'dashboard'},data:{instruction:'수량 조정'}})).json();const c={...template.command,status:'awaiting_confirmation',side_effects:null,dry_run:[{op_index:0,ok:false,diff:[],error:'사전 검사 거부'}]};await page.route('**/api/incidents/*/commands',r=>r.fulfill({json:{items:[c]}}));await page.goto(`/incidents/${i.id}`);await tab(page,'대화');await expect(page.getByRole('button',{name:'진행',exact:true})).toBeDisabled();await expect(page.locator('.command-card')).toContainText('모든 작업의 사전 검사 통과');await page.getByRole('button',{name:'진행',exact:true}).scrollIntoViewIfNeeded();await layout(page);await capture(page,info,'command-blocked');
});
