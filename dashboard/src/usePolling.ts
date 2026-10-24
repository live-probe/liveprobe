import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from './api';
/** One in-flight request per resource; response-driven timer, abort on route change, no interval overlaps. */
export function usePolling<T>(path: string, interval: number | ((data: T | null) => number)) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState(false); const [revision, setRevision] = useState(0);
  const intervalRef = useRef(interval); intervalRef.current = interval;
  const refresh = useCallback(() => setRevision(r => r + 1), []);
  useEffect(() => {
    let stopped = false; let timer: ReturnType<typeof setTimeout>; const controller = new AbortController();
    async function load() {
      let next: T | null = null;
      try { next = await request<T>(path, { signal: controller.signal }); if (!stopped) { setData(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next); setError(false); } }
      catch (e) { if (!stopped && !(e instanceof DOMException && e.name === 'AbortError')) setError(true); }
      if (!stopped) { const i = intervalRef.current; timer = setTimeout(load, typeof i === 'number' ? i : i(next)); }
    }
    void load(); return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [path, revision]);
  return { data, error, refresh };
}
