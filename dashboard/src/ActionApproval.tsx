import { useState } from 'react';
import { ApiError, request, type Action, type Incident } from './api';
import { CLOSE_EFFECTS, CLOSE_REASON_MAX, closeConfirmText, closeErrorKo, closeReasonError } from './closeIncident';
import { ACTION_KO } from './labels';
import { actionChanges } from './narrative';
import { ErrorBox } from './ui';

/** An action the unattended engine proposed under the approval policy. The server re-runs every safety check on approve. */
export function ActionApprovalCard({ action: a, onChange }: { action: Action; onChange: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const changes = actionChanges(a.params);
  async function decide(choice: 'approve' | 'reject') {
    if (busy) return;
    if (choice === 'approve' && !window.confirm(`이 조치를 승인하시겠습니까? ${ACTION_KO[a.type] ?? a.type} · ${a.target}\n서버가 안전 검사를 다시 수행한 뒤 실행합니다.`)) return;
    if (choice === 'reject' && !window.confirm('이 조치를 거절하시겠습니까? 거절하면 장애는 운영자에게 인계됩니다.')) return;
    setBusy(true); setError('');
    try { await request(`/actions/${a.id}/${choice}`, { method: 'POST', ...(choice === 'reject' ? { body: JSON.stringify({ reason: '운영자 거절' }) } : {}) }); onChange(); }
    catch { setError(choice === 'approve' ? '승인을 처리하지 못했습니다. 정책이 바뀌었거나 이미 처리된 조치일 수 있습니다. 최신 상태를 확인하세요.' : '거절을 처리하지 못했습니다. 최신 상태를 확인하세요.'); onChange(); }
    finally { setBusy(false); }
  }
  return <article className="decision-card action-card" aria-label={`조치 승인 요청 ${a.id}`}>
    <header><b>자동 조치 승인 요청</b><span className="badge warn" role="status">승인 대기</span></header>
    <p>{ACTION_KO[a.type] ?? '등록되지 않은 조치'} · <span className="mono">{a.target}</span></p>
    <dl className="kv"><dt>조치 종류</dt><dd className="mono">{a.type}</dd><dt>정책</dt><dd>운영자 승인 필요</dd>{changes.length > 0 && <><dt>변경 값</dt><dd className="mono">{changes.join(', ')}</dd></>}</dl>
    <div className="confirm-gate"><strong>이 조치를 실행할까요?</strong><div className="inline"><button className="btn primary" type="button" disabled={busy} onClick={() => void decide('approve')}>승인</button><button className="btn" type="button" disabled={busy} onClick={() => void decide('reject')}>거절</button></div></div>
    <ErrorBox>{error}</ErrorBox>
    {a.params != null && <details><summary>매개변수 원문 보기</summary><pre className="raw-text">{JSON.stringify(a.params, null, 2)}</pre></details>}
    {a.reason && <details><summary>제안 사유 원문 보기</summary><pre className="raw-text">{a.reason}</pre></details>}
  </article>;
}

/** Taking over a handoff records ownership only; it never authorizes a cluster change. */
export function HandoffCard({ incident: i, onChange }: { incident: Incident; onChange: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const h = (i.handoff ?? {}) as { claimed_by?: string; recommended_steps?: unknown };
  const steps = Array.isArray(h.recommended_steps) ? h.recommended_steps.filter((s): s is string => typeof s === 'string') : [];
  async function claim() {
    setBusy(true); setError('');
    try { await request(`/incidents/${i.id}/handoff/claim`, { method: 'POST' }); onChange(); }
    catch { setError('인계 인수를 처리하지 못했습니다. 다른 운영자가 이미 인수했을 수 있습니다.'); onChange(); }
    finally { setBusy(false); }
  }
  return <article className="decision-card handoff-card" aria-label="운영자 인계">
    <header><b>운영자 인계</b><span className={`badge ${h.claimed_by ? 'ok' : 'warn'}`} role="status">{h.claimed_by ? '인수됨' : '인수 전'}</span></header>
    {i.status_reason && <details><summary>인계 사유 원문 보기</summary><pre className="raw-text">{i.status_reason}</pre></details>}
    {steps.length > 0 && <><h4>권장 조치(원문)</h4><ul>{steps.map((s, n) => <li key={n} className="mono">{s}</li>)}</ul></>}
    {h.claimed_by ? <p className="muted">인수: {h.claimed_by}</p> : <button className="btn primary" type="button" disabled={busy} onClick={() => void claim()}>인계 인수</button>}
    <p className="muted">인수는 담당 기록일 뿐 클러스터 변경을 허가하지 않습니다. 변경은 아래 대화로 지시하면 계획과 부작용을 확인한 뒤 진행합니다.</p>
    <ErrorBox>{error}</ErrorBox>
  </article>;
}

/** Operator close: ends an incident the AI already stopped on. Records a decision only; never changes the cluster. */
export function CloseCard({ incident: i, blocked, onChange }: { incident: Incident; blocked: string; onChange: () => void }) {
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const invalid = closeReasonError(reason);
  async function close() {
    if (busy || blocked || invalid) { setError(blocked || invalid); return; }
    if (!window.confirm(closeConfirmText(i.id, reason))) return;
    setBusy(true); setError('');
    try { await request(`/incidents/${i.id}/close`, { method: 'POST', body: JSON.stringify({ reason: reason.trim(), confirm: true }) }); setReason(''); onChange(); }
    catch (e) { setError(e instanceof ApiError ? closeErrorKo(e.status, e.message) : closeErrorKo(0, '')); onChange(); }
    finally { setBusy(false); }
  }
  return <article className="decision-card close-card" aria-label="장애 종료">
    <header><b>장애 종료</b><span className="badge" role="status">운영자 결정</span></header>
    <p>원인을 확인했고 더 할 일이 없다면 이 장애를 종료합니다.</p>
    <ul className="close-effects">{CLOSE_EFFECTS.map(e => <li key={e}>{e}</li>)}</ul>
    <label className="close-reason"><span>종료 사유 <small className="faint">(필수, {CLOSE_REASON_MAX}자 이하)</small></span>
      <textarea rows={2} maxLength={CLOSE_REASON_MAX} value={reason} placeholder="예: 대상 정상 확인, 메모리 한도 변경 불필요" onChange={e => setReason(e.target.value)} disabled={busy} /></label>
    {blocked && <p className="notice" role="status">{blocked}</p>}
    <div className="confirm-gate"><strong>확인 후 종료할까요?</strong><div className="inline"><button className="btn primary" type="button" disabled={busy || !!blocked || !!invalid} onClick={() => void close()}>{busy ? '종료 중' : '장애 종료'}</button></div></div>
    <ErrorBox>{error}</ErrorBox>
  </article>;
}

/** After an operator close: the decision record, read-only. */
export function ClosedCard({ incident: i }: { incident: Incident }) {
  const h = (i.handoff ?? {}) as { closed_by?: string; closed_at?: string; close_reason?: string };
  return <article className="decision-card closed-card" aria-label="운영자 종료 기록">
    <header><b>운영자 종료</b><span className="badge" role="status">종료됨</span></header>
    <dl className="kv"><dt>종료한 운영자</dt><dd>{h.closed_by ?? '기록 없음'}</dd><dt>종료 시각</dt><dd>{h.closed_at ? new Date(h.closed_at).toLocaleString('ko-KR') : '기록 없음'}</dd><dt>사유</dt><dd>{h.close_reason ?? '기록 없음'}</dd></dl>
    <p className="muted">클러스터는 변경하지 않았습니다. 이 장애에는 더 이상 지시를 받지 않으며, 같은 증상이 다시 감지되면 새 장애로 접수됩니다.</p>
  </article>;
}
