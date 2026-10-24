import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { loadOpenIncidents } from './liveSnapshot';
import { subscribeIncidents, type SourceLike } from './liveStream';
import { request, type Incident } from './api';
import { diffAlerts, alertText, type AlertEvent } from './feed';

export type Accounts = { mode: string; active?: string | null; accounts: { name: string; state: string; blocked_for_s?: number }[]; note?: string };
type Toast = AlertEvent & { key: number };
type Ctx = { open: Incident[]; openState: 'loading' | 'ready' | 'error'; accounts: Accounts | null; refresh: () => void; toasts: Toast[]; dismiss: (k: number) => void; notifyState: NotificationPermission | 'unsupported'; askNotify: () => void };
const C = createContext<Ctx | null>(null);
export const useLive = () => { const v = useContext(C); if (!v) throw new Error('LiveProvider missing'); return v; };

/** Single live data source for the whole console: SSE-driven refresh plus a slow poll as a safety net. */
export function LiveProvider({ children, onOpenIncident }: { children: ReactNode; onOpenIncident: (id: number) => void }) {
  const [open, setOpen] = useState<Incident[]>([]);
  const [openState, setOpenState] = useState<'loading' | 'ready' | 'error'>('loading');
  const loadingOpen = useRef(false);
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const prev = useRef<Incident[] | null>(null);
  const key = useRef(0);
  const [notifyState, setNotifyState] = useState<NotificationPermission | 'unsupported'>(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission);

  const pushAlerts = useCallback((events: AlertEvent[]) => {
    for (const a of events) {
      const k = ++key.current;
      setToasts(t => [...t.slice(-3), { ...a, key: k }]);
      window.setTimeout(() => setToasts(t => t.filter(x => x.key !== k)), 12000);
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted' && a.kind !== 'resolved') {
        try { const n = new Notification(alertText(a), { body: a.title, tag: `incident-${a.id}` }); n.onclick = () => { window.focus(); onOpenIncident(a.id); }; } catch { /* some browsers forbid it */ }
      }
    }
  }, [onOpenIncident]);

  const refresh = useCallback(() => {
    request<{ items: Incident[] }>('/incidents?limit=200').then(d => { pushAlerts(diffAlerts(prev.current, d.items)); prev.current = d.items; }).catch(() => undefined);
    if (!loadingOpen.current) {
      loadingOpen.current = true;
      loadOpenIncidents(path => request(path)).then(items => { setOpen(items); setOpenState('ready'); }).catch(() => setOpenState('error')).finally(() => { loadingOpen.current = false; });
    }
    request<Accounts>('/llm/accounts').then(setAccounts).catch(() => undefined);
  }, [pushAlerts]);

  useEffect(() => { refresh(); const t = window.setInterval(refresh, 5000); return () => window.clearInterval(t); }, [refresh]);
  useEffect(() => subscribeIncidents(refresh, {
    createSource: url => new EventSource(url) as unknown as SourceLike,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms), clearTimeout: h => window.clearTimeout(h as number),
  }), [refresh]);

  const askNotify = useCallback(() => { if (typeof Notification !== 'undefined') Notification.requestPermission().then(setNotifyState); }, []);
  const dismiss = useCallback((k: number) => setToasts(t => t.filter(x => x.key !== k)), []);
  return <C.Provider value={{ open, openState, accounts, refresh, toasts, dismiss, notifyState, askNotify }}>{children}</C.Provider>;
}
