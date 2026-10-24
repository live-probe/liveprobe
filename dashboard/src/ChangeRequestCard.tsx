import { useState } from 'react';
import { request } from './api';
import { canCancelChange, cardErrorKo, changeProgress, changeStatusKo, nextStage, promotionConfirmText, rollbackKo, safePrUrl, stageHeadingKo, type ChangeRequest, type PromotionPlan, type ReleaseStage } from './changes';
import { ErrorBox, Spinner } from './ui';

export function ChangeRequestCard({ change: c, onChange }: {change:ChangeRequest;onChange:()=>void}) {
  const [busy,setBusy] = useState(false); const [error,setError] = useState('');
  const stage=nextStage(c); const pr=safePrUrl(c.build);
  async function promote() {
    if (busy || !stage) return;
    setBusy(true); setError('');
    try {
      const p = await request<PromotionPlan>(`/changes/${c.id}/plan?stage=${stage}`);
      if (!p.plan_sha256 || !p.side_effects_ko?.length || !p.rollback_ko) throw new Error('invalid plan');
      if (!window.confirm(promotionConfirmText(p))) return;
      await request(`/changes/${c.id}/promote`, {method:'POST',body:JSON.stringify({stage,plan_sha256:p.plan_sha256,confirm:true})});
      onChange();
    } catch { setError('승격하지 못했습니다. 최신 계획·이미지·단계 순서와 GitHub 병합 상태를 다시 확인하세요.'); onChange(); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (busy) return; setBusy(true); setError('');
    try { await request(`/changes/${c.id}/cancel`, {method:'POST'}); onChange(); }
    catch { setError('취소하지 못했습니다. 실행 중인 단계는 검증·원복이 끝날 때까지 취소할 수 없습니다.'); onChange(); }
    finally { setBusy(false); }
  }
  const running = ['patching','building','dev_deploying','stg_deploying','prd_deploying'].includes(c.status);
  const cardError = cardErrorKo(c);
  return <article className="command-card change-request-card" aria-label={`변경 요청 ${c.id}`} style={{minWidth:0,maxWidth:'100%',flexShrink:0,overflow:'hidden',overflowWrap:'anywhere'}}>
    <header><b>변경 요청 · #{c.id}</b><span role="status" className={`badge ${c.status.endsWith('_failed') || c.status==='error' ? 'danger' : running ? 'info' : 'ok'}`}>{running && <Spinner/>}{changeStatusKo(c.status)}</span></header>
    <p>{c.namespace_origin} · {c.system} · {c.service}</p>
    <ol aria-label="변경 단계" style={{display:'flex',flexWrap:'wrap',gap:'4px 12px',paddingLeft:16}}>{changeProgress(c).map(s=><li key={s.label} aria-current={s.state==='진행 중'?'step':undefined}><b>{s.label}</b> · {s.state}</li>)}</ol>
    <section><h4>패치 요약</h4><p>{c.patch_summary_ko || '소스와 진단을 확인한 뒤 수정안을 만듭니다.'}</p>{c.tests.risk_ko && <p className="muted">위험: {c.tests.risk_ko}</p>}</section>
    {c.diff && <details style={{minWidth:0,maxWidth:'100%'}}><summary>패치 원문 보기</summary><pre style={{fontFamily:'var(--mono)',whiteSpace:'pre',overflowX:'auto',maxWidth:'100%',margin:0,padding:8,background:'var(--surface)'}}>{c.diff}</pre></details>}
    <dl className="kv"><dt>회귀 시험</dt><dd>{c.tests.total == null ? '미수행' : `총 ${c.tests.total}개 · 실패 ${c.tests.failed ?? '미확인'}개`}</dd><dt>새 이미지</dt><dd style={{minWidth:0,overflowWrap:'anywhere'}}>{c.build.image || '빌드 대기'}</dd>{c.build.merge_build?.image && <><dt>병합 이미지</dt><dd>{c.build.merge_build.image}</dd></>}</dl>
    {c.tests.report_tail && <details><summary>시험 결과 원문</summary><pre className="raw-text">{c.tests.report_tail}</pre></details>}
    <section aria-label="GitHub 검토"><h4>코드 검토</h4>{pr ? <><a href={pr} target="_blank" rel="noopener noreferrer" style={{color:'var(--accent)',textDecoration:'underline'}}>실제 PR #{c.build.pr_number} 열기</a><p>상태: {{OPEN:'검토 중',MERGED:'병합됨',CLOSED:'닫힘'}[c.build.pr_state ?? ''] || '미조회'}</p><p>수정 커밋: <code>{c.build.head_sha || '미기록'}</code></p>{c.build.merge_sha && <p>병합 커밋: <code>{c.build.merge_sha}</code></p>}</> : <p className="muted">실제 PR이 생성되면 링크를 표시합니다.</p>}<p>GitHub에서 검토·병합하세요. 자동 병합하지 않습니다. 운영 승격은 최신 병합 커밋·재빌드 확인 후 가능합니다.</p></section>
    {(['dev','stg','prd'] as ReleaseStage[]).map(s=>{const entry=c.stages[s];if(!entry || entry.status==='planned')return null;const v=entry.verification;return <section key={s} aria-label={`${s} 검증 결과`}><h4>{s} 단계 · {stageHeadingKo(entry)}</h4><dl className="kv" style={{wordBreak:'keep-all'}}><dt>롤아웃</dt><dd>{v?.rollout?.status || '미수행'}</dd><dt>파드 준비</dt><dd>{v?.health?.status || '미수행'}</dd><dt>장애 요청 재현</dt><dd>{v?.repro?.status || '미수행'}</dd><dt>새 예외 로그</dt><dd>{v?.exceptions?.status || '미관측'}{v?.exceptions?.count != null ? ` · ${v.exceptions.count}건` : ''}</dd><dt>자동 원복</dt><dd>{rollbackKo(entry)}</dd></dl>{entry.previous_image && <p className="muted">이전 이미지: {entry.previous_image}</p>}{entry.error && <p role="alert" className={entry.status==='blocked' ? 'notice' : 'error'} style={{margin:'8px 0 0'}}>{entry.error}</p>}</section>;})}
    {cardError && <div className="error" role="alert">{cardError}</div>}
    <div className="inline">{stage && <button type="button" className="btn primary" disabled={busy || (stage==='prd' && !c.build.review_completed)} onClick={()=>void promote()}>{busy?'계획 확인 중':`${stage}로 승격`}</button>}{canCancelChange(c) && <button type="button" className="btn" disabled={busy} onClick={()=>void cancel()}>변경 요청 취소</button>}</div>
    {stage==='prd' && !c.build.review_completed && <p className="notice">운영 승격 전 GitHub 검토·병합과 정확한 병합 커밋의 시험·빌드가 필요합니다.</p>}
    <ErrorBox>{error}</ErrorBox>
  </article>;
}
