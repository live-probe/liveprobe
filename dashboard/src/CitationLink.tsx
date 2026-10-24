import type { Citation } from './api';

/** Never use a citation-provided URL until its scheme and authority are checked. */
export function safeCitationUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) return null;
    return url.href;
  } catch { return null; }
}

export function CitationLink({ citation }: { citation: Citation }) {
  const href = safeCitationUrl(citation.source_uri);
  const label = `${citation.doc}${citation.locator ? ` · ${citation.locator}` : ''}`;
  return href ? <a href={href} target="_blank" rel="noopener noreferrer">{label} ↗</a> : <span>{label} (원문 링크 없음)</span>;
}
