// Administrator live source: Server-Sent Events from Zero Zone.
//
//   EventSource('/admin/api/squadreader/canli/<server>/akis?bilet=<ticket>')
//
// Events: `frame` (data = full or position frame JSON), `match`
// ({matchId, state}), `reset`, `hb`. The stream is not delayed by the server;
// the viewer draws it 2 s in the past (ADMIN_RENDER_DELAY_MS).
//
// EventSource retries by itself while the network is down, but it gives up for
// good on an HTTP error (a 401 for an expired ticket, a 503 ...). So this
// module owns reconnection: on a closed stream it opens a new EventSource with
// exponential back-off, and a watchdog reconnects a stream that went silent.
//
// The ticket lives only in the URL query. It is never logged, never stored and
// never included in an error message.
import {
  adminStreamUrl, realTimers,
  type ConnState, type LiveSource, type SourceListener, type Timers,
} from "./liveUrls";

/** K7: the admin feed is drawn this far in the past. */
export const ADMIN_RENDER_DELAY_MS = 2_000;
export const SSE_BACKOFF_MIN_MS = 1_000;
export const SSE_BACKOFF_MAX_MS = 30_000;
/** No event (frame / hb / ...) for this long: the connection is dead. */
export const SSE_SILENCE_MS = 45_000;
/** Consecutive failed connections before the state turns from "reconnecting"
 *  to "closed" (it keeps retrying either way). */
export const SSE_GIVE_UP_AFTER = 6;
/** Consecutive connections that fail without ever opening before the ticket's
 *  age is checked (EventSource does not expose the HTTP status of a 403). */
export const SSE_EXPIRED_CHECK_AFTER = 3;
/** A single SSE payload larger than this is dropped. */
export const SSE_MAX_DATA_CHARS = 16 * 1024 * 1024;

/** The slice of EventSource this module uses (lets tests inject a fake). */
export interface EventSourceLike {
  readonly readyState: number;
  addEventListener(type: string, fn: (ev: MessageEventLike) => void): void;
  close(): void;
  onopen: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}
export interface MessageEventLike { data?: unknown }
export type EventSourceFactory = (url: string) => EventSourceLike;

const ES_CLOSED = 2;

/** True only when the ticket (`v1.<base64url JSON {.., e: epoch seconds}>.<sig>`)
 *  decodes and its `e` is in the past. Only the expiry is read; nothing else of
 *  the payload is kept and the ticket is never logged. Any decode problem
 *  returns false (the normal reconnect loop goes on). */
export function isTicketExpired(ticket: string, nowMs: number): boolean {
  try {
    const parts = ticket.split(".");
    if (parts.length < 2 || !parts[1]) return false;
    let b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    const obj: unknown = JSON.parse(new TextDecoder().decode(
      Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))));
    const e = obj && typeof obj === "object" ? (obj as { e?: unknown }).e : undefined;
    return typeof e === "number" && Number.isFinite(e) && e * 1000 <= nowMs;
  } catch {
    return false;
  }
}

export const browserEventSource: EventSourceFactory = (url) =>
  new EventSource(url, { withCredentials: true }) as unknown as EventSourceLike;

export interface SseSourceOptions {
  server: string;
  ticket: string;
  onEvent: SourceListener;
  factory?: EventSourceFactory;
  timers?: Timers;
  random?: () => number;
  /** Epoch milliseconds (tests inject a fixed clock). */
  now?: () => number;
}

export function createSseSource(opts: SseSourceOptions): LiveSource {
  const factory = opts.factory ?? browserEventSource;
  const timers = opts.timers ?? realTimers;
  const random = opts.random ?? Math.random;
  const now = opts.now ?? Date.now;
  const emit = opts.onEvent;

  let running = false;
  let gen = 0;
  let es: EventSourceLike | null = null;
  let retryTimer: unknown = null;
  let watchdog: unknown = null;
  let failures = 0;
  let everOpened = false;
  let conn: ConnState | null = null;

  function setConn(state: ConnState) {
    if (conn === state) return;
    conn = state;
    emit({ type: "conn", state });
  }
  function clearTimers() {
    if (retryTimer !== null) { timers.clearTimeout(retryTimer); retryTimer = null; }
    if (watchdog !== null) { timers.clearTimeout(watchdog); watchdog = null; }
  }
  function closeStream() {
    if (es) {
      es.onopen = null; es.onerror = null;
      try { es.close(); } catch { /* already closed */ }
      es = null;
    }
  }
  function backoffMs(): number {
    const base = Math.min(SSE_BACKOFF_MAX_MS,
      SSE_BACKOFF_MIN_MS * 2 ** Math.min(Math.max(0, failures - 1), 6));
    return Math.round(base * (0.8 + random() * 0.4));
  }
  function armWatchdog(myGen: number) {
    if (watchdog !== null) timers.clearTimeout(watchdog);
    watchdog = timers.setTimeout(() => {
      if (!running || myGen !== gen) return;
      failed(myGen);                    // silent for too long: reconnect
    }, SSE_SILENCE_MS);
  }
  function failed(myGen: number) {
    if (!running || myGen !== gen) return;
    closeStream();
    if (watchdog !== null) { timers.clearTimeout(watchdog); watchdog = null; }
    failures++;
    if (failures >= SSE_EXPIRED_CHECK_AFTER && isTicketExpired(opts.ticket, now())) {
      // The server answers an expired ticket with 403 and EventSource closes
      // for good; retrying with the same ticket is pointless.
      setConn("ticket_expired");
      return;
    }
    setConn(failures >= SSE_GIVE_UP_AFTER ? "closed"
            : everOpened ? "reconnecting" : "connecting");
    retryTimer = timers.setTimeout(() => { retryTimer = null; open(); }, backoffMs());
  }

  function parse(data: unknown): unknown | undefined {
    if (typeof data !== "string" || data.length === 0
        || data.length > SSE_MAX_DATA_CHARS) return undefined;
    try { return JSON.parse(data); } catch { return undefined; }
  }

  function open() {
    if (!running) return;
    const myGen = ++gen;
    closeStream();
    let stream: EventSourceLike;
    try {
      stream = factory(adminStreamUrl(opts.server, opts.ticket));
    } catch {
      failed(myGen);
      return;
    }
    es = stream;
    armWatchdog(myGen);
    stream.onopen = () => {
      if (!running || myGen !== gen) return;
      everOpened = true;
      failures = 0;
      setConn("live");
      emit({ type: "info", info: {
        serverId: opts.server, epoch: null, delaySec: null, live: true, ended: false,
      } });
      armWatchdog(myGen);
    };
    stream.onerror = () => {
      if (!running || myGen !== gen) return;
      if (stream.readyState === ES_CLOSED) {
        failed(myGen);                  // the browser gave up: reopen ourselves
      } else {
        // The browser is retrying on its own (network blip).
        setConn(everOpened ? "reconnecting" : "connecting");
      }
    };
    const alive = () => running && myGen === gen;
    stream.addEventListener("frame", (ev) => {
      if (!alive()) return;
      armWatchdog(myGen);
      const obj = parse(ev.data);
      if (obj !== undefined) emit({ type: "line", obj });
    });
    stream.addEventListener("match", (ev) => {
      if (!alive()) return;
      armWatchdog(myGen);
      const obj = parse(ev.data);
      if (obj && typeof obj === "object") {
        const o = obj as { matchId?: unknown; state?: unknown };
        if (typeof o.state === "string") {
          emit({ type: "match",
                 matchId: typeof o.matchId === "string" || typeof o.matchId === "number"
                   ? String(o.matchId) : null,
                 state: o.state });
        }
      }
    });
    stream.addEventListener("reset", (ev) => {
      if (!alive()) return;
      armWatchdog(myGen);
      const obj = parse(ev.data);
      const epoch = obj && typeof obj === "object"
        ? (obj as { epoch?: unknown }).epoch : undefined;
      emit({ type: "line", obj: typeof epoch === "number"
        ? { t: "reset", epoch } : { t: "reset" } });
    });
    stream.addEventListener("hb", () => {
      if (!alive()) return;
      armWatchdog(myGen);
    });
  }

  function stop() {
    running = false;
    gen++;
    clearTimers();
    closeStream();
  }

  return {
    start() {
      if (running) return;
      running = true;
      setConn(everOpened ? "reconnecting" : "connecting");
      open();
    },
    stop,
    restart() {
      stop();
      failures = 0; conn = null;
      running = true;
      setConn(everOpened ? "reconnecting" : "connecting");
      open();
    },
  };
}
