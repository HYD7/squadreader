// Live feed hook: builds the pipeline of liveFeed.ts for the URL (public
// segment polling or the administrator SSE stream), feeds the viewer store, and
// exposes the connection / end-of-match state to the UI.
//
// Frames reach the store through viewerStore.ingestLive() (via liveFrames), so
// MapCanvas' live branch, the team bar and the scoreboard work as they do in
// the upstream live viewer. The kill feed uses the shared useKillFeed diff.
import { useCallback, useEffect, useRef, useState } from "react";
import { useViewerStore } from "../../state/viewerStore";
import { useKillFeed } from "../../killfeed/useKillFeed";
import type { ZzLiveLocation } from "../mode";
import type { EndState } from "./liveEnd";
import type { ConnState } from "./liveUrls";
import { navigateTop } from "./liveNavigate";
import { createLiveFeed, type LiveFeed, type LiveFeedStore } from "./liveFeed";

export interface LiveFeedView {
  conn: ConnState;
  delaySec: number | null;
  admin: boolean;
  end: EndState;
  /** "Sonraki maçı izle" (public): restart from the newest data. */
  onNext(): void;
  onGoto(): void;
  /** Close the information layer (admin). */
  onClose(): void;
  /** Reconnect now ("Şimdi dene"). */
  onRetry(): void;
}

const store: LiveFeedStore = {
  ingest: (snap) => useViewerStore.getState().ingestLive(snap),
  reset: () => useViewerStore.getState().resetLive(),
  renderDelayMs: () => useViewerStore.getState().renderDelayMs,
  setRenderDelayMs: (ms) => useViewerStore.getState().setRenderDelayMs(ms),
  setStatus: (s) => useViewerStore.getState().setStatus(s),
};

export function useLiveFeed(loc: ZzLiveLocation): LiveFeedView {
  useKillFeed();

  const admin = loc.kind === "admin-live";
  const ticket = loc.kind === "admin-live" ? loc.ticket : null;

  const [conn, setConn] = useState<ConnState>("connecting");
  const [delaySec, setDelaySec] = useState<number | null>(null);
  const [end, setEnd] = useState<EndState>({ phase: "hidden" });
  const feedRef = useRef<LiveFeed | null>(null);

  useEffect(() => {
    const target: ZzLiveLocation = loc.kind === "admin-live"
      ? { ok: true, kind: "admin-live", server: loc.server, ticket: ticket ?? "" }
      : { ok: true, kind: "live", server: loc.server };
    const feed = createLiveFeed(target, {
      store,
      now: () => performance.now(),
      navigate: (url) => navigateTop(url, window),
    }, {
      onConn: setConn,
      onDelay: setDelaySec,
      onEnd: setEnd,
    });
    feedRef.current = feed;
    feed.start();
    return () => {
      feed.stop();
      feedRef.current = null;
    };
    // The ticket only matters at connect time.
  }, [loc.kind, loc.server, ticket]);

  const onNext = useCallback(() => feedRef.current?.next(), []);
  const onGoto = useCallback(() => feedRef.current?.goto(), []);
  const onClose = useCallback(() => feedRef.current?.close(), []);
  const onRetry = useCallback(() => feedRef.current?.retry(), []);

  return { conn, delaySec, admin, end, onNext, onGoto, onClose, onRetry };
}
