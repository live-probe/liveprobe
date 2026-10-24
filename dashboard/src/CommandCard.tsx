import { useEffect, useRef, useState, type FormEvent } from 'react';
import { display, request, type Command } from './api';
import { commandControls, commandErrorKo, commandStatusKo, downtimeKo, opKo, reversibleKo, rollbackReviewKo, secretKeys, secretSizeError } from './commands';
import { ErrorBox, Spinner } from './ui';
export function CommandCard({ command: c, onChange }: { command: Command; onChange: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const form = useRef<HTMLFormElement>(null);
  const ctl = commandControls(c); const keys = secretKeys(c.plan?.operations);
  useEffect(() => { form.current?.reset(); setError(''); }, [c.id, c.status, c.plan_sha256]);
  async function act(action: 'confirm' | 'cancel' | 'rollback') {
    if (busy) return;
    if (action === 'rollback' && !window.confirm(`이 작업을 원복하시겠습니까? ${c.side_effects?.rollback_ko || '기록된 이전 상태로 되돌립니다.'}`)) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await request(`/commands/${c.id}/${action}`, { method: 'POST', ...(action === 'cancel' ? {} : { body: JSON.stringify({ plan_sha256: c.plan_sha256 }) }) });
      setNotice(action === 'confirm' ? '확인을 전달했습니다.' : action === 'cancel' ? '취소를 전달했습니다.' : '원복을 요청했습니다.'); onChange();
    } catch { setError('요청하지 못했습니다. 계획이 바뀌었을 수 있으니 최신 내용을 확인하고 다시 시도하세요.'); onChange(); }
    finally { setBusy(false); }
  }
  async function submitSecrets(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (busy || !c.plan_sha256 || !keys.length) return;
    const target = e.currentTarget;
    let values: Record<string, string> = Object.fromEntries(keys.map(k => [k, String(new FormData(target).get(k) ?? '')]));
    // No React state, event history, storage, logging, or rendering ever receives a value. Clear the DOM synchronously.
    const sizeError = secretSizeError(values, c.plan_sha256);
    if (sizeError) { values = {}; target.reset(); setError(`${sizeError} 값을 다시 입력하세요.`); return; }
    target.reset(); setBusy(true); setError('');
    try { const body = JSON.stringify({ plan_sha256: c.plan_sha256, values }); values = {}; await request(`/commands/${c.id}/secret-values`, { method: 'POST', body }); setNotice('비밀값을 전달했습니다. 값은 화면에 남기지 않습니다.'); onChange(); }
    catch { values = {}; setError('비밀값을 적용하지 못했습니다. 최신 계획을 확인한 뒤 값을 다시 입력하세요.'); onChange(); }
    finally { target.reset(); setBusy(false); }
  }
  const effects = c.side_effects; const review = rollbackReviewKo(c);
  return <article className={`command-card status-${c.status}`} aria-label={`운영자 지시 ${c.id}`} data-command-id={c.id}>
    <header><span className="eyebrow">운영자 지시 · #{c.id}</span><span className={`badge ${c.status === 'succeeded' || c.status === 'rolled_back' ? 'ok' : c.status === 'failed' || c.status === 'rejected' ? 'danger' : 'warn'}`} role="status">{ctl.busy && <Spinner />}{commandStatusKo(c.status)}</span></header>
    <p className="command-instruction">{c.instruction}</p>
    {c.plan && <><h4>실행 계획</h4><p>{c.plan.summary_ko || '계획 설명이 없습니다.'}</p><ol className="operations">{(c.plan.operations ?? []).map((o, index) => <li key={index}><b>{opKo(o.op)}</b><code>{o.kind} · {o.namespace ? `${o.namespace}/` : ''}{o.name}</code>{o.replicas != null && <span>목표 수량 {o.replicas}개</span>}{o.op === 'secret_update' && <small>변경할 키: {(o.keys ?? []).join(', ') || '미지정'} · 값은 별도 입력</small>}{(o.manifest != null || o.patch != null) && o.op !== 'secret_update' && <details><summary>변경 명세 원문</summary><pre className="raw-text">{display(o.manifest ?? o.patch)}</pre></details>}</li>)}</ol></>}
    {c.dry_run && <div className="dry-runs"><h4>사전 검사 결과</h4>{c.dry_run.length === 0 ? <p className="muted">사전 검사 결과가 없습니다.</p> : c.dry_run.map((d, i) => <div className="dry-run" key={i}><b className={d.ok ? 'ok-text' : 'danger-text'}>작업 {d.op_index + 1} · {d.ok ? '통과' : '실패'}</b>{d.error && <><pre className="raw-text">{d.error}</pre><p>사전 검사에서 오류가 발생했습니다.</p></>}{(d.diff ?? []).map((diff, j) => <div className="diff-row" key={j}><code>{diff.path}</code><div><span className="diff-before">{display(diff.before) ?? '없음'}</span><span aria-label="변경 후"> → </span><span className="diff-after">{display(diff.after) ?? '없음'}</span></div></div>)}</div>)}</div>}
    {effects && <section className="side-effects" aria-label="예상 부작용"><h4>예상 부작용</h4>{effects.cluster_wide && <div className="error cluster-warning" role="alert"><b>클러스터 전체 영향</b><p>노드 또는 클러스터 범위 자원을 변경합니다. 다른 서비스에도 영향이 갈 수 있습니다.</p></div>}<ul>{(effects.items_ko ?? []).map((s, i) => <li key={i}>{s}</li>)}</ul><dl className="kv"><dt>영향 대상</dt><dd>{(effects.targets ?? []).join(', ') || '미지정'}</dd><dt>재시작</dt><dd>{effects.restarts === true ? '재시작 있음' : effects.restarts === false ? '재시작 없음' : '미확인'}</dd><dt>서비스 중단</dt><dd>{downtimeKo(effects.downtime)}</dd><dt>원복</dt><dd>{reversibleKo(effects.reversible)}</dd></dl><p>{effects.rollback_ko || '원복 방법이 기록되지 않았습니다.'}</p>{(effects.warnings_ko ?? []).map((s, i) => <p key={i} className="notice">{s}</p>)}</section>}
    {ctl.confirm && <div className="confirm-gate"><strong>정말 진행하시겠습니까?</strong>{!ctl.confirmEnabled && <p className="notice">계획 해시, 부작용 설명, 모든 작업의 사전 검사 통과가 확인되어야 진행할 수 있습니다.</p>}<div className="inline"><button className="btn primary" disabled={busy || !ctl.confirmEnabled} onClick={() => void act('confirm')}>진행</button><button className="btn" disabled={busy} onClick={() => void act('cancel')}>취소</button></div></div>}
    {ctl.secret && <form ref={form} onSubmit={submitSecrets} className="secret-form" autoComplete="off"><h4>비밀값을 직접 입력하세요</h4><p className="notice">값은 AI에게 전달하거나 기록에 저장하지 않습니다. 전송 즉시 입력란을 비웁니다.</p>{keys.map(key => <label key={key}>{key}<input aria-label={`비밀값 ${key}`} name={key} type="password" autoComplete="off" spellCheck={false} required disabled={busy} maxLength={2048} /></label>)}{!keys.length && <p role="alert">입력할 키가 없습니다. 계획을 다시 확인하세요.</p>}<button className="btn primary" type="submit" disabled={busy || !keys.length || !c.plan_sha256}>비밀값 전달</button></form>}
    {ctl.cancel && !ctl.confirm && <button className="btn" disabled={busy} onClick={() => void act('cancel')}>취소</button>}
    {(c.result != null || c.verification != null || c.rollback != null) && <div className="command-results">{[['실행 결과', c.result], ['검증 결과', c.verification], ['원복 결과', c.rollback]].map(([label, value]) => value != null && <div key={String(label)}><h4>{String(label)}</h4><pre className="raw-text">{display(value)}</pre><p className="muted">서버가 반환한 {String(label)} 원문입니다.</p></div>)}</div>}
    {review && <div className="error" role="alert"><b>운영자 검토 필요</b><p>{review}</p></div>}
    {c.error && <div className="error" role="alert"><p>{commandErrorKo(c.error)}</p><pre className="raw-text">{c.error}</pre></div>}
    {ctl.rollback && <button className="btn" disabled={busy} onClick={() => void act('rollback')}>원복</button>}
    <ErrorBox>{error}</ErrorBox>{notice && <p role="status" className="muted">{notice}</p>}
  </article>;
}
