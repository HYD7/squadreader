// Test helper (no production code imports this): a manual clock with
// setTimeout/clearTimeout and an async `advance` that lets promise callbacks
// (fake fetch) settle between timers.
import type { Timers } from "./liveUrls";

export interface FakeTimers extends Timers {
  now(): number;
  pending(): number;
  /** Run every timer due within `ms`, in order, flushing promises around each. */
  advance(ms: number): Promise<void>;
  flush(): Promise<void>;
}

export async function flush(settleMs = 0): Promise<void> {
  // Several turns of the event loop: a fake fetch -> json() / arrayBuffer() ->
  // next fetch chain needs more than one.
  for (let i = 0; i < 12; i++) await new Promise<void>((r) => setImmediate(r));
  if (settleMs > 0) await new Promise<void>((r) => setTimeout(r, settleMs));
}

/** `settleMs` > 0 also waits real time after each step (needed when a fake
 *  response is gzip: DecompressionStream finishes on the thread pool). */
export function fakeTimers(settleMs = 0): FakeTimers {
  let now = 0;
  let seq = 0;
  const q: { id: number; at: number; fn: () => void }[] = [];
  return {
    now: () => now,
    pending: () => q.length,
    setTimeout(fn, ms) {
      const id = ++seq;
      q.push({ id, at: now + Math.max(0, ms), fn });
      return id;
    },
    clearTimeout(h) {
      const i = q.findIndex((t) => t.id === h);
      if (i >= 0) q.splice(i, 1);
    },
    flush: () => flush(settleMs),
    async advance(ms) {
      const end = now + ms;
      await flush(settleMs);
      for (;;) {
        q.sort((a, b) => a.at - b.at || a.id - b.id);
        const t = q[0];
        if (!t || t.at > end) break;
        q.shift();
        now = Math.max(now, t.at);
        t.fn();
        await flush(settleMs);
      }
      now = end;
      await flush(settleMs);
    },
  };
}
