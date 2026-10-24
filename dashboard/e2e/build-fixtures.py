# -*- coding: utf-8 -*-
"""실제 수집 표본을 화면 계약 fixture로 변환한다. 사용: python3 e2e/build-fixtures.py <표본 폴더>
외부 요청·DB·클러스터 변경 없음. 진행 중 사례와 명령 계획만 로컬 검증용으로 구성한다.
"""
import json
import re
import sys
from pathlib import Path

src = Path(sys.argv[1]).expanduser()
out = Path(__file__).parent / 'fixtures'
out.mkdir(exist_ok=True)
def write(name, value):
    (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
def read(name):
    return json.loads((src / name).read_text())
roles = {'router': ('사건 수준 판단', '원천 근거로 장애 수준과 분석 그룹을 결정합니다.'), 'signal_analyst': ('신호 분석', '이벤트와 상태를 읽고 원인과 조치를 제안합니다.'), 'result_verifier': ('결과 확인', '분석 결론과 조치가 근거에 맞는지 확인합니다.'), 'k8s_log_analyst': ('쿠버네티스·로그 분석', '쿠버네티스 상태와 오류 로그를 함께 분석합니다.'), 'cause_reasoner': ('원인 추론', '여러 원인 후보와 근거를 비교합니다.'), 'challenger': ('반론 검토', '반대 근거로 결론의 약점을 확인합니다.'), 'recovery_planner': ('복구 계획', '복구 방법과 되돌릴 수 있는 조치를 계획합니다.'), 'group_verifier': ('독립 결과 확인', '그룹의 결론과 인용을 독립적으로 검증합니다.'), 'evidence_curator': ('근거 정리', '고위험 장애에서 확인한 것과 아직 모르는 것을 구분합니다.'), 'safety_verifier': ('안전 검증', '자동 조치를 멈추고 운영자에게 전달할 내용을 확인합니다.'), 'k8s_analyst': ('쿠버네티스 분석', '배포와 파드 상태를 읽어 장애 원인을 좁힙니다.'), 'log_summarizer': ('로그 요약', '오류 로그와 지표에서 증상을 정리합니다.'), 'coordinator': ('진행 조정', '병렬 분석 결과를 합쳐 복구 방향을 정합니다.')}
levels = {'SIMPLE': '단순', 'NORMAL': '보통', 'COMPLEX': '복잡', 'HIGH_RISK': '고위험'}
stages = {'SIMPLE': [['signal_analyst'], ['result_verifier']], 'NORMAL': [['k8s_analyst', 'log_summarizer'], ['coordinator'], ['group_verifier']], 'COMPLEX': [['k8s_log_analyst'], ['cause_reasoner'], ['challenger', 'recovery_planner'], ['group_verifier']], 'HIGH_RISK': [['evidence_curator'], ['safety_verifier']]}
refmap = {'deployment': 'E1_deployment', 'pod_status': 'E2_pods', 'service': 'E3_service', 'k8s_event': 'E5_recent_events', 'prometheus': 'E6_metrics', 'loki': 'E7_error_logs', 'node': 'E8_node', 'configmap': 'E9_config'}
def refs(run):
    o = run.get('output') or {}
    rs = [e.get('ref') for e in o.get('evidence', [])]
    for c in o.get('root_cause_candidates', []) + o.get('candidates', []):
        rs += c.get('evidence_refs', [])
    return list(dict.fromkeys(r for r in rs if r))
def conclusion_ko(iid, role):
    if iid == 971:
        return {'router': '메모리 초과 종료와 정상 연결 대상 소실이 함께 발생했고 호출 서비스도 영향을 받아 복잡 그룹을 선택했습니다.', 'challenger': '메모리 초과 종료는 확인했지만 그 전에 상태 점검이 실패했습니다. 메모리 증가의 근본 원인까지 입증된 것은 아닙니다.', 'recovery_planner': '메모리 한도를 512Mi에서 640Mi로 올리고 즉시 복구를 검증합니다. 증가 원인은 별도로 조사해야 합니다.', 'group_verifier': '메모리 초과 종료와 연결 대상 소실은 근거와 일치합니다. 한도 조정은 되돌릴 수 있는 완화 조치이며 근본 원인은 미확인입니다.'}.get(role, '유일한 컨테이너가 512Mi 한도에서 메모리 초과로 종료되어 반복 재시작 중입니다. 준비된 연결 대상이 없어 주문 서비스의 호출도 실패했습니다.')
    if iid == 987:
        return '새 배포 버전 107의 이미지 태그를 내려받을 수 없습니다. 이전 버전 106은 준비된 파드가 있어 이전 배포로 되돌리는 조치가 근거와 일치합니다.'
    if iid == 990:
        return '네트워크 차단 신호는 있지만 실패 경로·원인·영향 범위는 확인되지 않았습니다. 파드 준비 상태만으로 통신 정상이라고 할 수 없습니다. 자동 조치 없이 운영자에게 인계합니다.'
    return '주문 프로세스의 데이터베이스 연결 풀이 모두 사용 중이며 연결 획득 실패가 반복됩니다. 재시작은 범위가 한정된 완화 조치이며 장기 원인은 추가 확인이 필요합니다.'
def reading(raw, kind):
    for pat, ko in [(r'ImagePull|pull image|pull access denied', '이미지를 내려받지 못했습니다. 태그와 저장소 접근 권한을 확인해야 합니다.'), (r'OOMKilled', '메모리 한도를 넘어 컨테이너가 강제 종료되었습니다.'), (r'CrashLoopBackOff|Back-off restarting', '컨테이너가 반복 종료되어 재시작을 대기하고 있습니다.'), (r'no healthy upstream|503 Service Unavailable', '호출할 준비된 대상이 없어 서비스 요청이 실패했습니다.'), (r'Connection is not available|CannotGetJdbc|HikariPool|connection pool', '데이터베이스 연결 획득에 실패했습니다. 연결 풀 고갈 여부를 확인해야 합니다.'), (r'probe failed|connection refused', '상태 확인 또는 포트 연결에 실패했습니다.')]:
        if re.search(pat, raw, re.I): return ko
    return {'event': '쿠버네티스가 기록한 자원 상태 변화입니다. 원문의 대상·사유·시각을 확인하세요.', 'status': '수집 당시의 자원 상태 원문입니다. 준비 상태와 목표 수량을 함께 확인하세요.', 'metric': '수집 당시의 원천 지표입니다. 누락된 지표는 정상으로 판정하지 않습니다.', 'config': '설정의 키와 해시만 수집했습니다. 값은 표시하지 않습니다.', 'node': '노드 준비 상태와 할당 가능한 자원 정보입니다.'}.get(kind)
def make(iid, progressing=False):
    d = read(f'incident-{iid}.json'); incident = {k: v for k,v in d['incident'].items() if k in ['id','title','namespace','workload','status','severity_level','signal_count','created_at','updated_at','closed_at','total_cost_usd','agent_run_count']}
    level = incident['severity_level']; runs = d['agent_runs'] if not progressing else d['agent_runs'][:2]
    if progressing: incident.update(id=1976, status='analyzing', closed_at=None, agent_run_count=len(runs), created_at='2026-10-19T00:00:00+09:00')
    flow = {'nodes': [], 'edges': []}
    def node(nid, typ, label, depth, lane=0, role=None, status='done', **kwargs):
        rko, job = roles.get(role, (label, '이 단계의 실행 상태를 확인합니다.'))
        n = dict(id=nid, type=typ, label=label, role=role, role_ko=rko, job_ko=job, status=status, model=None, effort=None, tokens=None, cost_usd=None, duration_ms=None, inputs=[], conclusion_ko=None, conclusion_raw=None, lane=lane, depth=depth, group=None)
        n.update(kwargs); flow['nodes'].append(n); return n
    def edge(a,b,kind='flow',label=None): flow['edges'].append(dict(id=f'{a}>{b}:{kind}',source=a,target=b,kind=kind,label=label))
    node('signal','signal','메모리 초과 종료 감지' if iid==971 else '이미지 내려받기 실패 감지' if iid==987 else '네트워크 차단 신호' if iid==990 else '연결 풀 고갈 신호',0)
    node('evidence','evidence',f'원천 근거 {len(d["evidence"])}건',1); edge('signal','evidence')
    previous = ['evidence']; role_nodes = {}; columns = {}
    for r in runs:
        role = r['role']; ko = roles[role][0]
        if role == 'router': depth,lane = 2,0
        else:
            si = next(i for i,rs in enumerate(stages[level]) if role in rs); depth,lane = 4+si, stages[level][si].index(role)
        old = role_nodes.get(role)
        if old: old['status']='escalated'; lane = max([n['lane'] for n in flow['nodes'] if n['depth']==depth] + [0])+1
        o = r['output'] or {}; raw = o.get('summary') or o.get('rationale')
        n = node('router' if role=='router' else f'run:{r["id"]}', 'router' if role=='router' else 'verify' if 'verifier' in role else 'agent', ko, depth, lane, role, model=r['model'],effort=r['reasoning_effort'],tokens={'in':r['input_tokens'],'out':r['output_tokens']},cost_usd=r['estimated_cost_usd'],duration_ms=r['duration_ms'],inputs=refs(r),conclusion_raw=raw,conclusion_ko=conclusion_ko(iid,role),group=None if role=='router' else level)
        role_nodes[role]=n
        if role=='router': edge('evidence','router'); node('group','group',f'{levels[level]} 그룹 선택',3,group=level); edge('router','group')
        else:
            columns.setdefault(depth,[]).append(n)
            if old: edge(old['id'],n['id'],'escalate','상위 모델 승격')
    if progressing:
        for si, rs in enumerate(stages[level]):
            for lane, role in enumerate(rs):
                if role not in role_nodes:
                    n=node(f'planned:{role}', 'verify' if 'verifier' in role else 'agent', roles[role][0],4+si,lane,role,status='running' if role=='k8s_analyst' else 'planned',group=level,model='gpt-6.1-sol',effort='medium')
                    columns.setdefault(4+si,[]).append(n)
    previous=['group']
    for depth in sorted(columns):
        for n in columns[depth]:
            if not any(e['target']==n['id'] and e['kind']=='escalate' for e in flow['edges']):
                for p in previous: edge(p,n['id'])
        previous=[n['id'] for n in columns[depth] if n['status']!='escalated']
    depth=max([n['depth'] for n in flow['nodes']])+1
    if not progressing:
        for a in d['actions']:
            n=node(f'action:{a["id"]}','action','메모리 한도 조정' if iid==971 else '이전 배포로 롤백' if iid==987 else '배포 재시작',depth,conclusion_ko='조치를 실행한 뒤 복구 검증을 통과했습니다.',conclusion_raw=f'{a["type"]} → {a["status"]}')
            for p in previous: edge(p,n['id'])
            previous=[n['id']]; depth+=1
    outcome='자동 복구 완료' if incident['status']=='resolved' else '운영자 인계' if incident['status']=='handed_off' else '결과 대기'
    node('outcome','outcome',outcome,depth,status='planned' if progressing else 'done')
    for p in previous: edge(p,'outcome')
    raw_items=[]
    for ev in d['evidence']:
        source=ev['source']; ref=refmap.get(source)
        if not ref or ev['payload'] is None: continue
        kind={'k8s_event':'event','loki':'log','prometheus':'metric','node':'node','configmap':'config'}.get(source,'status')
        payload=ev['payload']
        vals=payload if source in ['k8s_event','pod_status'] else payload.get('lines',[]) if source=='loki' else [payload]
        for ix,v in enumerate(vals):
            text=v['message'] if source=='k8s_event' else v if isinstance(v,str) else json.dumps(v,ensure_ascii=False,sort_keys=True)
            sev='error' if re.search('ERROR|OOMKilled|CrashLoopBackOff|ErrImagePull|ImagePullBackOff',text) else 'warn' if source=='k8s_event' and v.get('type')=='Warning' else 'info'
            cited=[r['role'] for r in runs if ref in refs(r)]
            raw_items.append(dict(id=f'{ref}#{ix}',ref=ref,kind=kind,source='loki' if source=='loki' else 'prometheus' if source=='prometheus' else 'kubernetes',at=v.get('last') if source=='k8s_event' else ev['collected_at'],raw=text,severity=sev,ko=reading(text,kind),ko_source='rules' if reading(text,kind) else None,cited_by=list(dict.fromkeys(cited))))
    for r in runs:
        o=r['output'] or {}; text=o.get('summary') or o.get('rationale')
        if text: raw_items.append(dict(id=f'agent:{r["id"]}',ref=f'agent:{r["role"]}',kind='agent',source=f'agent:{r["role"]}',at=r['created_at'],raw=text,severity='info',ko=conclusion_ko(iid,r['role']),ko_source='llm',cited_by=[]))
    raw_items.sort(key=lambda i:(i['kind']=='agent',{'error':0,'warn':1,'info':2}[i['severity']]))
    what={971:'재고 서비스가 메모리 초과로 종료되었습니다',987:'주문 서비스의 새 이미지를 내려받지 못했습니다',990:'네트워크 차단 신호의 경로를 확인해야 합니다',976:'주문 서비스에서 데이터베이스 연결 풀이 고갈되었습니다'}[iid]
    impact={971:'재고 서비스의 유일한 파드가 준비되지 않아 주문 서비스에서 503 오류가 함께 발생했습니다.',987:'새 배포 파드가 대기 중입니다. 이전 버전의 파드가 요청을 처리하고 있습니다.',990:'파드는 준비되었지만 실제 통신 경로와 다른 서비스의 영향은 미확인입니다.',976:'주문 서비스의 연결 풀이 모두 점유되어 데이터베이스 조회 실패가 반복됩니다.'}[iid]
    stage=dict(key='analyzing' if progressing else incident['status'],label='쿠버네티스 분석 중' if progressing else outcome)
    next_='다음 단계부터 조사 방향을 반영합니다. 오른쪽 대화에서 AI에게 지시하세요.' if progressing else 'AI가 조치를 실행하고 복구를 검증했습니다. 재발 여부를 확인하세요.' if incident['status']=='resolved' else '자동 조치 없이 인계했습니다. 원문 근거를 확인한 뒤 AI에게 후속 작업을 지시하세요.'
    narrative=dict(incident=incident,headline=dict(what=what,impact=impact,stage=stage,next=next_,source='rules'),raw=raw_items,flow=flow,interpreting=False)
    events=read(f'events-{iid}.json')
    if progressing: events['events']=events['events'][:7]
    write(f'narrative-{incident["id"]}.json',narrative); write(f'events-{incident["id"]}.json',events)
    return narrative
narratives=[make(971),make(987),make(990),make(976),make(976,True)]
top=read('topology.json')
# 지도 표본을 읽기 쉬운 연결 경로로 한정한다. 수집되지 않은 사용량은 null이다.
keep={'liveprobe-target','liveprobe-system'}
ns_nodes=[n for n in top['nodes'] if n.get('namespace') in keep or n['kind']=='Node']
for n in ns_nodes: n.pop('warnings',None)
ids={n['id'] for n in ns_nodes}
top['nodes']=ns_nodes;top['observed_edges']=[e for e in top['observed_edges'] if e['from'] in ids and e['to'] in ids];top['structure_edges']=[e for e in top['structure_edges'] if e['from'] in ids and e['to'] in ids]
write('topology.json',top)
workloads=[]
met=next(e['payload'] for e in read('incident-976.json')['evidence'] if e['source']=='prometheus')
for n in ns_nodes:
    if n['kind'] not in ['Deployment','StatefulSet','DaemonSet']:continue
    req=met.get('http_rate_per_s',{}).get(n['name']); err=met.get('http_5xx_rate_per_s',{}).get(n['name'])
    workloads.append(dict(id=n['id'],namespace=n['namespace'],name=n['name'],kind=n['kind'],ready=n.get('ready'),desired=n.get('pods'),restarts_1h=None,cpu=None,mem=None,req_rate=req,err_rate=err/req if req and err is not None else None,p95_ms=None,incident=dict(id=1976,level='NORMAL',status='analyzing',stage=narratives[-1]['headline']['stage']) if n['name']=='order' else None))
nsstats=[]
for ns in sorted(keep):
    ws=[w for w in workloads if w['namespace']==ns]
    nsstats.append(dict(name=ns,pods_running=sum(w['ready'] or 0 for w in ws),pods_pending=0,pods_failed=0,restarts_1h=None,cpu_cores=None,mem_bytes=None,workloads=len(ws),workloads_unhealthy=None))
opened={k:v for k,v in narratives[-1]['incident'].items() if k in ['id','title','namespace','workload','severity_level','status','created_at']};opened['stage']=narratives[-1]['headline']['stage']
events=[]
for iid in [971,987,990]:
    ev=next(e for e in read(f'incident-{iid}.json')['evidence'] if e['source']=='k8s_event')
    for e in ev['payload']:
        if e['type']=='Warning': events.append(dict(at=e['last'],namespace='liveprobe-target',object=e['object'],reason=e['reason'],type=e['type'],message=e['message'],count=e['count']))
write('overview.json',dict(generated_at='2026-10-19T00:16:00+09:00',cluster=dict(nodes=[dict(name='lima-rancher-desktop',ready=True,cpu_cores=4,cpu_used_cores=None,mem_bytes=12232176*1024,mem_used_bytes=None,pods=sum(n['pods_running'] for n in nsstats))],namespaces=nsstats),workloads=workloads,events=events[:8],incidents=dict(open=[opened],today=dict(total=5,resolved=3,handed_off=1,failed=0)),topology=top,sources=dict(kubernetes='ok',prometheus='ok',hubble='connected',envoy='표본에 Envoy 지표 없음')))
write('incidents.json',dict(items=[{**n['incident'],'stage':n['headline']['stage']} for n in reversed(narratives)],total=len(narratives)))
print(f'작성 완료: {len(list(out.glob("*.json")))}개 fixture')
