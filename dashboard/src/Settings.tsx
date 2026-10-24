import { useEffect, useState } from 'react';
import { request } from './api';
import { useLive } from './live';
import { ModelSettings } from './ModelSettings';

type Mode = 'auto' | 'approval' | 'suggest_only';
const MODES: [Mode, string, string][] = [['auto', '자동 실행', '허용된 저위험 조치를 자동 실행합니다.'], ['approval', '승인 후 실행', '조치를 실행하기 전 운영자 승인을 요청합니다.'], ['suggest_only', '제안만', '조치를 실행하지 않고 제안만 기록합니다.']];

export function Settings() {
  const [mode, setMode] = useState<Mode>('approval'); const [error, setError] = useState<string | null>(null); const [msg, setMsg] = useState(''); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState<{ active: string; requested?: string; downgraded_reason?: string | null } | null>(null);
  const { accounts, notifyState, askNotify } = useLive();
  useEffect(() => {
    request<{ automation_mode: Mode }>('/settings').then(s => setMode(s.automation_mode)).catch(e => setError(e.message)).finally(() => setLoading(false));
    request<typeof profile>('/policy/profile').then(setProfile).catch(() => undefined);
  }, []);
  async function save() {
    setBusy(true); setMsg(''); setError(null);
    try { const s = await request<{ automation_mode: Mode }>('/settings', { method: 'PUT', body: JSON.stringify({ automation_mode: mode }) }); setMode(s.automation_mode); setMsg('저장했습니다.'); }
    catch (e) { setError(e instanceof Error ? e.message : '저장 실패'); } finally { setBusy(false); }
  }
  return <div className="page" style={{ maxWidth: 1040 }}>
    <div className="page-head"><div><h1>설정</h1><p>자동화 정책과 실행 환경 상태입니다.</p></div></div>
    {error && <div className="error" role="alert">{error}</div>}
    <div className="card" style={{ marginBottom: 24 }}><header>조치 자동화 모드</header><div className="body">{loading ? '불러오는 중…' : <div style={{ display: 'grid', gap: 8 }}>
      {MODES.map(([v, label, desc]) => <label className="opt" key={v}><input type="radio" name="mode" checked={mode === v} onChange={() => setMode(v)} /><span><b>{label}</b><small>{desc}</small></span></label>)}
      <div style={{ display: 'flex', gap: 16, alignItems: 'center', marginTop: 8 }}><button className="btn primary" disabled={busy} onClick={save}>{busy ? '저장 중…' : '저장'}</button>{msg && <span role="status" className="muted">{msg}</span>}</div></div>}</div></div>
    <div className="card" style={{ marginBottom: 24 }}><header>정책 프로필</header><div className="body">{profile ? <>
      <p><span className={`badge ${profile.active === 'assessment' ? 'ok' : 'warn'}`}>{profile.active}</span></p>
      <p className="muted" style={{ marginTop: 8 }}>{profile.active === 'assessment' ? '평가 기준입니다. HIGH_RISK 사건은 설정과 관계없이 운영자에게 인계되고 실행되지 않습니다.' : 'PoC 자율 프로필입니다. HIGH_RISK 조치도 자동 실행되지 않고 운영자 승인을 거칩니다.'}</p>
      {profile.downgraded_reason && <p className="notice" style={{ marginTop: 8 }}>요청한 {profile.requested} 대신 assessment로 동작합니다: {profile.downgraded_reason}</p>}</> : '확인 중…'}</div></div>
    <div className="card" style={{ marginBottom: 24 }}><header>Codex 계정</header><div className="body">{!accounts ? '확인 중…' : <>
      {accounts.accounts.map(a => <p key={a.name}><b>{a.name === 'primary' ? '주 계정' : '예비 계정'}</b> <span className={`badge ${a.state === 'available' ? 'ok' : 'warn'}`}>{a.state === 'available' ? '사용 가능' : a.state === 'limited' ? `한도 · 약 ${Math.ceil((a.blocked_for_s ?? 0) / 60)}분 후` : a.state}</span></p>)}
      {accounts.mode !== 'pool' && <p className="notice" style={{ marginTop: 8 }}>예비 계정이 설정되지 않았습니다. 한도에 닿으면 상태를 저장하고 대기합니다. API 키나 다른 모델로 전환하지 않습니다.</p>}</>}</div></div>
    <ModelSettings />
    <div className="card"><header>브라우저 알림</header><div className="body">
      {notifyState === 'unsupported' ? <span className="muted">이 브라우저는 알림을 지원하지 않습니다. 화면 안 알림은 계속 표시됩니다.</span> : notifyState === 'granted' ? <span className="badge ok">켜짐</span> : notifyState === 'denied' ? <span className="muted">브라우저 설정에서 차단되어 있습니다. 화면 안 알림은 계속 표시됩니다.</span> : <button className="btn" onClick={askNotify}>알림 허용 요청</button>}</div></div>
  </div>;
}
