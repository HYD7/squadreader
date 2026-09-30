// "Maç bitti" layer state machine (pure: fetch, timers and navigation are
// injected, so it is tested with fake timers).
//
//   hidden --show(token)--> pending --{state:'ready',url}--> ready (countdown)
//                              |  ^                            |
//                              |  '-- {state:'pending'} / errors (poll again)
//                              '-- too many polls --> failed
//
// Public feed: polls GET /canli/api/<server>/sonuc/<matchToken> every 5 s; when
// the match record is ready it counts down 10 s and goes to its `url`.
// Administrator feed (`redirect: false`): information only, never navigates
// and never polls (the SSE stream carries no match token).
import {
  isSafeSitePath, realTimers, resultUrl, type Timers,
} from "./liveUrls";
import { parseRetryAfterMs } from "./segmentSource";

export const END_POLL_MS = 5_000;
export const END_COUNTDOWN_SEC = 10;
/** 120 polls x 5 s = 10 min, then "kayda ulaşılamadı". */
export const END_MAX_POLLS = 120;

export type EndState =
  | { phase: "hidden" }
  /** Admin, or no usable match token: nothing to wait for. */
  | { phase: "info"; hasToken: boolean }
  | { phase: "pending" }
  | { phase: "ready"; url: string; hasReplay: boolean; countdown: number | null }
  | { phase: "failed" };

export interface EndMachineOptions {
  server: string;
  /** false = administrator view: information only. */
  redirect: boolean;
  onChange: (state: EndState) => void;
  /** "Sonraki maçı izle": restart the source. */
  onNext: () => void;
  navigate: (url: string) => void;
  fetchImpl?: typeof fetch;
  timers?: Timers;
  pollMs?: number;
  countdownSec?: number;
  maxPolls?: number;
}

export interface EndMachine {
  show(matchToken: string | null): void;
  /** Admin: the next match started, drop the layer. */
  hide(): void;
  next(): void;
  goto(): void;
  state(): EndState;
  destroy(): void;
}

export function createEndMachine(opts: EndMachineOptions): EndMachine {
  const fetchImpl: typeof fetch = opts.fetchImpl ?? ((...a) => globalThis.fetch(...a));
  const timers = opts.timers ?? realTimers;
  const pollMs = opts.pollMs ?? END_POLL_MS;
  const countdownSec = opts.countdownSec ?? END_COUNTDOWN_SEC;
  const maxPolls = opts.maxPolls ?? END_MAX_POLLS;

  let cur: EndState = { phase: "hidden" };
  let gen = 0;                 // bumps on every show/hide/next: stale work stops
  let timer: unknown = null;
  let abort: AbortController | null = null;
  let polls = 0;

  function set(next: EndState) {
    cur = next;
    opts.onChange(next);
  }
  function clear() {
    gen++;
    if (timer !== null) { timers.clearTimeout(timer); timer = null; }
    abort?.abort();
    abort = null;
  }
  function after(ms: number, myGen: number, fn: () => void) {
    if (myGen !== gen) return;
    timer = timers.setTimeout(() => { timer = null; if (myGen === gen) fn(); }, ms);
  }

  async function poll(myGen: number, token: string): Promise<void> {
    if (myGen !== gen) return;
    polls++;
    let wait = pollMs;
    try {
      abort = new AbortController();
      const res = await fetchImpl(resultUrl(opts.server, token), {
        credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json" }, signal: abort.signal,
      });
      if (myGen !== gen) return;
      if (res.status === 429) {
        wait = parseRetryAfterMs(res.headers.get("Retry-After"), pollMs);
      } else if (res.ok) {
        const body = await res.json() as
          { state?: unknown; url?: unknown; replay?: unknown } | null;
        if (myGen !== gen) return;
        if (body && body.state === "ready") {
          if (isSafeSitePath(body.url)) {
            ready(myGen, body.url, body.replay != null && body.replay !== false);
          } else {
            set({ phase: "failed" });
          }
          return;
        }
        // {state:'pending'} or anything else: keep waiting.
      }
      // 404 / 5xx / other statuses: the record may simply not exist yet.
    } catch (err) {
      if (myGen !== gen) return;
      if ((err as { name?: string } | null)?.name === "AbortError") return;
    }
    if (polls >= maxPolls) { set({ phase: "failed" }); return; }
    after(wait, myGen, () => { void poll(myGen, token); });
  }

  function ready(myGen: number, url: string, hasReplay: boolean) {
    if (!opts.redirect) {
      set({ phase: "ready", url, hasReplay, countdown: null });
      return;
    }
    let left = countdownSec;
    set({ phase: "ready", url, hasReplay, countdown: left });
    const tick = () => {
      left--;
      if (left <= 0) {
        set({ phase: "ready", url, hasReplay, countdown: 0 });
        opts.navigate(url);
        return;
      }
      set({ phase: "ready", url, hasReplay, countdown: left });
      after(1000, myGen, tick);
    };
    after(1000, myGen, tick);
  }

  return {
    show(matchToken) {
      clear();
      const myGen = gen;
      polls = 0;
      if (!opts.redirect) { set({ phase: "info", hasToken: matchToken !== null }); return; }
      if (matchToken === null) { set({ phase: "info", hasToken: false }); return; }
      set({ phase: "pending" });
      void poll(myGen, matchToken);
    },
    hide() {
      clear();
      if (cur.phase !== "hidden") set({ phase: "hidden" });
    },
    next() {
      clear();
      set({ phase: "hidden" });
      opts.onNext();
    },
    goto() {
      if (cur.phase === "ready") {
        const url = cur.url;
        clear();
        opts.navigate(url);
      }
    },
    state() { return cur; },
    destroy() { clear(); },
  };
}
