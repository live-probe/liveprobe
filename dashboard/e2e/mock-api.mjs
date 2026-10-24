// 로컬 전용 계약 서버. 외부 접속·클러스터 변경·의존 패키지·요청 본문 기록 없음.
import http from 'node:http';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}.json`, import.meta.url), 'utf8'));
const clone = x => structuredClone(x); const now = () => new Date().toISOString();
const templates = fixture('command-plans');
let narratives, eventLogs, overview, commands, nextId, automationMode, modelSettings, actions;
const role = (model = 'gpt-6.1-sol', effort = 'medium') => ({ model, effort });
const groups = {
  SIMPLE: { primary_mode: 'router', roles: { signal_analyst: role('gpt-6-luna','low'), result_verifier: role() } },
  NORMAL: { primary_mode: 'router', roles: { k8s_analyst: role(), log_summarizer: role('gpt-6-luna','low'), coordinator: role(), group_verifier: role() } },
  COMPLEX: { primary_mode: 'router', roles: { k8s_log_analyst: role(), cause_reasoner: role(), challenger: role('gpt-6-astra','high'), recovery_planner: role(), group_verifier: role() } },
  HIGH_RISK: { primary_mode: 'router', roles: { evidence_curator: role('gpt-6-astra','high'), safety_verifier: role('gpt-6-astra','xhigh') } },
};
function reset() {
  narratives = new Map([971,987,990,976,1976].map(id => [id, fixture(`narrative-${id}`)]));
  eventLogs = new Map([...narratives.keys()].map(id => [id, fixture(`events-${id}`)]));
  overview = fixture('overview-envs'); commands = new Map(); nextId = 1; automationMode = 'approval'; actions = new Map();
  const draft = { router: role('gpt-6-luna','low'), groups: clone(groups) };
  modelSettings = { revision: 0, source: 'config', provider: 'codex_cli', editable: true, ...draft, defaults: clone(draft), catalog: ['gpt-6-luna','gpt-6.1-sol','gpt-6-astra'].map(model => ({ model, efforts: ['low','medium','high','xhigh'] })), limits: { model_ladder: ['gpt-6-luna','gpt-6.1-sol','gpt-6-astra'], max_cost_usd: { NORMAL:1.3,COMPLEX:2.3 } } };
}
reset();
function append(id, kind, summary, detail = {}) { const log = eventLogs.get(id); if (log) log.events.push({ seq: (log.events.at(-1)?.seq ?? 0)+1, kind, actor: 'liveprobe', summary, detail, at: now() }); }
function advance(c) {
  const elapsed = Date.now() - Date.parse(c.updated_at);
  if (c.status === 'planning' && elapsed > 500) { c.status = 'awaiting_confirmation'; c.updated_at = now(); append(c.incident_id,'command_planned','운영자 지시 계획 수립 완료',{command_id:c.id}); }
  if (['executing','rolling_back'].includes(c.status) && elapsed > 600) { const back = c.status === 'rolling_back'; c.status = back ? 'rolled_back' : 'succeeded'; c.result = { ok: true, summary_ko: '요청한 변경을 적용했습니다.' }; c.verification = { ok:true, summary_ko:'변경 후 상태를 확인했습니다.' }; if (back) c.rollback = { ok:true, summary_ko:'이전 상태로 되돌렸습니다.' }; c.updated_at=now(); append(c.incident_id,back?'command_rolled_back':'command_executed',back?'지시 원복 완료':'지시 실행 완료',{command_id:c.id}); }
  return c;
}
function createCommand(id, instruction) {
  const kind = /비밀|Secret|secret/.test(instruction) ? 'secret' : /노드|클러스터/.test(instruction) ? 'cluster' : 'scale';
  const plan = clone(templates[kind]);
  const hash = createHash('sha256').update(JSON.stringify(plan)).digest('hex');
  const c = { id: nextId++, incident_id:id, instruction, status:'planning', ...plan, plan_sha256:hash, confirm_question:'정말 진행하시겠습니까?', result:null, verification:null, rollback:null, error:null, created_at:now(), updated_at:now() };
  commands.set(c.id,c); return c;
}
const changes = new Map(); // P18 mock-only change state; no GitHub or cluster transport.
const server = http.createServer(async (req,res) => {
  const u = new URL(req.url,'http://127.0.0.1'); const path = u.pathname; const method = req.method;
  const send = (value, status=200) => { res.writeHead(status, {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(value)); };
  if (path === '/api/stream') { res.writeHead(200, {'Content-Type':'text/event-stream','Cache-Control':'no-cache','Connection':'keep-alive'}); res.write(': 로컬 연결 유지\n\n'); const t=setInterval(()=>res.write(': 연결 유지\n\n'),10000);req.on('close',()=>clearInterval(t));return; }
  let body={};
  if (!['GET','HEAD'].includes(method)) {
    if(req.headers['x-liveprobe-client'] !== 'dashboard') return send({detail:'클라이언트 헤더가 필요합니다.'},403);
    let text=''; for await (const chunk of req) { text+=chunk; if(text.length>64000) return send({detail:'본문이 너무 큽니다.'},413); }
    try { body=text?JSON.parse(text):{}; } catch { return send({detail:'요청 형식을 확인하세요.'},400); }
  }
  // P18 changes block: intentionally independent of overview fixtures.
  if(path==='/api/mock/reset' && method==='POST') changes.clear();
  if(path==='/api/mock/change' && method==='POST') {
    const id=changes.size+1;const c={id,incident_id:Number(body.incident_id),system:'payment',service:'payment',namespace_origin:'prd',status:'built',
      patch_summary_ko:'예외 처리와 회귀 시험을 추가했습니다.',diff:'--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-bad\n+good\n--- /dev/null\n+++ b/tests/test_fix.py\n@@ -0,0 +1 @@\n+assert True\n',tests:{total:1,failed:0},build:{image:`local/payment:cr-${id}`,repository:'live-probe/payment'},stages:{},error:null,created_at:now(),updated_at:now()};changes.set(id,c);return send(c,201);
  }
  const changeMatch=path.match(/^\/api\/changes(?:\/(\d+)(?:\/(plan|promote|cancel))?)?$/);
  if(changeMatch) {
    const advanceChange=c=>{if(c.status.endsWith('_deploying')&&Date.now()-Date.parse(c.updated_at)>500){const stage=c.status.split('_')[0];c.status=stage+'_verified';c.stages[stage]={...c.stages[stage],status:'verified',finished_at:now(),verification:{ok:true,rollout:{status:'통과'},health:{status:'통과'},repro:{status:'미수행'},exceptions:{status:'통과',count:0}},reverted:false};}return c;};
    if(!changeMatch[1])return send({items:[...changes.values()].filter(c=>!u.searchParams.has('incident_id')||c.incident_id===Number(u.searchParams.get('incident_id'))).map(advanceChange)});
    const c=changes.get(Number(changeMatch[1]));if(!c)return send({detail:'변경 요청을 찾지 못했습니다.'},404);advanceChange(c);const action=changeMatch[2];if(!action)return send(c);
    const stage=action==='plan'?u.searchParams.get('stage'):body.stage;
    if(action==='cancel'&&method==='POST'){if(c.status.endsWith('_deploying'))return send({detail:'검증 중입니다.'},409);c.status='cancelled';return send(c);}
    if(c.status!==({dev:'built',stg:'dev_verified',prd:'stg_verified'})[stage])return send({detail:'단계 순서를 확인하세요.'},409);
    const plan={stage,namespace:stage,deployment:c.service,container:c.service,previous_image:'local/payment:base',new_image:c.build.image,side_effects_ko:[`${stage} 환경 이미지 변경 및 파드 재시작, 중단 가능성이 있습니다.`],rollback_ko:'이전 이미지로 자동 원복하고 확인합니다.'};plan.plan_sha256=createHash('sha256').update(JSON.stringify(plan)).digest('hex');
    if(action==='plan')return send(plan);
    if(action==='promote'&&method==='POST'){if(body.confirm!==true)return send({detail:'명시적 확인이 필요합니다.'},422);if(body.plan_sha256!==plan.plan_sha256)return send({detail:'계획 해시 불일치'},409);c.status=stage+'_deploying';c.updated_at=now();c.stages[stage]={status:'executing',previous_image:plan.previous_image,new_image:plan.new_image,started_at:now(),reverted:false};return send(c);}
  }
  // End P18 changes block.
  if(path==='/api/mock/reset' && method==='POST'){reset();return send({ok:true});}
  // 시험 전용: 승인 대기 자동 조치 한 건을 만든다(실제 엔진의 approval 정책 결과와 같은 모양).
  if(path==='/api/mock/pending-action' && method==='POST'){const id=Number(body.incident_id);const n=narratives.get(id);if(!n)return send({detail:'장애를 찾지 못했습니다.'},404);const a={id:700+actions.size,incident_id:id,type:'scale_replicas',target:`Deployment/${n.incident.workload}`,params:{replicas:2},policy_mode:'approval',status:'pending_approval',reason:'레플리카 부족으로 응답 지연이 관측되었습니다.',executed:false,created_at:now(),updated_at:now()};actions.set(a.id,a);n.incident.status='action_pending';return send(a,201);}
  const actionMatch=path.match(/^\/api\/actions\/(\d+)\/(approve|reject)$/);
  if(actionMatch&&method==='POST'){const a=actions.get(Number(actionMatch[1]));if(!a)return send({detail:'조치를 찾지 못했습니다.'},404);if(a.status!=='pending_approval')return send({detail:`action is ${a.status}, not pending_approval`},409);const n=narratives.get(a.incident_id);if(actionMatch[2]==='approve'){a.status='succeeded';a.executed=true;n.incident.status='resolved';append(a.incident_id,'action_executed','운영자 승인 후 조치를 실행했습니다.',{action_id:a.id});}else{a.status='rejected';a.reason=String(body.reason||'');n.incident.status='handed_off';n.incident.handoff={recommended_steps:['kubectl -n liveprobe-target get pods']};append(a.incident_id,'note','운영자가 조치를 거절했습니다.',{action_id:a.id});}a.updated_at=now();return send(a);}
  if(path==='/api/overview')return send(overview);
  if(path==='/api/topology')return send(overview.topology);
  if(path==='/api/incidents'){
    let items=[...narratives.values()].reverse().map(n=>({...n.incident,stage:n.headline.stage})); const status=u.searchParams.get('status'), search=u.searchParams.get('search');
    if(status)items=items.filter(i=>i.status===status); if(search)items=items.filter(i=>(i.title+' '+i.workload).includes(search)); const total=items.length;const offset=Number(u.searchParams.get('offset')||0),limit=Number(u.searchParams.get('limit')||50);return send({items:items.slice(offset,offset+limit),total});
  }
  const claimMatch=path.match(/^\/api\/incidents\/(\d+)\/handoff\/claim$/);
  if(claimMatch&&method==='POST'){const n=narratives.get(Number(claimMatch[1]));if(!n)return send({detail:'장애를 찾지 못했습니다.'},404);if(n.incident.status!=='handed_off')return send({detail:'incident is not handed off'},409);n.incident.handoff={...(n.incident.handoff??{}),claimed_by:'operator',claimed_at:now()};return send(n.incident.handoff);}
  const closeMatch=path.match(/^\/api\/incidents\/(\d+)\/close$/);
  if(closeMatch&&method==='POST'){const n=narratives.get(Number(closeMatch[1]));if(!n)return send({detail:'장애를 찾지 못했습니다.'},404);const reason=String(body.reason||'').trim();if(body.confirm!==true||!reason||reason.length>500)return send({detail:'reason must be 1-500 characters'},422);const h=n.incident.handoff??{};if(n.incident.status==='closed')return send({id:n.incident.id,status:'closed',closed_by:h.closed_by,closed_at:h.closed_at,close_reason:h.close_reason,closed_from:h.closed_from,cluster_changed:false});if(!['handed_off','action_pending'].includes(n.incident.status))return send({detail:`incident is ${n.incident.status}; only a hand-off or a stale approval can be closed`},409);if([...actions.values()].some(a=>a.incident_id===n.incident.id&&a.status==='pending_approval'))return send({detail:'an action is still pending or running; approve or reject it first'},409);if([...commands.values()].some(c=>c.incident_id===n.incident.id&&['planning','awaiting_confirmation','awaiting_secret','executing','rolling_back'].includes(c.status)))return send({detail:'an operator command is still in progress; finish or cancel it first'},409);const from=n.incident.status;n.incident.status='closed';n.incident.closed_at=now();n.incident.updated_at=now();n.incident.status_reason=`운영자 종료: ${reason}`;n.incident.handoff={...h,closed_by:'local-operator',closed_at:now(),close_reason:reason,closed_from:from};n.headline.stage={key:'closed',label:'운영자 종료'};n.headline.next='운영자가 종료했습니다. 클러스터는 변경하지 않았고 이 장애에는 더 이상 지시를 받지 않습니다.';const o=n.flow.nodes.find(x=>x.id==='outcome');if(o){o.label='운영자 종료';o.status='done';}append(n.incident.id,'completed',`운영자 종료: ${reason}`,{outcome:'closed',previous_status:from});return send({id:n.incident.id,status:'closed',closed_by:'local-operator',closed_at:n.incident.handoff.closed_at,close_reason:reason,closed_from:from,cluster_changed:false});}
  const incidentMatch=path.match(/^\/api\/incidents\/(\d+)(?:\/(narrative|events|messages|commands|control))?$/);
  if(incidentMatch){
    const id=Number(incidentMatch[1]), action=incidentMatch[2], n=narratives.get(id);if(!n)return send({detail:'장애를 찾지 못했습니다.'},404);
    if(!action){const inc={...n.incident,status_reason:n.incident.status_reason??null,handoff:n.incident.handoff??null};return send({incident:inc,actions:[...actions.values()].filter(a=>a.incident_id===id),evidence:[],routing:[],agent_runs:[],audit:[]});}
    if(action==='narrative'){
      const copy=clone(n); let previous='outcome'; let depth=Math.max(0,...copy.flow.nodes.map(x=>x.depth))+1;
      for(const c of [...commands.values()].filter(c=>c.incident_id===id).map(advance)){
        const nid=`command:${c.id}`; const status=['failed','rejected'].includes(c.status)?'failed':c.status==='cancelled'?'skipped':['succeeded','rolled_back'].includes(c.status)?'done':'running';
        copy.flow.nodes.push({id:nid,type:'command',label:'운영자 지시 실행',role:null,role_ko:'운영자 지시 실행',job_ko:'운영자가 확인한 계획을 실행하고 결과를 검증합니다.',status,model:null,effort:null,tokens:null,cost_usd:null,duration_ms:null,inputs:[],conclusion_ko:c.plan?.summary_ko??null,conclusion_raw:c.instruction,lane:0,depth:depth++,group:null});copy.flow.edges.push({id:`${previous}>${nid}`,source:previous,target:nid,kind:'steer',label:'지시 실행'});previous=nid;
      }return send(copy);
    }
    if(action==='events')return send({...eventLogs.get(id),events:eventLogs.get(id).events.filter(e=>e.seq>Number(u.searchParams.get('since')||0))});
    if(action==='commands'&&method==='GET')return send({items:[...commands.values()].filter(c=>c.incident_id===id).map(advance)});
    if(action==='commands'&&method==='POST')return send({command:createCommand(id,String(body.instruction||''))},201);
    if(action==='messages'&&method==='POST'){
      if(['resolved','handed_off','failed'].includes(n.incident.status))return send({detail:'종료된 장애입니다.'},409);
      const message_id=Date.now();append(id,'operator_message',String(body.body||''),{message_id});append(id,'steering_interpreted','지시를 해석했습니다.',{intent:'instruction',target_role:'coordinator',ack_ko:'로그와 연결 풀 지표를 우선 확인하도록 다음 단계에 반영하겠습니다.'});
      const target=n.flow.nodes.find(x=>x.role==='coordinator');if(target&&!n.flow.nodes.some(x=>x.id===`steer:${message_id}`)){n.flow.nodes.push({...target,id:`steer:${message_id}`,type:'steer',role:null,role_ko:'운영자 지시',label:'운영자 조사 방향',job_ko:'운영자의 조사 방향을 다음 단계에 반영합니다.',status:'planned',lane:-1,conclusion_raw:String(body.body||'')});n.flow.edges.push({id:`steer:${message_id}>${target.id}`,source:`steer:${message_id}`,target:target.id,kind:'steer',label:'다음 단계 반영'});}
      return send({message_id,applies:'next_node_boundary'},202);
    }
    if(action==='control'&&method==='POST'){
      const a=body.action;eventLogs.get(id).control=a==='pause'?'paused':a==='resume'?'running':'stopping';append(id,a==='pause'?'paused':a==='resume'?'resumed':'stopped','운영자가 분석 상태를 변경했습니다.');if(a==='stop'){n.incident.status='failed';n.incident.closed_at=now();n.headline.stage={key:'failed',label:'분석 중단'};}return send({control:eventLogs.get(id).control});
    }
  }
  const commandMatch=path.match(/^\/api\/commands\/(\d+)\/(confirm|cancel|rollback|secret-values)$/);
  if(commandMatch&&method==='POST'){
    const c=commands.get(Number(commandMatch[1])),action=commandMatch[2];if(!c)return send({detail:'지시를 찾지 못했습니다.'},404);
    advance(c);if(action!=='cancel'&&body.plan_sha256!==c.plan_sha256)return send({detail:'계획이 바뀌었습니다. 다시 확인하세요.'},409);
    if(action==='cancel'){if(!['planning','awaiting_confirmation','awaiting_secret'].includes(c.status))return send({detail:'취소할 수 없는 상태입니다.'},409);c.status='cancelled';}
    if(action==='confirm'){if(c.status!=='awaiting_confirmation')return send({detail:'확인 대기 상태가 아닙니다.'},409);c.status=c.plan.operations.some(o=>o.op==='secret_update')?'awaiting_secret':'executing';append(c.incident_id,'command_confirmed','운영자가 실행을 확인했습니다.',{command_id:c.id});}
    if(action==='rollback'){if(c.status!=='succeeded'||!c.side_effects.reversible)return send({detail:'원복할 수 없습니다.'},409);c.status='rolling_back';}
    if(action==='secret-values'){
      if(c.status!=='awaiting_secret')return send({detail:'비밀값 입력 대기가 아닙니다.'},409);
      const keys=c.plan.operations.filter(o=>o.op==='secret_update').flatMap(o=>o.keys||[]);
      if(keys.some(k=>typeof body.values?.[k]!=='string'||!body.values[k])){body={};return send({detail:'필요한 비밀키를 모두 입력하세요.'},400);}
      // Read only to validate presence. Never put values in state, events, result, response, or logs.
      body={};c.status='executing';c.updated_at=now();return send({status:c.status,keys});
    }
    c.updated_at=now();return send({command:c});
  }
  if(path==='/api/settings'){if(method==='PUT')automationMode=body.automation_mode;return send({automation_mode:automationMode});}
  if(path==='/api/model-settings'){if(method==='PUT'){if(body.expected_revision!==modelSettings.revision)return send({detail:'설정 버전이 바뀌었습니다.'},409);modelSettings={...modelSettings,router:body.router,groups:body.groups,revision:modelSettings.revision+1,source:'database'};}return send(modelSettings);}
  if(path==='/api/llm/accounts')return send({mode:'single',active:'primary',accounts:[{name:'primary',state:'available'}]});
  if(path==='/api/policy/profile')return send({active:'assessment',requested:'assessment',downgraded_reason:null});
  if(path==='/api/review-ledger')return send({total:1,by_reason:{high_risk_level:1},items:[{id:1,created_at:now(),incident_id:990,reason_code:'high_risk_level',profile:'assessment',level:'HIGH_RISK',workload:'p14-net-sink',proposed:{},rationale:'통신 실패 경로가 미확인이라 자동 조치를 보류했습니다.',impact:'다른 서비스의 영향은 아직 미확인입니다.',outcome:'held_for_operator',rollback:null,operator_involved:false}]});
  if(path==='/api/review-ledger.csv'){res.writeHead(200,{'Content-Type':'text/csv; charset=utf-8'});return res.end('장애,사유\n990,고위험 판정\n');}
  return send({detail:'로컬 표본에 없는 경로입니다.'},404);
});
const port=Number(process.env.LIVEPROBE_MOCK_PORT||8015);server.listen(port,'127.0.0.1',()=>console.log(`로컬 모의 API 준비: ${port}`));
