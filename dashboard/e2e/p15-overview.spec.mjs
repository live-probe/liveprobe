import { test, expect } from '@playwright/test';
import { capture, errors, layout, mock, noPageScroll, resetMock } from './p15-helpers.mjs';
test.beforeEach(async ({request})=>resetMock(request));
const ready = page => expect(page.locator('.topology-canvas .resource-button').first()).toBeVisible({timeout:10000});
test('운영 관제의 지도·보조 상태·원천·경고 원문과 장애 진입', async ({page,request},info)=>{
  const es=errors(page);const response=await request.get('/api/overview');expect(response.ok()).toBe(true);const data=await response.json();
  await page.goto('/');await ready(page);
  await expect(page.getByRole('heading',{name:'서비스 연결 지도'})).toBeVisible();await expect(page.getByRole('region',{name:'클러스터 건강도 요약'})).toBeVisible();await expect(page.getByRole('combobox',{name:'지도 네임스페이스'})).toBeVisible();
  expect(await page.locator('iframe').count()).toBe(0);await expect(page.getByLabel('데이터 원천 상태')).toBeVisible();
  // 운영 관제의 제목 영역과 큰 요약 카드는 없다. 화면 제목은 보조 기술에만 남긴다.
  await expect(page.locator('.page-head,.kpi-grid,.overview-right,.namespace-table')).toHaveCount(0);await expect(page.locator('h1.sr-only')).toHaveText('운영 관제');
  if(data.events?.length)await expect(page.locator('.warning-event .raw-text').first()).toHaveText(data.events[0].message);
  await layout(page);await capture(page,info,'overview');
  await page.getByRole('navigation',{name:'보조 정보 전환'}).getByRole('button',{name:/^네임스페이스/}).click();await expect(page.locator('.ns-row').first()).toBeVisible();await layout(page);await capture(page,info,'overview-namespaces');
  const target=data.incidents?.open?.[0];
  if(target){
    const node=page.locator(`.react-flow__node`).filter({has:page.getByRole('button',{name:new RegExp(`장애 ${target.id} 관제실 열기`)})});await expect(node.locator('.incident-pin')).toBeVisible();await node.getByRole('button').focus();await node.getByRole('button').click();await expect(page).toHaveURL(new RegExp(`/incidents/${target.id}$`));await expect(page.getByRole('heading',{name:'장애 관제실',exact:true})).toBeAttached();
  }
  expect(es).toEqual([]);
});
test('데스크톱 운영 관제는 페이지 스크롤 없이 지도가 첫 화면의 대부분을 차지한다',async({page},info)=>{
  test.skip(info.project.name!=='width-1440','데스크톱 배치 기준');
  for(const [width,height] of [[1440,900],[1280,800]]){
    await page.setViewportSize({width,height});await page.goto('/');await ready(page);await noPageScroll(page);
    const r=await page.locator('.topology-canvas').boundingBox();expect(r.y).toBeLessThanOrEqual(120);expect(r.width*r.height/(width*height),`${width}x${height}`).toBeGreaterThanOrEqual(.55);
    const nodes=await page.locator('.topology-canvas .resource-button').evaluateAll(es=>{const f=document.querySelector('.topology-canvas .react-flow').getBoundingClientRect();return es.map(e=>{const a=e.getBoundingClientRect();return a.left>=f.left-1&&a.right<=f.right+1&&a.top>=f.top-1&&a.bottom<=f.bottom+1;});});
    expect(nodes.every(Boolean),'처음 화면에서 모든 자원이 지도 안에 보인다').toBe(true);
    await layout(page);await capture(page,info,`overview-${width}x${height}`);
  }
});
test('우측 상단 알림 버튼은 진행 중 장애 수를 보여 주고 드롭다운에서 관제실로 이동한다',async({page,request},info)=>{
  const open=[];for(const state of ['open','analyzing','action_pending','acting']){let offset=0,total=1;while(offset<total){const r=await request.get(`/api/incidents?status=${state}&limit=100&offset=${offset}`);expect(r.ok()).toBe(true);const d=await r.json();total=d.total;expect(d.items.length>0||offset>=total).toBe(true);open.push(...d.items);offset+=d.items.length;}}
  await page.goto('/');const button=page.getByRole('button',{name:`진행 중 장애 ${open.length}건`});await expect(button).toBeVisible();await expect(button).toHaveAttribute('aria-expanded','false');
  await button.click();const panel=page.getByRole('region',{name:'진행 중 장애 목록'});await expect(panel).toBeVisible();await expect(panel.locator('.alert-item')).toHaveCount(open.length);await layout(page);await capture(page,info,'alert-menu');
  await page.keyboard.press('Escape');await expect(panel).toHaveCount(0);await expect(button).toBeFocused();
  await button.click();await page.locator('.topology-canvas').click({position:{x:5,y:5}});await expect(panel).toHaveCount(0);
  if(open.length){await button.click();await panel.getByRole('link',{name:new RegExp(`장애 ${open[0].id} 관제실 열기|장애 ${open.map(i=>i.id).join('|')}`)}).first().click();await expect(page).toHaveURL(/\/incidents\/\d+$/);await expect(page.getByRole('region',{name:'진행 중 장애 목록'})).toHaveCount(0);}
});
test('최근 200건 밖의 오래된 승인 대기 장애도 알림에서 빠지지 않는다',async({page,request})=>{
  test.skip(!mock,'모의 조회에서 과거 장애를 재현한다');
  const {items}=await(await request.get('/api/incidents?limit=200')).json();const base=items[0];
  const old={...base,id:14,workload:'old-inventory',status:'action_pending',created_at:'2026-01-01T00:00:00Z'};
  const rows=[...Array.from({length:230},(_,j)=>({...base,id:1000+j,status:'resolved'})),old];
  await page.route('**/api/incidents?*',r=>{const q=new URL(r.request().url()).searchParams;const state=q.get('status');const filtered=state?rows.filter(i=>i.status===state):rows;const offset=Number(q.get('offset')||0),limit=Number(q.get('limit')||50);return r.fulfill({json:{items:filtered.slice(offset,offset+limit),total:filtered.length}});});
  await page.goto('/');await page.getByRole('button',{name:'진행 중 장애 1건',exact:true}).click();
  await expect(page.getByRole('region',{name:'진행 중 장애 목록'}).getByRole('link',{name:'old-inventory 장애 14 관제실 열기'})).toBeVisible();
});
test('준비된 자원의 통신 경고는 정상과 구분하고 상세에 원천 근거를 보인다',async({page,request})=>{
  test.skip(!mock,'모의 관측 경고 재현');
  const o=await(await request.get('/api/overview')).json();const n=o.topology.nodes.find(n=>n.namespace==='liveprobe-target'&&n.kind==='Deployment'&&!(o.incidents.open??[]).some(i=>i.namespace===n.namespace&&i.workload===n.name));
  n.status='ready';n.network_warnings=[{peer:'sample-peer',dropped:0,errors:7}];
  await page.route('**/api/overview',r=>r.fulfill({json:o}));await page.goto('/');await ready(page);
  const b=page.getByRole('button',{name:`${n.name} 자원 상세 열기`,exact:true});await expect(b).toContainText('관측 경고');
  await page.getByRole('combobox',{name:'지도 자원 선택'}).selectOption(n.id);await expect(page.getByRole('complementary',{name:'자원 상세'})).toContainText('네트워크 오류 7');
});
test('정상 자원 상세는 보조 영역에서 서비스 이름·포트·엔드포인트와 원문을 유지한다',async({page,request},info)=>{
  const o=await(await request.get('/api/overview')).json();const candidates=(o.topology?.nodes??[]).filter(n=>!n.folded_into&&n.namespace&&n.kind!=='Node'&&!(o.workloads??[]).find(w=>w.id===n.id)?.incident&&!(o.incidents?.open??[]).some(i=>i.namespace===n.namespace&&i.workload===n.name));
  const n=candidates.find(n=>n.services?.length)??candidates[0];test.skip(!n,'선택할 자원 없음');await page.goto('/');await ready(page);await page.getByRole('combobox',{name:'지도 자원 선택'}).selectOption(n.id);const panel=page.getByRole('complementary',{name:'자원 상세'});await expect(panel).toBeVisible();await expect(panel).toContainText(n.name);for(const s of n.services??[]){await expect(panel).toContainText(s.name);await expect(panel).toContainText('엔드포인트');}
  await expect(panel.locator('details')).not.toHaveAttribute('open','');await panel.getByText('수집 필드 원문 보기').click();await expect(panel.locator('pre.raw-text')).toContainText(n.id);
  await panel.scrollIntoViewIfNeeded();await layout(page);await capture(page,info,'resource-detail');await panel.getByRole('button',{name:'자원 상세 닫기'}).click();await expect(panel).toHaveCount(0);
});
test('시스템 구성요소는 기본으로 숨기고 문제가 있으면 보이며 전환 버튼으로 모두 표시한다',async({page,request})=>{
  test.skip(!mock,'모의 응답을 변형한다');
  const o=await(await request.get('/api/overview')).json();
  o.topology.nodes.push({id:'kube-system/Deployment/coredns',kind:'Deployment',name:'coredns',namespace:'kube-system',status:'ready',role:'system'},{id:'kube-system/Deployment/metrics-server',kind:'Deployment',name:'metrics-server',namespace:'kube-system',status:'down',role:'system'});
  await page.route('**/api/overview',r=>r.fulfill({json:o}));await page.goto('/');await ready(page);
  const name=n=>page.locator('.react-flow__node').filter({has:page.getByRole('button',{name:new RegExp(`^${n} `)})});
  await expect(name('metrics-server')).toHaveCount(1);await expect(name('coredns')).toHaveCount(0);
  const toggle=page.getByRole('button',{name:'시스템 구성요소 1개 표시'});await toggle.click();await expect(name('coredns')).toHaveCount(1);await expect(page.getByRole('button',{name:'시스템 구성요소 숨기기'})).toHaveAttribute('aria-pressed','true');
  await page.getByRole('button',{name:'시스템 구성요소 숨기기'}).click();await page.getByRole('combobox',{name:'지도 네임스페이스'}).selectOption('kube-system');await expect(name('coredns')).toHaveCount(1);
});
test('이전 지도 주소는 운영 관제로 이동하고 유지 메뉴가 동작한다',async({page})=>{
  await page.goto('/map');await expect(page).toHaveURL(/\/$/);for(const [url,heading]of [['/incidents','장애 관제'],['/ledger','검토 기록'],['/settings','설정']]){await page.goto(url);await expect(page.getByRole('heading',{name:heading,exact:true})).toBeVisible();await layout(page);}
});
test('오류율 비율을 자원과 연결선에서 백분율로 표시한다', async ({page,request})=>{
  test.skip(process.env.LIVEPROBE_MOCK !== '1','실서비스 데이터는 변경하지 않는다');
  const data=await(await request.get('/api/overview')).json();
  for(const w of data.workloads??[])w.err_rate=0.125;
  for(const e of data.topology?.observed_edges??[])Object.assign(e,{req_rate:2,err_rate:0.25});
  await page.route('**/api/overview',route=>route.fulfill({json:data}));
  await page.goto('/');
  await expect(page.locator('.resource-metrics').filter({hasText:'오류 12.5%'}).first()).toBeAttached({timeout:3000});
  await expect(page.locator('.react-flow__edge-text').filter({hasText:'오류 25%'}).first()).toBeAttached({timeout:3000});
});

test('본문과 상태 토큰은 흰 바탕 및 상태 바탕에서 명도 대비를 확보한다',async({page})=>{
 await page.goto('/');const values=await page.evaluate(()=>{const c=getComputedStyle(document.documentElement);const lum=hex=>{const rgb=hex.trim().slice(1).match(/../g).map(x=>parseInt(x,16)/255).map(x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};return ['text','text-2','text-3','ok','warn','danger','accent'].map(n=>{const fg=lum(c.getPropertyValue('--'+n)),bg=lum(c.getPropertyValue('--'+n+'-bg')||'#ffffff');return{name:n,ratio:(Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};});});for(const v of values)expect(v.ratio,v.name).toBeGreaterThanOrEqual(4.5);
});
