// Public live source: polls the delayed segment feed of Zero Zone.
//
//   GET /canli/api/<server>/durum        -> status JSON (polled every 5 s)
//   GET /canli/api/<server>/parca/<seq>  -> gzip NDJSON segment (5 s of frames)
//
// The source is only transport: it reports connection state, the status info
// and every parsed NDJSON line, strictly in segment order. Turning lines into
// snapshots (and pacing them) is liveFrames.ts. It never talks to anything but
// same-origin paths and never logs response bodies.
import {
  segmentUrl, statusUrl, realTimers,
  type ConnState, type LiveInfo, type LiveSource, type SourceListener, type Timers,
} from "./liveUrls";

export const SEGMENT_POLL_MS = 5_000;
/** Segments to start behind the newest one: a small lead so playback does not
 *  run dry while the next segment is being fetched. */
export const START_BACK_SEGMENTS = 3;
/** Fetch at most this many segments per cycle (catch-up is paced, not a burst). */
export const MAX_SEGMENTS_PER_CYCLE = 8;
export const CLOSED_POLL_MS = 30_000;
export const FULL_POLL_MS = 10_000;
export const BACKOFF_MIN_MS = 1_000;
export const BACKOFF_MAX_MS = 30_000;
export const MAX_SEGMENT_BYTES = 32 * 1024 * 1024;

// The viewer slot token from `durum` is sent back on later requests. The
// contract (CANLI.md) had not fixed the transport when this was written, so it
// goes both as a header and as `?v=`; the server may read either. Change here.
export const VIEWER_TOKEN_HEADER = "X-Viewer-Token";
export const VIEWER_TOKEN_QUERY = "v";

export interface StatusResponse {
  serverId: string | null;
  epoch: number | null;
  latestSeq: number;
  oldestSeq: number;
  delaySec: number | null;
  live: boolean;
  ended: boolean;
  viewerToken: string | null;
  full: boolean;
}

function int(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.trunc(v) : null;
}

/** Validate the `durum` body; null when it is not an object. */
export function parseStatus(body: unknown): StatusResponse | null {
  if (!body || typeof body !== "object") return null;
  const o = body as Record<string, unknown>;
  const latest = int(o.latestSeq), oldest = int(o.oldestSeq);
  const token = typeof o.viewerToken === "string" && o.viewerToken.length <= 512
    ? o.viewerToken : null;
  return {
    serverId: typeof o.serverId === "string" ? o.serverId : null,
    epoch: int(o.epoch),
    latestSeq: latest ?? 0,
    oldestSeq: oldest ?? (latest ?? 0),
    delaySec: int(o.delaySec),
    live: o.live === true,
    ended: o.ended === true,
    viewerToken: token,
    full: o.full === true,
  };
}

/** `Retry-After` in seconds (HTTP-date form is treated as unknown). */
export function parseRetryAfterMs(v: string | null | undefined,
                                  fallbackMs: number): number {
  if (!v) return fallbackMs;
  const n = Number(v.trim());
  if (!Number.isFinite(n) || n < 0) return fallbackMs;
  return Math.max(1_000, Math.min(120_000, Math.round(n * 1000)));
}

export interface LimitBody {
  error: string | null;
  retryAfterSec: number | null;
}

/** Error body of `durum` / `parca` (`{error, retryAfterSec}`); never throws. */
async function readLimitBody(res: Response): Promise<LimitBody> {
  try {
    const o = await res.json() as Record<string, unknown> | null;
    if (!o || typeof o !== "object") return { error: null, retryAfterSec: null };
    return {
      error: typeof o.error === "string" ? o.error : (o.full === true ? "full" : null),
      retryAfterSec: int(o.retryAfterSec),
    };
  } catch {
    return { error: null, retryAfterSec: null };
  }
}

/** Wait before the next attempt: the `Retry-After` header wins, then the body's
 *  `retryAfterSec`, then `fallbackMs`. */
export function limitWaitMs(res: Response, body: LimitBody, fallbackMs: number): number {
  const header = res.headers.get("Retry-After");
  if (header) return parseRetryAfterMs(header, fallbackMs);
  if (body.retryAfterSec !== null && body.retryAfterSec >= 0) {
    return parseRetryAfterMs(String(body.retryAfterSec), fallbackMs);
  }
  return fallbackMs;
}

/** Split NDJSON text into parsed objects, skipping blank / unparsable lines. */
export function parseNdjson(text: string): { lines: unknown[]; bad: number } {
  const lines: unknown[] = [];
  let bad = 0;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    try { lines.push(JSON.parse(line)); } catch { bad++; }
  }
  return { lines, bad };
}

/** Body bytes -> text. The browser normally inflates `Content-Encoding: gzip`
 *  itself; if the bytes still start with the gzip magic (a proxy that hides the
 *  header, or `application/gzip`), inflate them here with DecompressionStream. */
export async function bodyToText(res: Response): Promise<string> {
  const buf = new Uint8Array(await res.arrayBuffer());
  if (buf.byteLength > MAX_SEGMENT_BYTES) throw new Error("segment_too_large");
  if (buf.length > 2 && buf[0] === 0x1f && buf[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") throw new Error("no_gunzip");
    const stream = new Blob([buf]).stream()
      .pipeThrough(new DecompressionStream("gzip"));
    const reader = stream.getReader();
    const parts: Uint8Array[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_SEGMENT_BYTES) {
        await reader.cancel();
        throw new Error("segment_too_large");
      }
      parts.push(value);
    }
    const all = new Uint8Array(total);
    let off = 0;
    for (const p of parts) { all.set(p, off); off += p.byteLength; }
    return new TextDecoder().decode(all);
  }
  return new TextDecoder().decode(buf);
}

export interface SegmentSourceOptions {
  server: string;
  onEvent: SourceListener;
  fetchImpl?: typeof fetch;
  timers?: Timers;
  random?: () => number;
  pollMs?: number;
}

export interface SegmentSource extends LiveSource {
  /** Read-only progress for the test hook. */
  stats(): { nextSeq: number | null; lastSeq: number | null; epoch: number | null;
             segments: number; ended: boolean };
}

export function createSegmentSource(opts: SegmentSourceOptions): SegmentSource {
  const fetchImpl: typeof fetch = opts.fetchImpl
    ?? ((...a) => globalThis.fetch(...a));
  const timers = opts.timers ?? realTimers;
  const random = opts.random ?? Math.random;
  const pollMs = opts.pollMs ?? SEGMENT_POLL_MS;
  const emit = opts.onEvent;

  let running = false;
  let run = 0;                       // generation: stale async work checks it
  let timer: unknown = null;
  let abort: AbortController | null = null;
  let nextSeq: number | null = null;
  let lastSeq: number | null = null;
  let epoch: number | null = null;
  let viewerToken: string | null = null;
  let hadSuccess = false;
  let failures = 0;
  let segments = 0;
  let ended = false;
  // Segment that carried the last `end` marker: a restart resumes AFTER it, so
  // "Sonraki maçı izle" cannot land on the same end marker again.
  let resumeAfterSeq = 0;
  let conn: ConnState | null = null;

  function setConn(state: ConnState) {
    if (conn === state) return;
    conn = state;
    emit({ type: "conn", state });
  }
  function failConn() {
    setConn(hadSuccess ? "reconnecting" : "connecting");
  }
  function backoffMs(): number {
    const step = Math.min(Math.max(0, failures - 1), 6);
    const base = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** step);
    return Math.round(base * (0.8 + random() * 0.4));
  }
  function schedule(ms: number, gen: number) {
    if (!running || gen !== run) return;
    timer = timers.setTimeout(() => { void cycle(gen); }, ms);
  }
  function withToken(url: string): string {
    if (!viewerToken) return url;
    return `${url}?${VIEWER_TOKEN_QUERY}=${encodeURIComponent(viewerToken)}`;
  }
  function request(url: string): Promise<Response> {
    const headers: Record<string, string> = {
      Accept: "application/x-ndjson, application/json",
    };
    if (viewerToken) headers[VIEWER_TOKEN_HEADER] = viewerToken;
    return fetchImpl(withToken(url), {
      credentials: "same-origin", cache: "no-store", headers,
      signal: abort?.signal,
    });
  }

  async function cycle(gen: number): Promise<void> {
    if (!running || gen !== run) return;
    let nextMs = pollMs;
    let limited = false;              // waiting out a server-given delay
    try {
      const res = await request(statusUrl(opts.server));
      if (gen !== run) return;
      if (res.status === 429 || res.status === 503) {
        // 503 {error:'full'}: viewer limit reached; 429 {error:'ip_limit'}: too
        // many viewers from this network. Anything else on 429 is the plain
        // rate limit (wait, keep the state); other 503s are ordinary failures.
        const body = await readLimitBody(res);
        if (gen !== run) return;
        if (body.error === "full") {
          setConn("full");
          schedule(limitWaitMs(res, body, FULL_POLL_MS), gen);
          return;
        }
        if (body.error === "ip_limit") {
          setConn("ip_limit");
          schedule(limitWaitMs(res, body, FULL_POLL_MS), gen);
          return;
        }
        if (res.status === 503) throw new Error("status_503");
        schedule(limitWaitMs(res, body, pollMs), gen);
        return;
      }
      if (res.status === 404) {
        setConn("closed");
        schedule(CLOSED_POLL_MS, gen);
        return;
      }
      if (!res.ok) throw new Error(`status_${res.status}`);
      const st = parseStatus(await res.json());
      if (gen !== run) return;
      if (!st) throw new Error("bad_status");
      hadSuccess = true;
      failures = 0;
      viewerToken = st.viewerToken ?? viewerToken;
      if (st.full) {
        setConn("full");
        schedule(FULL_POLL_MS, gen);
        return;
      }
      if (epoch !== null && st.epoch !== null && st.epoch !== epoch) {
        // The server started a new stream (privacy reset / restart): start over.
        emit({ type: "line", obj: { t: "reset", epoch: st.epoch } });
        nextSeq = null;
        resumeAfterSeq = 0;
      }
      if (st.epoch !== null) epoch = st.epoch;
      const info: LiveInfo = {
        serverId: st.serverId, epoch: st.epoch, delaySec: st.delaySec,
        live: st.live, ended: st.ended,
      };
      emit({ type: "info", info });

      const haveData = st.latestSeq >= 1 && st.oldestSeq <= st.latestSeq;
      if (haveData) {
        if (nextSeq === null) {
          nextSeq = Math.max(st.oldestSeq, st.latestSeq - START_BACK_SEGMENTS + 1, 1,
                             resumeAfterSeq + 1);
        } else if (nextSeq < st.oldestSeq) {
          // Fell behind the server's buffer: the missed data is gone.
          emit({ type: "line", obj: { t: "gap" } });
          nextSeq = st.oldestSeq;
        } else if (nextSeq > st.latestSeq + 1) {
          // The counter went backwards without an epoch change: treat as a gap.
          emit({ type: "line", obj: { t: "gap" } });
          nextSeq = Math.max(st.oldestSeq, st.latestSeq - START_BACK_SEGMENTS + 1, 1);
        }
      }
      setConn(st.live || haveData ? "live" : "closed");

      let fetched = 0;
      while (haveData && nextSeq !== null && nextSeq <= st.latestSeq
             && fetched < MAX_SEGMENTS_PER_CYCLE) {
        const seq = nextSeq;
        const r = await request(segmentUrl(opts.server, seq));
        if (gen !== run) return;
        if (r.status === 429 || r.status === 503) {
          const body = await readLimitBody(r);
          if (gen !== run) return;
          if (body.error === "full" || body.error === "ip_limit") {
            setConn(body.error === "full" ? "full" : "ip_limit");
            nextMs = limitWaitMs(r, body, FULL_POLL_MS);
            limited = true;
            break;
          }
          if (r.status === 503) throw new Error("segment_503");
          nextMs = limitWaitMs(r, body, pollMs);
          limited = true;
          break;
        }
        if (r.status === 404 || r.status === 410) {
          // Segment aged out between `durum` and now.
          emit({ type: "line", obj: { t: "gap" } });
          nextSeq = null;
          nextMs = 250;
          break;
        }
        if (!r.ok) throw new Error(`segment_${r.status}`);
        const text = await bodyToText(r);
        if (gen !== run) return;
        const { lines } = parseNdjson(text);
        segments++;
        lastSeq = seq;
        nextSeq = seq + 1;
        fetched++;
        for (const obj of lines) {
          emit({ type: "line", obj });
          if (obj && typeof obj === "object"
              && (obj as { t?: unknown }).t === "end") {
            // The match is over: stop here. "Sonraki maçı izle" calls
            // restart(); until then nothing more is fetched.
            ended = true;
            resumeAfterSeq = seq;
            running = false;
            return;
          }
        }
      }
      if (limited) {
        // keep the server-given delay as is
      } else if (nextSeq !== null && nextSeq <= st.latestSeq && nextMs === pollMs) {
        nextMs = 250;                        // more to catch up on
      } else if (nextMs === pollMs) {
        nextMs = Math.round(pollMs * (0.9 + random() * 0.2));
      }
      schedule(nextMs, gen);
    } catch (err) {
      if (gen !== run) return;
      if ((err as { name?: string } | null)?.name === "AbortError") return;
      failures++;
      failConn();
      schedule(backoffMs(), gen);
    }
  }

  function begin() {
    running = true;
    run++;
    abort = new AbortController();
    setConn(hadSuccess ? "reconnecting" : "connecting");
    void cycle(run);
  }

  function stop() {
    running = false;
    run++;
    if (timer !== null) { timers.clearTimeout(timer); timer = null; }
    abort?.abort();
    abort = null;
  }

  return {
    start() { if (!running && !ended) begin(); },
    stop,
    restart() {
      stop();
      nextSeq = null; lastSeq = null; ended = false;   // epoch kept: a new one zeroes resumeAfterSeq
      failures = 0; conn = null; hadSuccess = false; viewerToken = null;
      begin();
    },
    stats() { return { nextSeq, lastSeq, epoch, segments, ended }; },
  };
}
