import type { ReactNode } from 'react';

const paths: Record<string, ReactNode> = {
  entry: <><path d="M3 12h12m-4-4 4 4-4 4M18 4h3v16h-3" /></>,
  data: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" /></>,
  resource: <><path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5" /></>,
  signal: <path d="M2 12h4l3-8 6 16 3-8h4" />,
  evidence: <><path d="M6 3h9l4 4v14H6V3Zm8 0v5h5M9 12h7M9 16h5" /></>,
  router: <><path d="m12 3 9 9-9 9-9-9 9-9ZM8 12h8m-4-4 4 4-4 4" /></>,
  group: <><rect x="8" y="3" width="8" height="6" rx="1" /><rect x="2" y="16" width="7" height="5" rx="1" /><rect x="15" y="16" width="7" height="5" rx="1" /><path d="M12 9v4H5v3m7-3h7v3" /></>,
  agent: <><rect x="5" y="5" width="14" height="14" rx="4" /><path d="M9 10h.01M15 10h.01M9 14h6M12 2v3M2 11h3m14 0h3M9 19v3m6-3v3" /></>,
  verify: <><path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4Z" /><path d="m8 12 3 3 5-6" /></>,
  action: <><path d="m14 2-9 12h6l-1 8 9-12h-6l1-8Z" /></>,
  outcome: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  steer: <><path d="M3 4h18v13H9l-6 4V4Zm5 5h8m-8 4h5" /></>,
  interpret: <><path d="M3 4h12M9 2v2M5 4c0 7 9 11 9 11M13 4c0 7-10 12-10 12m11 5 4-11 4 11m-6-4h4" /></>,
  namespace: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
};
/** Local line icons keep diagram roles legible without fonts or runtime dependencies. */
export function DiagramIcon({ kind }: { kind: string }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind === 'command' ? 'action' : kind] ?? paths.resource}</svg>;
}
