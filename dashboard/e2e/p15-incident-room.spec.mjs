import { test, expect } from '@playwright/test';
import { capture, errors, evidenceTab, layout, noPageScroll, resetMock, tab } from './p15-helpers.mjs';
test.beforeEach(async({request})=>resetMock(request));
const ready = page => expect(page.locator('.incident-room')).toBeVisible({timeout:10000});
test('표본을 고정하지 않고 API에서 단순·복잡·고위험·진행 중 관제실을 확인한다',async({page,request},info)=>{
 test.setTimeout(90000);const es=errors(page);const {items}=await(await request.get('/api/incidents?limit=200')).json();
 const selected=[items.find(i=>['open','analyzing','acting','action_pending'].includes(i.status)),...['COMPLEX','SIMPLE','HIGH_RISK'].map(l=>items.find(i=>i.severity_level===l)),items.find(i=>i.status==='resolved')].filter(Boolean);const cases=[...new Map(selected.map(i=>[i.id,i])).values()];expect(cases.length).toBeGreaterThan(0);
 for(const i of cases){
  const response=await request.get(`/api/incidents/${i.id}/narrative`);expect(response.ok()).toBe(true);const n=await response.json();
  await page.goto(`/incidents/${i.id}`);await ready(page);await expect(page.getByRole('heading',{name:'장애 관제실',exact:true})).toBeAttached();await expect(page.getByLabel('장애 상황 요약')).toContainText(n.headline.what);
  // 흐름: 모든 단계가 노드 버튼이며 depth가 클수록 아래에 있다.
  await tab(page,'흐름');await expect(page.getByRole('heading',{name:'AI 분석 흐름',exact:true})).toBeVisible();
  await expect(page.locator('.flow-step button')).toHaveCount(n.flow.nodes.length);
  if(n.flow.nodes.length){const tops=await page.locator('.react-flow__node').evaluateAll(es=>es.map(e=>({id:e.dataset.id,y:e.getBoundingClientRect().top})));const y=new Map(tops.map(t=>[t.id,t.y]));for(const a of n.flow.nodes)for(const b of n.flow.nodes)if(a.depth<b.depth)expect(y.get(a.id),`${a.id}→${b.id}`).toBeLessThan(y.get(b.id));}
  await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-flow`);
  // 근거: 한글 해석이 먼저, 원문은 접힌 채 시작한다. 펼친 원문은 API 원문과 같다.
  await evidenceTab(page);
  if(n.raw.length){
   const g=page.locator('.evidence-group').first();await expect(g.locator('.ko-reading')).toBeVisible();await expect(g.locator('.raw-item')).toHaveCount(0);
   const order=await g.evaluate(e=>[...e.children].map(c=>c.className));expect(order.indexOf('ko-reading')).toBeLessThan(order.findIndex(c=>c.includes('raw-toggle')));
   await g.getByText(/^영어 원문 보기/).click();const ids=(await g.getAttribute('data-raw-ids')).split(' ');await expect(g.locator('.raw-item')).toHaveCount(ids.length);
   for(const [k,id] of ids.entries())await expect(g.locator('.raw-item .raw-text').nth(k)).toHaveText(n.raw.find(r=>r.id===id).raw);
   expect(await page.locator('.evidence-group').count()).toBeLessThanOrEqual(n.raw.length);
  }
  await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-evidence`);
  const filter=page.getByRole('button',{name:'오류',exact:true});await filter.focus();await page.keyboard.press('Enter');await expect(filter).toHaveAttribute('aria-pressed','true');await page.getByRole('button',{name:'전체',exact:true}).click();
  // 흐름 단계 선택 → 단계 상세와 그 단계의 근거만 표시, 입력 근거로 이동.
  const ix=n.flow.nodes.findIndex(x=>x.role&&x.inputs?.some(ref=>n.raw.some(r=>r.ref===ref||r.id===ref)));const index=ix>=0?ix:n.flow.nodes.findIndex(x=>x.role);
  if(index>=0){
   await tab(page,'흐름');const control=page.locator('.flow-step button').nth(index);await control.focus();await page.keyboard.press('Enter');
   const detail=page.getByLabel('선택한 에이전트 상세');await expect(detail).toBeVisible();await expect(detail).toContainText('입력 / 출력 토큰');
   await tab(page,'흐름');await expect(control).toHaveAttribute('aria-pressed','true');await expect.poll(()=>page.locator('.flow-step.selected').evaluate(e=>{const a=e.getBoundingClientRect(),b=e.closest('.react-flow').getBoundingClientRect();return a.left>=b.left-1&&a.right<=b.right+1&&a.top>=b.top-1&&a.bottom<=b.bottom+1;})).toBe(true);
   await evidenceTab(page);await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-step`);
   const refs=detail.locator('.ref-link:not(.missing)');if(await refs.count()){await refs.first().click();await expect(page.locator('.evidence-group.highlighted').first()).toBeVisible();await expect(page.locator('.evidence-group.highlighted .raw-item').first()).toBeVisible();await layout(page);}
   await detail.getByRole('button',{name:'상세 닫기'}).click();await expect(detail).toHaveCount(0);
  }
  await tab(page,'대화');await expect(page.getByRole('textbox',{name:'운영자 메시지'})).toBeVisible();await expect(page.getByRole('textbox',{name:'운영자 메시지'})).toHaveAttribute('maxlength',['resolved','handed_off','failed'].includes(i.status)?'8000':'2000');await expect(page.getByRole('button',{name:'보내기',exact:true})).toBeDisabled();await layout(page);await capture(page,info,`room-${i.severity_level}-${i.status}-chat`);
 }
 expect(es).toEqual([]);
});
test('데스크톱 관제실은 페이지 스크롤 없이 세로 흐름 전체와 대화를 함께 보여 준다',async({page,request},info)=>{
 test.skip(info.project.name!=='width-1440','데스크톱 배치 기준');
 const {items}=await(await request.get('/api/incidents?limit=200')).json();const i=items.find(i=>i.severity_level==='COMPLEX')??items[0];test.skip(!i,'장애 기록 없음');
 await page.setViewportSize({width:1440,height:900});await page.goto(`/incidents/${i.id}`);await ready(page);await expect(page.locator('.flow-step').first()).toBeVisible();
 await noPageScroll(page);for(const pane of ['AI 분석 흐름','운영자 대화'])await expect(page.getByRole('region',{name:pane})).toBeVisible();
 await expect.poll(()=>page.locator('.flow-step').evaluateAll(es=>{const f=document.querySelector('.flow-canvas .react-flow').getBoundingClientRect();return es.every(e=>{const a=e.getBoundingClientRect();return a.left>=f.left-1&&a.right<=f.right+1&&a.top>=f.top-1&&a.bottom<=f.bottom+1;});})).toBe(true);
 await expect(page.getByRole('textbox',{name:'운영자 메시지'})).toBeInViewport();await layout(page);await capture(page,info,'room-desktop-1440x900');
});
test('화면 전환·필터·흐름 노드의 키보드 접근과 모바일 대화 복귀',async({page,request})=>{
 const {items}=await(await request.get('/api/incidents?limit=200')).json();const i=items.find(i=>i.status==='resolved')??items[0];test.skip(!i,'장애 기록 없음');await page.goto(`/incidents/${i.id}`);await ready(page);
 const tabs=page.getByRole('navigation',{name:'관제실 화면 전환'});if(await tabs.isVisible()){const b=tabs.getByRole('button',{name:'대화',exact:true});await b.focus();await page.keyboard.press('Enter');await expect(b).toHaveAttribute('aria-pressed','true');}
 await tab(page,'흐름');const graph=page.locator('.flow-canvas .react-flow__viewport');await expect(page.locator('.flow-step').first()).toBeVisible();const before=await graph.getAttribute('style');await page.getByRole('button',{name:'흐름 확대',exact:true}).click();await expect(graph).not.toHaveAttribute('style',before);
 const nodes=page.locator('.flow-step button');if(await nodes.count()){await nodes.first().focus();await page.keyboard.press('Enter');await expect(page.getByLabel('선택한 에이전트 상세')).toBeVisible();}
 await tab(page,'대화');await page.getByRole('textbox',{name:'운영자 메시지'}).focus();await expect(page.getByRole('textbox',{name:'운영자 메시지'})).toBeFocused();await layout(page);
});
