import { useEffect, useState } from 'react';
import { request } from './api';

type Placement = { model: string; effort: string };
type Group = { primary_mode: 'router' | 'fixed'; roles: Record<string, Placement> };
type Draft = { router: Placement; groups: Record<string, Group> };
type ModelSettingsData = Draft & { revision: number; source: string; provider: string; editable: boolean;
  defaults: Draft; catalog: { model: string; efforts: string[] }[];
  limits: { model_ladder: string[]; max_cost_usd: Record<string, number> } };
const LEVELS = [['SIMPLE', '단순 그룹'], ['NORMAL', '보통 그룹'], ['COMPLEX', '복잡 그룹'], ['HIGH_RISK', '고위험 그룹']];
const ROLES: Record<string, string> = { router: '사건 분류', signal_analyst: '신호 분석', result_verifier: '결과 확인',
  coordinator: '진행 조정', k8s_analyst: '쿠버네티스 분석', log_summarizer: '로그 요약', group_verifier: '독립 결과 확인',
  k8s_log_analyst: '쿠버네티스·로그 분석', cause_reasoner: '원인 추론', challenger: '반론 검토',
  recovery_planner: '복구 계획', evidence_curator: '근거 정리', safety_verifier: '안전 검증' };
const PRIMARY: Record<string, string> = { SIMPLE: 'signal_analyst', NORMAL: 'k8s_analyst', COMPLEX: 'cause_reasoner', HIGH_RISK: 'evidence_curator' };
const ROLE_ORDER: Record<string, string[]> = {
  SIMPLE: ['signal_analyst', 'result_verifier'],
  NORMAL: ['coordinator', 'k8s_analyst', 'log_summarizer', 'group_verifier'],
  COMPLEX: ['k8s_log_analyst', 'cause_reasoner', 'challenger', 'recovery_planner', 'group_verifier'],
  HIGH_RISK: ['evidence_curator', 'safety_verifier'],
};
const EFFORTS: Record<string, string> = { low: '낮음', medium: '중간', high: '높음', xhigh: '최고' };
const cloneDraft = (s: Draft): Draft => structuredClone({ router: s.router, groups: s.groups });

export function ModelSettings() {
  const [data, setData] = useState<ModelSettingsData | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [level, setLevel] = useState('SIMPLE');
  const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(''); const [message, setMessage] = useState('');
  async function load() {
    setLoading(true); setError(''); setMessage('');
    try { const s = await request<ModelSettingsData>('/model-settings'); setData(s); setDraft(cloneDraft(s)); }
    catch (e) { setError(e instanceof Error ? e.message : '모델 설정을 불러오지 못했습니다.'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  const changes: string[] = [];
  if (data && draft) {
    if (JSON.stringify(draft.router) !== JSON.stringify(data.router)) changes.push('공통 사건 분류');
    for (const [key, label] of LEVELS) {
      const a = draft.groups[key]; const b = data.groups[key];
      if (a.primary_mode !== b.primary_mode) changes.push(`${label} · 주 모델 선택 방식`);
      for (const role of Object.keys(a.roles)) if (JSON.stringify(a.roles[role]) !== JSON.stringify(b.roles[role])) changes.push(`${label} · ${ROLES[role]}`);
    }
  }
  function edit(role: string, field: keyof Placement, value: string, common = false) {
    setMessage('');
    setDraft(old => {
      if (!old) return old;
      const next = cloneDraft(old);
      const target = common ? next.router : next.groups[level].roles[role];
      target[field] = value;
      return next;
    });
  }
  async function save() {
    if (!data || !draft) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const saved = await request<ModelSettingsData>('/model-settings', { method: 'PUT', body: JSON.stringify({ expected_revision: data.revision, ...draft }) });
      setData(saved); setDraft(cloneDraft(saved)); setMessage(`버전 ${saved.revision} 저장 완료. 다음 분석 실행부터 적용됩니다.`);
    } catch (e) { setError(e instanceof Error ? e.message : '저장하지 못했습니다. 초안은 유지됩니다.'); }
    finally { setBusy(false); }
  }
  function placement(role: string, value: Placement, common = false) {
    const primary = !common && PRIMARY[level] === role;
    const automatic = primary && draft?.groups[level].primary_mode === 'router';
    const restricted = role === 'safety_verifier' || role === 'challenger';
    const candidates = (data?.catalog ?? []).filter(c => restricted ? c.model === 'gpt-6-astra'
      : ['result_verifier', 'group_verifier'].includes(role) ? c.model !== 'gpt-6-luna' : true)
      .filter(c => !primary || automatic || level === 'SIMPLE' || (level === 'HIGH_RISK' ? c.model === 'gpt-6-astra' : c.model !== 'gpt-6-luna'));
    const efforts = (data?.catalog.find(c => c.model === value.model)?.efforts ?? []).filter(e => role !== 'safety_verifier' || e === 'high' || e === 'xhigh');
    return <div className="model-role" key={role}>
      <div><b>{ROLES[role]}</b>{primary && <span className="badge">주 분석</span>}
        {automatic && <p className="muted">모델은 사건별 라우터가 선택합니다.</p>}</div>
      <label>{ROLES[role]} 모델<select aria-label={`${ROLES[role]} 모델`} className="select" value={value.model} disabled={busy || automatic || !data?.editable} onChange={e => edit(role, 'model', e.target.value, common)}>
        {candidates.map(c => <option key={c.model} value={c.model}>{c.model}</option>)}
      </select></label>
      <label>{ROLES[role]} 추론 강도<select aria-label={`${ROLES[role]} 추론 강도`} className="select" value={value.effort} disabled={busy || !data?.editable} onChange={e => edit(role, 'effort', e.target.value, common)}>
        {efforts.map(e => <option key={e} value={e}>{EFFORTS[e] ?? e}</option>)}
      </select></label>
    </div>;
  }
  return <section className="model-settings" aria-labelledby="model-settings-title">
    <div className="model-heading"><div><h2 id="model-settings-title">그룹별 모델</h2><p className="muted">역할별 모델과 추론 강도입니다. 진행 중인 실행과 과거 기록은 바뀌지 않습니다.</p></div>
      {data && <span className="badge">저장 버전 {data.revision}</span>}</div>
    {error && <div className="error" role="alert">{error}</div>}
    {loading ? <p role="status">모델 설정을 불러오는 중…</p> : !data || !draft ? <button className="btn" onClick={load}>모델 설정 다시 불러오기</button> : <>
      {!data.editable && <p className="notice">현재 공급자는 {data.provider}입니다. 이 화면의 편집은 Codex CLI에서만 지원합니다.</p>}
      <p className="model-boundary">Codex CLI 전용 · API 키 전환 없음 · 고위험 자동 조치 금지</p>
      <div className="card"><header>공통 사건 분류</header><div className="body">{placement('router', draft.router, true)}</div></div>
      <div className="model-group-buttons" aria-label="사건 수준별 그룹">{LEVELS.map(([key, label]) => <button className={`btn ${level === key ? 'primary' : ''}`} key={key} aria-pressed={level === key} onClick={() => setLevel(key)}>{label}</button>)}</div>
      <div className="card"><header><h3>{LEVELS.find(([key]) => key === level)?.[1]} 역할 배치</h3></header><div className="body">
        <label className="model-mode">주 모델 선택 방식<select aria-label="주 모델 선택 방식" className="select" value={draft.groups[level].primary_mode} disabled={busy || !data.editable} onChange={e => {
          const mode = e.target.value as Group['primary_mode']; setMessage(''); setDraft(old => {
            if (!old) return old;
            const next = cloneDraft(old); next.groups[level].primary_mode = mode;
            if (mode === 'fixed' && level === 'HIGH_RISK') next.groups[level].roles.evidence_curator.model = 'gpt-6-astra';
            return next;
          });
        }}><option value="router">라우터 자동 선택</option><option value="fixed">직접 지정</option></select></label>
        <p className="muted model-note">자동 선택에서는 주 분석 모델만 사건별 점수로 정하고, 추론 강도와 다른 역할 배치는 아래 설정을 따릅니다.</p>
        {ROLE_ORDER[level].map(role => placement(role, draft.groups[level].roles[role]))}
      </div></div>
      <p className="model-note muted">검증 역할의 최소 모델 등급은 낮출 수 없습니다. 승격 순서: {data.limits.model_ladder.join(' → ')}. 예산과 안전조치 한도는 이 화면에서 변경하지 않습니다.</p>
      <div className="model-changes" aria-live="polite"><b>저장할 변경 {changes.length}건</b>{changes.length > 0 && <ul>{changes.map(c => <li key={c}>{c}</li>)}</ul>}</div>
      <div className="model-actions"><button className="btn primary" disabled={busy || changes.length === 0 || !data.editable} onClick={save}>{busy ? '저장 중…' : '모델 설정 저장'}</button>
        <button className="btn" disabled={busy} onClick={() => { setDraft(cloneDraft(data.defaults)); setMessage('기본값을 초안에 불러왔습니다. 저장해야 적용됩니다.'); }}>기본값 불러오기</button>
        <button className="btn" disabled={busy} onClick={() => { if (!changes.length || window.confirm('저장하지 않은 초안을 버리고 다시 불러올까요?')) void load(); }}>모델 설정 다시 불러오기</button></div>
      <div className="model-status" role="status">{message}</div>
    </>}
  </section>;
}
