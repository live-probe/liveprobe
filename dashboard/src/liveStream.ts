// Resilient SSE subscription for /api/stream (KAN-49).
// The native EventSource retries on its own only while readyState is CONNECTING; after a non-200
// response (e.g. 502/503 while the API Pod restarts) it goes CLOSED for good. This helper reopens
// it with the last acknowledged event cursor (?since=<id>) and exponential backoff + jitter, and
// asks the caller to refetch its snapshot after a reconnect.

export const SSE_BACKOFF_MS = { initial: 1000, max: 30000 } as const;
const CLOSED = 2;

export interface SourceLike {
  readonly readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  addEventListener(type: string, listener: (ev: { lastEventId?: string }) => void): void;
  close(): void;
}

export interface LiveStreamDeps {
  createSource: (url: string) => SourceLike;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
  random?: () => number;
  onClosed?: () => void; // e.g. e.g. probe API health
}

export function streamUrl(cursor: string | null): string {
  return cursor ? `/api/stream?since=${encodeURIComponent(cursor)}` : '/api/stream';
}

/** Subscribe to incident events; `onChange` fires on every event and after every reconnect. Returns a stop function. */
export function subscribeIncidents(onChange: () => void, deps: LiveStreamDeps): () => void {
  const random = deps.random ?? Math.random;
  let source: SourceLike | null = null;
  let timer: unknown;
  let stopped = false;
  let cursor: string | null = null;
  let delay: number = SSE_BACKOFF_MS.initial;
  let failuresWithCursor = 0;
  let reconnecting = false;

  const connect = () => {
    if (stopped) return;
    const s = deps.createSource(streamUrl(cursor));
    source = s;
    s.addEventListener('incident', (e) => {
      if (stopped || source !== s) return;
      if (e.lastEventId) cursor = e.lastEventId;
      onChange();
    });
    s.onopen = () => {
      if (stopped || source !== s) return;
      delay = SSE_BACKOFF_MS.initial;
      failuresWithCursor = 0;
      if (reconnecting) { reconnecting = false; onChange(); }
    };
    s.onerror = () => {
      if (stopped || source !== s || s.readyState !== CLOSED) return; // stale or native browser retry
      s.close();
      reconnecting = true;
      // A cursor rejected by the server (409 expired/ahead) would fail forever: drop it after
      // repeated failures and rely on the snapshot refetch instead.
      if (cursor && ++failuresWithCursor >= 3) { cursor = null; failuresWithCursor = 0; }
      deps.onClosed?.();
      timer = deps.setTimeout(connect, delay + Math.floor(random() * 250));
      delay = Math.min(delay * 2, SSE_BACKOFF_MS.max);
    };
  };
  connect();
  return () => {
    stopped = true;
    if (timer !== undefined) deps.clearTimeout(timer);
    source?.close();
  };
}
