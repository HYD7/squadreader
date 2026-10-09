// The live feed pipeline without React: source (public segments or admin SSE)
// -> sink (decode, pace, store) -> end-of-match layer. useLiveFeed.ts is a thin
// hook around this; keeping the wiring here lets the whole pipeline run under
// the node test runner with fake timers, fetch and EventSource.
import type { ZzLiveLocation } from "../mode";
import { createLiveSink, type LiveSink, type LiveStoreApi } from "./liveFrames";
import { createEndMachine, type EndMachine, type EndState } from "./liveEnd";
import { createSegmentSource, type SegmentSource } from "./segmentSource";
import {
  ADMIN_RENDER_DELAY_MS, createSseSource, type EventSourceFactory,
} from "./sseSource";
import type { ConnState, LiveSource, SourceEvent, Timers } from "./liveUrls";
import { realTimers } from "./liveUrls";
import { patchLiveDebug } from "./liveDebug";

/** Canvas delay of the public feed: the viewer default (see viewerStore). */
export const PUBLIC_RENDER_DELAY_MS = 6_000;

export interface LiveFeedStore extends LiveStoreApi {
  setRenderDelayMs(ms: number): void;
  setStatus(s: "connecting" | "live" | "reconnecting" | "idle"): void;
}

export interface LiveFeedDeps {
  store: LiveFeedStore;
  navigate: (url: string) => void;
  now: () => number;
  timers?: Timers;
  fetchImpl?: typeof fetch;
  eventSourceFactory?: EventSourceFactory;
  /** Admin SSE `alert` event payload. */
  onAlert?: (payload: unknown) => void;
}

export interface LiveFeedCallbacks {
  onConn(state: ConnState): void;
  onDelay(delaySec: number | null): void;
  onEnd(state: EndState): void;
}

export interface LiveFeed {
  start(): void;
  stop(): void;
  /** "Sonraki maçı izle". */
  next(): void;
  /** "Maç sayfasına git". */
  goto(): void;
  /** Close the end layer (admin). */
  close(): void;
  /** Reconnect now ("Şimdi dene"). */
  retry(): void;
}

function statusFor(state: ConnState): "connecting" | "live" | "reconnecting" | "idle" {
  return state === "live" ? "live"
       : state === "reconnecting" ? "reconnecting"
       : state === "connecting" ? "connecting" : "idle";
}

export function createLiveFeed(loc: ZzLiveLocation, deps: LiveFeedDeps,
                               cb: LiveFeedCallbacks): LiveFeed {
  const admin = loc.kind === "admin-live";
  const timers = deps.timers ?? realTimers;
  const { store } = deps;
  let alive = false;
  let source: LiveSource;
  let sink: LiveSink;

  const endMachine: EndMachine = createEndMachine({
    server: loc.server,
    redirect: !admin,
    timers,
    fetchImpl: deps.fetchImpl,
    onChange: (s) => { if (alive) { cb.onEnd(s); patchLiveDebug({ end: s.phase }); } },
    onNext: () => { sink.hardReset(); source.restart(); },
    navigate: deps.navigate,
  });

  sink = createLiveSink({
    store,
    paced: !admin,
    now: deps.now,
    handlers: {
      onEnd: (token) => endMachine.show(token),
      onMatch: (_id, state) => {
        if (!admin) return;
        if (state === "ended") endMachine.show(null);
        else if (state === "started") endMachine.hide();
      },
    },
  });

  const onEvent = (ev: SourceEvent) => {
    if (!alive) return;
    switch (ev.type) {
      case "conn":
        cb.onConn(ev.state);
        store.setStatus(statusFor(ev.state));
        patchLiveDebug({ conn: ev.state });
        break;
      case "info":
        cb.onDelay(ev.info.delaySec);
        patchLiveDebug({ delaySec: ev.info.delaySec, epoch: ev.info.epoch });
        break;
      case "line":
        sink.push(ev.obj);
        if (!admin) sink.pump();
        break;
      case "match":
        sink.match(ev.matchId, ev.state);
        break;
    }
  };

  const segSource: SegmentSource | null = admin ? null : createSegmentSource({
    server: loc.server, onEvent, timers, fetchImpl: deps.fetchImpl,
  });
  source = admin
    ? createSseSource({
      server: loc.server, ticket: loc.kind === "admin-live" ? loc.ticket : "",
      onEvent, timers, factory: deps.eventSourceFactory, onAlert: deps.onAlert,
    })
    : segSource!;

  // Pacing driver: releases queued frames when they are due. The wake-up is the
  // exact due time, capped so the debug numbers stay fresh.
  let pumpTimer: unknown = null;
  const step = () => {
    pumpTimer = null;
    if (!alive) return;
    sink.pump();
    const st = sink.stats();
    patchLiveDebug({
      frames: st.frames, dropped: st.dropped, queued: st.queued,
      ...(segSource ? { segments: segSource.stats().segments,
                        lastSeq: segSource.stats().lastSeq } : {}),
    });
    const due = sink.nextDueMs();
    pumpTimer = timers.setTimeout(step, due === null ? 250 : Math.max(0, Math.min(due, 250)));
  };

  return {
    start() {
      if (alive) return;
      alive = true;
      store.setRenderDelayMs(admin ? ADMIN_RENDER_DELAY_MS : PUBLIC_RENDER_DELAY_MS);
      store.reset();
      patchLiveDebug({
        kind: loc.kind, server: loc.server, conn: "connecting", delaySec: null,
        end: "hidden", frames: 0, dropped: 0, queued: 0, segments: 0,
        lastSeq: null, epoch: null,
      });
      step();
      source.start();
    },
    stop() {
      alive = false;
      if (pumpTimer !== null) { timers.clearTimeout(pumpTimer); pumpTimer = null; }
      source.stop();
      endMachine.destroy();
    },
    next() { endMachine.next(); },
    goto() { endMachine.goto(); },
    close() { endMachine.hide(); },
    retry() { source.restart(); },
  };
}
