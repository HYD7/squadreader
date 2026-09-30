// Live frames: from raw NDJSON / SSE lines to the viewer store.
//
//  LiveDecoder   line object -> item. Position frames are folded into full
//                snapshots with replayReconstruct.reconstructFromPosition (the
//                same code the replay loader uses); markers become items.
//  PlayoutQueue  paces a burst of frames (a whole 5 s segment arrives at once)
//                so they reach the store at their own recorded rhythm.
//  createLiveSink glues both to the store through viewerStore.ingestLive().
//
// Pure module: no DOM, no React, the clock and the store are injected, so it
// runs under the node test runner.
import type { PositionFrame, Snapshot } from "../../state/types";
import { reconstructFromPosition } from "../../state/replayReconstruct";

export type LiveItem =
  | { kind: "frame"; frame: Snapshot; mediaMs: number }
  | { kind: "gap" }
  | { kind: "reset"; epoch: number | null }
  | { kind: "end"; matchToken: string | null };

const TOKEN_RE = /^[A-Za-z0-9_.-]{1,64}$/;

export function isValidMatchToken(v: unknown): v is string {
  return typeof v === "string" && TOKEN_RE.test(v);
}

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

/** Turns parsed lines into items, in order. Keeps the last full frame so that
 *  position frames can be reconstructed. */
export class LiveDecoder {
  private lastFull: Snapshot | null = null;
  private lastMediaMs = -Infinity;
  /** Lines that were not usable (bad shape, orphan pos, out of order). */
  dropped = 0;

  reset(): void {
    this.lastFull = null;
    this.lastMediaMs = -Infinity;
  }

  push(obj: unknown): LiveItem | null {
    if (!isObject(obj)) { this.dropped++; return null; }
    const t = obj.t;
    if (t === "seg") return null;                    // informational marker
    if (t === "gap") {
      this.reset();
      return { kind: "gap" };
    }
    if (t === "reset") {
      this.reset();
      return { kind: "reset", epoch: typeof obj.epoch === "number" ? obj.epoch : null };
    }
    if (t === "end") {
      return { kind: "end",
               matchToken: isValidMatchToken(obj.matchToken) ? obj.matchToken : null };
    }
    let snap: Snapshot;
    if (t === "pos") {
      if (!this.lastFull || typeof obj.timestamp !== "string"
          || !Array.isArray(obj.players) || !Array.isArray(obj.vehicles)) {
        this.dropped++;
        return null;
      }
      snap = reconstructFromPosition(this.lastFull, obj as unknown as PositionFrame);
    } else if (t === undefined && typeof obj.timestamp === "string"
               && Array.isArray(obj.players)) {
      snap = obj as unknown as Snapshot;
    } else {
      this.dropped++;                                 // unknown marker / shape
      return null;
    }
    const mediaMs = Date.parse(snap.timestamp);
    if (!Number.isFinite(mediaMs) || mediaMs < this.lastMediaMs) {
      this.dropped++;
      return null;
    }
    this.lastMediaMs = mediaMs;
    if (t !== "pos") this.lastFull = snap;
    return { kind: "frame", frame: snap, mediaMs };
  }
}

// ---------------------------------------------------------------------------

export const LATE_RESYNC_MS = 2_000;

interface QueueEntry { item: LiveItem; mediaMs: number }

/** Releases frames at the pace of their own timestamps. The first frame plays
 *  at once and fixes an anchor (wall time <-> media time). A frame that is
 *  later than LATE_RESYNC_MS behind schedule (the next segment came late)
 *  re-anchors, so playback resumes at natural speed instead of bursting. */
export class PlayoutQueue {
  private q: QueueEntry[] = [];
  private head = 0;
  private anchorWall: number | null = null;
  private anchorMedia = 0;
  private lastMedia = 0;

  constructor(private readonly lateMs: number = LATE_RESYNC_MS) {}

  get size(): number { return this.q.length - this.head; }

  push(item: LiveItem): void {
    const mediaMs = item.kind === "frame" ? item.mediaMs : this.lastMedia;
    if (item.kind === "frame") this.lastMedia = mediaMs;
    this.q.push({ item, mediaMs });
  }

  clear(): void {
    this.q = []; this.head = 0; this.anchorWall = null;
  }

  drain(nowMs: number): LiveItem[] {
    const out: LiveItem[] = [];
    while (this.head < this.q.length) {
      const e = this.q[this.head]!;
      if (e.item.kind === "frame") {
        if (this.anchorWall === null) {
          this.anchorWall = nowMs;
          this.anchorMedia = e.mediaMs;
        }
        const due = this.anchorWall + (e.mediaMs - this.anchorMedia);
        if (due > nowMs) break;
        if (nowMs - due > this.lateMs) {
          this.anchorWall = nowMs;
          this.anchorMedia = e.mediaMs;
        }
      } else if (e.item.kind === "gap" || e.item.kind === "reset") {
        this.anchorWall = null;          // media time may jump after these
      }
      out.push(e.item);
      this.head++;
    }
    if (this.head > 256 && this.head * 2 > this.q.length) {
      this.q = this.q.slice(this.head);
      this.head = 0;
    }
    return out;
  }

  /** Milliseconds until the next item is due (0 = now), null when empty. */
  nextDueMs(nowMs: number): number | null {
    if (this.head >= this.q.length) return null;
    const e = this.q[this.head]!;
    if (e.item.kind !== "frame") return 0;
    if (this.anchorWall === null) return 0;
    return Math.max(0, this.anchorWall + (e.mediaMs - this.anchorMedia) - nowMs);
  }
}

// ---------------------------------------------------------------------------

/** The slice of the viewer store the live path needs. */
export interface LiveStoreApi {
  ingest(snap: Snapshot): void;
  /** Drop every trace of the previous stream (players, aliases, kill feed). */
  reset(): void;
  /** Current canvas delay; the "match ended" layer waits this long. */
  renderDelayMs(): number;
}

export interface LiveSinkHandlers {
  /** A match ended (drawn frames have caught up). `matchToken` may be null. */
  onEnd(matchToken: string | null): void;
  /** Admin `match` event (state: started | ended | ...). */
  onMatch?(matchId: string | null, state: string): void;
  /** A gap / reset was applied. */
  onDiscontinuity?(kind: "gap" | "reset"): void;
}

export interface LiveSinkOptions {
  store: LiveStoreApi;
  handlers: LiveSinkHandlers;
  /** true: frames are paced by their timestamps (public segments); false:
   *  they go to the store as they arrive (admin SSE). */
  paced: boolean;
  now: () => number;
}

export interface LiveSink {
  push(obj: unknown): void;
  match(matchId: string | null, state: string): void;
  pump(nowMs?: number): void;
  nextDueMs(nowMs?: number): number | null;
  /** Forget everything (next match / restart). */
  hardReset(): void;
  stats(): { frames: number; dropped: number; queued: number };
}

interface Deferred { dueMs: number; run: () => void }

export function createLiveSink(opts: LiveSinkOptions): LiveSink {
  const { store, handlers, paced } = opts;
  const decoder = new LiveDecoder();
  const queue = new PlayoutQueue();
  let deferred: Deferred[] = [];
  let frames = 0;
  let lastEndToken: string | null | undefined;

  function defer(nowMs: number, run: () => void) {
    deferred.push({ dueMs: nowMs + Math.max(0, store.renderDelayMs()), run });
  }

  function apply(item: LiveItem, nowMs: number) {
    switch (item.kind) {
      case "frame":
        frames++;
        store.ingest(item.frame);
        break;
      case "gap":
      case "reset":
        store.reset();
        handlers.onDiscontinuity?.(item.kind);
        break;
      case "end": {
        // The same end marker can come twice (a restart re-reads it): once.
        if (item.matchToken !== null && item.matchToken === lastEndToken) break;
        lastEndToken = item.matchToken;
        const token = item.matchToken;
        defer(nowMs, () => handlers.onEnd(token));
        break;
      }
    }
  }

  function push(obj: unknown) {
    const item = decoder.push(obj);
    if (!item) return;
    if (paced) queue.push(item);
    else apply(item, opts.now());
  }

  function runDeferred(nowMs: number) {
    if (!deferred.length) return;
    const due = deferred.filter((d) => d.dueMs <= nowMs);
    if (!due.length) return;
    deferred = deferred.filter((d) => d.dueMs > nowMs);
    for (const d of due) d.run();
  }

  return {
    push,
    match(matchId, state) {
      // Admin feed. "ended" is shown once the drawn frames have caught up; a
      // new match ("started") wipes the previous match's players and kill feed.
      if (state === "ended") {
        defer(opts.now(), () => handlers.onMatch?.(matchId, state));
        return;
      }
      if (state === "started") {
        decoder.reset();
        store.reset();
      }
      handlers.onMatch?.(matchId, state);
    },
    pump(nowMs = opts.now()) {
      for (const item of queue.drain(nowMs)) apply(item, nowMs);
      runDeferred(nowMs);
    },
    nextDueMs(nowMs = opts.now()) {
      let next = queue.nextDueMs(nowMs);
      for (const d of deferred) {
        const wait = Math.max(0, d.dueMs - nowMs);
        next = next === null ? wait : Math.min(next, wait);
      }
      return next;
    },
    hardReset() {
      decoder.reset();
      queue.clear();
      deferred = [];
      // lastEndToken is kept on purpose: the marker of the match that just
      // ended must not open the layer again, a new match has a new token.
      store.reset();
    },
    stats() { return { frames, dropped: decoder.dropped, queued: queue.size }; },
  };
}
