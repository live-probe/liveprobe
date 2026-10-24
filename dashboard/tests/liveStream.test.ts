// Run: node --test tests/liveStream.test.ts   (Node >= 23 native type stripping)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { subscribeIncidents, streamUrl, SSE_BACKOFF_MS, type SourceLike } from '../src/liveStream.ts';

class FakeSource implements SourceLike {
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;
  listeners: Record<string, ((ev: { lastEventId?: string }) => void)[]> = {};
  url: string;
  constructor(url: string) { this.url = url; }
  addEventListener(type: string, l: (ev: { lastEventId?: string }) => void) { (this.listeners[type] ??= []).push(l); }
  close() { this.closed = true; this.readyState = 2; }
  open() { this.readyState = 1; this.onopen?.({}); }
  emit(id: string) { for (const l of this.listeners.incident ?? []) l({ lastEventId: id }); }
  fail(state: number) { this.readyState = state; this.onerror?.({}); }
}

function harness() {
  const sources: FakeSource[] = [];
  const timers: { fn: () => void; ms: number; cleared: boolean }[] = [];
  let closedProbes = 0; let changes = 0;
  const stop = subscribeIncidents(() => { changes++; }, {
    createSource: url => { const s = new FakeSource(url); sources.push(s); return s; },
    setTimeout: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
    clearTimeout: h => { (h as { cleared: boolean }).cleared = true; },
    random: () => 0,
    onClosed: () => { closedProbes++; },
  });
  return { sources, timers, stop, get changes() { return changes; }, get probes() { return closedProbes; } };
}

test('streamUrl encodes the cursor', () => {
  assert.equal(streamUrl(null), '/api/stream');
  assert.equal(streamUrl('42'), '/api/stream?since=42');
});

test('reconnects with last event cursor after the EventSource closes, then refetches', () => {
  const h = harness();
  assert.equal(h.sources[0].url, '/api/stream');
  h.sources[0].open(); h.sources[0].emit('7'); h.sources[0].emit('9');
  assert.equal(h.changes, 2);
  h.sources[0].fail(2); // API Pod restart: 502 -> CLOSED
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.probes, 1);
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, SSE_BACKOFF_MS.initial);
  h.timers[0].fn();
  assert.equal(h.sources[1].url, '/api/stream?since=9');
  h.sources[1].open();
  assert.equal(h.changes, 3, 'snapshot refetch after reconnect');
});

test('native CONNECTING retries are left to the browser', () => {
  const h = harness();
  h.sources[0].open();
  h.sources[0].fail(0);
  assert.equal(h.timers.length, 0);
  assert.equal(h.sources[0].closed, false);
});

test('exponential backoff capped, reset on successful open', () => {
  const h = harness();
  const delays: number[] = [];
  for (let i = 0; i < 8; i++) { h.sources.at(-1)!.fail(2); delays.push(h.timers.at(-1)!.ms); h.timers.at(-1)!.fn(); }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  h.sources.at(-1)!.open();
  h.sources.at(-1)!.fail(2);
  assert.equal(h.timers.at(-1)!.ms, SSE_BACKOFF_MS.initial);
});

test('drops a repeatedly rejected cursor (409) and falls back to live stream', () => {
  const h = harness();
  h.sources[0].open(); h.sources[0].emit('5'); h.sources[0].fail(2);
  for (let i = 0; i < 3; i++) { h.timers.at(-1)!.fn(); assert.equal(h.sources.at(-1)!.url, i < 2 ? '/api/stream?since=5' : '/api/stream'); h.sources.at(-1)!.fail(2); }
});

test('events from an obsolete connection cannot rewind the replay cursor', () => {
  const h = harness();
  h.sources[0].open(); h.sources[0].emit('7');
  h.sources[0].fail(2); h.timers[0].fn();
  h.sources[1].open(); h.sources[1].emit('9');
  const changes = h.changes;
  h.sources[0].emit('2'); // late callback from the already-closed EventSource
  assert.equal(h.changes, changes);
  h.sources[1].fail(2); h.timers[1].fn();
  assert.equal(h.sources[2].url, '/api/stream?since=9');
});

test('stop closes the source and cancels pending reconnect', () => {
  const h = harness();
  h.sources[0].fail(2);
  h.stop();
  assert.equal(h.timers[0].cleared, true);
  h.timers[0].fn();
  assert.equal(h.sources.length, 1);
});
