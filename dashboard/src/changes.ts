export type ReleaseStage = 'dev' | 'stg' | 'prd';
export type Verification = { ok?: boolean; rollout?: {status: string}; health?: {status:string}; repro?: {status:string}; exceptions?: {status:string; count?:number; observation_seconds?:number} };
export type ChangeBuild = { commit?:string; branch?:string; image?:string; image_id?:string; repository?:string; pr_url?:string; pr_number?:number; head_sha?:string; pr_state?:string; merge_sha?:string; main_sha?:string; review_completed?:boolean; review_checked_at?:string; merge_build?:{image?:string;commit?:string} };
export type StageEntry = {status:string;previous_image?:string;new_image?:string;started_at?:string;finished_at?:string;verification?:Verification;reverted?:boolean;rollback?:Verification&{status?:string};error?:string;blocked_reason?:string};
export type ChangeRequest = { id:number; incident_id:number; system:string; service:string; namespace_origin:string; status:string; patch_summary_ko:string|null; diff:string|null; diff_sha256?:string|null; tests:{total?:number|null;failed?:number|null;report_tail?:string;risk_ko?:string;added?:string[]}; build:ChangeBuild; stages:Partial<Record<ReleaseStage,StageEntry>>; error:string|null; created_at?:string;updated_at?:string };
export type PromotionPlan = { stage:ReleaseStage; namespace:string; deployment:string;container:string;previous_image:string;new_image:string;side_effects_ko:string[];rollback_ko:string;plan_sha256:string };
const STATUS_KO: Record<string,string> = {awaiting_source:'소스 수신 대기',patching:'패치 생성 중',awaiting_build:'시험·빌드 대기',building:'시험·빌드 중',built:'시험·빌드 완료',patch_failed:'패치 생성 실패',tests_failed:'회귀 시험 실패',build_failed:'이미지 빌드 실패',dev_deploying:'개발 환경 승격·검증 중',dev_verified:'개발 환경 검증 완료',dev_failed:'개발 환경 검증 실패',stg_deploying:'검증 환경 승격·검증 중',stg_verified:'검증 환경 검증 완료',stg_failed:'검증 환경 검증 실패',prd_deploying:'운영 환경 승격·검증 중',prd_verified:'운영 환경 검증 완료',prd_failed:'운영 환경 검증 실패',cancelled:'취소됨',error:'요청 준비 실패'};
export function changeProgress(c:ChangeRequest) {
  const labels=['소스 수신','패치 생성','시험·빌드','dev','stg','prd'];
  const positions:Record<string,number>={awaiting_source:0,patching:1,patch_failed:1,awaiting_build:2,building:2,tests_failed:2,build_failed:2,built:3,dev_deploying:3,dev_failed:3,dev_verified:4,stg_deploying:4,stg_failed:4,stg_verified:5,prd_deploying:5,prd_failed:5,prd_verified:6};
  const current=positions[c.status] ?? -1;
  return labels.map((label,i)=>({label,state:i<current?'완료':i===current?(c.status.endsWith('_failed')?'실패':c.status.endsWith('_deploying')||['awaiting_source','patching','building','awaiting_build'].includes(c.status)?'진행 중':'대기'):'대기'}));
}
export const changeStatusKo = (status:string) => STATUS_KO[status] ?? '상태 미확인';
export const nextStage = (c:ChangeRequest):ReleaseStage|null => ({built:'dev',dev_verified:'stg',stg_verified:'prd'} as Record<string,ReleaseStage>)[c.status] ?? null;
export const changePollMs = (changes:ChangeRequest[]) => changes.some(c=>['awaiting_source','patching','awaiting_build','building','dev_deploying','stg_deploying','prd_deploying'].includes(c.status)) ? 2000 : 10000;
export const canCancelChange = (c:ChangeRequest) => !['building','dev_deploying','stg_deploying','prd_deploying','prd_verified','cancelled'].includes(c.status);
export const promotionConfirmText = (p:PromotionPlan) => [`${p.stage} · ${p.deployment} 승격`, ...p.side_effects_ko, `원복 방법: ${p.rollback_ko}`, '정말 진행하시겠습니까?'].join('\n');
// 서버가 실행 전에 거절한 단계(blocked)는 클러스터를 바꾸지 않았으므로 실패·원복으로 표시하지 않는다.
export const stageHeadingKo = (e:StageEntry) => ({verified:'검증 완료',failed:'검증 실패',blocked:'실행 전 중단 · 변경 없음'} as Record<string,string>)[e.status] ?? '진행 중';
export function rollbackKo(e:StageEntry):string {
  if (e.status==='blocked') return '변경이 없어 원복하지 않음';
  if (e.reverted) return '이전 이미지로 원복·검증 완료';
  return e.rollback ? '원복 미확정 · 사람 확인 필요' : '해당 없음';
}
export function safePrUrl(b:ChangeBuild):string|null {
  const expected = `https://github.com/${b.repository}/pull/${b.pr_number}`;
  return /^live-probe\/[a-z0-9-]+$/.test(b.repository ?? '') && Number.isInteger(b.pr_number) && (b.pr_number ?? 0)>0 && b.pr_url === expected ? expected : null;
}
