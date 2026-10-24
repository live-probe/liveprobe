import { expect } from '@playwright/test';
import { mkdirSync, appendFileSync, readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
const sourceFiles = ['index.html','vite.config.ts',...readdirSync('src').filter(f => /\.(ts|tsx|css)$/.test(f)).sort().map(f => `src/${f}`)];
const sourceHash = createHash('sha256').update(sourceFiles.map(f => `${f}\n${readFileSync(f,'utf8')}`).join('\n')).digest('hex');
export const mock = process.env.LIVEPROBE_MOCK === '1';
// 각 개선 단계가 이전 배포의 캡처·검사 증거를 덮어쓰지 않도록 분리한다.
const artifactRoot = process.env.LIVEPROBE_EVIDENCE_ROOT ?? 'e2e/artifacts';
export async function resetMock(request) { if (mock) await request.post('/api/mock/reset', { headers: { 'X-LiveProbe-Client':'dashboard' } }); }
export async function tab(page, name) { await page.locator('.incident-room').waitFor(); const nav=page.getByRole('navigation',{name:'관제실 화면 전환'}); if(await nav.isVisible()) await nav.getByRole('button',{name,exact:true}).click(); }
/** 장애 관제실의 근거 탭 이름은 단계 선택 여부에 따라 바뀐다. */
export async function evidenceTab(page) { await page.locator('.incident-room').waitFor(); const nav=page.getByRole('navigation',{name:'관제실 화면 전환'}); if(await nav.isVisible()) await nav.getByRole('button',{name:/^(근거·해석|단계·근거)$/}).click(); }
/** 데스크톱 무스크롤 검사: 페이지(.main) 자체가 세로로 스크롤되지 않는다. */
export async function noPageScroll(page) { const m=await page.locator('.main').evaluate(e=>({sh:e.scrollHeight,ch:e.clientHeight})); expect(m.sh,JSON.stringify(m)).toBeLessThanOrEqual(m.ch+1); }
export async function capture(page, testInfo, name) {
  const dir = `${artifactRoot}/${mock ? 'mock' : 'live'}`;
  mkdirSync(dir,{recursive:true});
  const path=`${dir}/${name}-${testInfo.project.name}.png`;
  await page.screenshot({path,fullPage:true});
  appendFileSync(`${artifactRoot}/captures.jsonl`, JSON.stringify({ path, state:name, project:testInfo.project.name, viewport:page.viewportSize(), url:page.url(), at:new Date().toISOString(), source_sha256:sourceHash })+'\n');
}
export async function layout(page) {
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  const sizes=await page.locator('.main,.room-pane,.evidence-scroll,.chat-feed,.composer,.resource-detail,.agent-detail,.command-card,.decision-card,.ops-side,.side-scroll,.alert-panel').evaluateAll(es=>es.filter(e=>e.getBoundingClientRect().width>0).map(e=>({name:e.className,client:e.clientWidth,scroll:e.scrollWidth})));
  for(const s of sizes)expect(s.scroll,JSON.stringify(s)).toBeLessThanOrEqual(s.client+1);
  const overlaps=await page.evaluate(()=>{
    const out=[];
    for(const group of document.querySelectorAll('.composer form,.confirm-gate .inline,.room-tabs,.filters,.map-toolbar,.nav,.graph-toolbar,.bar-right,.side-tabs,.ops-side-head')){
      const es=[...group.querySelectorAll('button,input,select,textarea,a')].filter(e=>e.getBoundingClientRect().width>0);
      for(let i=0;i<es.length;i++)for(let j=i+1;j<es.length;j++){
        const a=es[i].getBoundingClientRect(),b=es[j].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)out.push([es[i].textContent,es[j].textContent]);
      }
    }
    for(const root of document.querySelectorAll('.topology-canvas,.flow-canvas')){
      const frame=root.querySelector('.react-flow')?.getBoundingClientRect();if(!frame||!frame.width)continue;
      for(const control of root.querySelectorAll('.graph-toolbar button'))for(const node of root.querySelectorAll('.resource-button,.flow-step button')){
        const a=control.getBoundingClientRect(),b=node.getBoundingClientRect();
        const left=Math.max(b.left,frame.left),right=Math.min(b.right,frame.right),top=Math.max(b.top,frame.top),bottom=Math.min(b.bottom,frame.bottom);
        if(Math.min(a.right,right)-Math.max(a.left,left)>1&&Math.min(a.bottom,bottom)-Math.max(a.top,top)>1)out.push(['그래프 제어와 노드',control.getAttribute('aria-label'),node.getAttribute('aria-label')]);
      }
    }
    // 지도 연결선 문구는 자원 카드 뒤에 가려지면 안 된다(카드가 SVG 연결선 위에 그려진다).
    for(const label of document.querySelectorAll('.topology-canvas .react-flow__edge-textwrapper'))for(const node of document.querySelectorAll('.topology-canvas .resource-button')){
      const a=label.getBoundingClientRect(),b=node.getBoundingClientRect();
      if(a.width&&Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)out.push(['연결선 문구가 자원에 가려짐',label.textContent,node.getAttribute('aria-label')]);
    }
    return out;
  });expect(overlaps).toEqual([]);
  const clipped=await page.locator('.btn,.filter,.room-tabs button,.side-tabs button,.alert-button,.cited-by button,.graph-toolbar button').evaluateAll(es=>es.filter(e=>e.getBoundingClientRect().width>0 && (e.scrollWidth>e.clientWidth+2||e.scrollHeight>e.clientHeight+2)).map(e=>({text:e.textContent,w:e.clientWidth,sw:e.scrollWidth,h:e.clientHeight,sh:e.scrollHeight})));
  expect(clipped).toEqual([]);
  mkdirSync(artifactRoot,{recursive:true});
  appendFileSync(`${artifactRoot}/layout.jsonl`, JSON.stringify({ url:page.url(), viewport:page.viewportSize(), source_sha256:sourceHash, document:await page.evaluate(()=>({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})), panels:sizes, overlaps, clipped })+'\n');
}
export function errors(page) { const es=[];page.on('pageerror',e=>es.push(e.message));return es; }
