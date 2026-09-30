// Live viewer chrome: the "CANLI" badge with the connection state, and the
// Turkish loading / waiting / limit-reached layers (the shared BufferOverlay is
// English and replay-oriented, so the live view has its own).
import { useEffect, useRef, useState } from "react";
import { useViewerStore } from "../../state/viewerStore";
import type { ConnState } from "./liveUrls";
import { LIVE_TR, badgeText } from "./liveText";
import { createPhaseTracker, type Phase, type PhaseTracker } from "./livePhase";

export function LiveBadge(props: {
  conn: ConnState; delaySec: number | null; admin: boolean;
}) {
  const mapName = useViewerStore((s) => s.curSnap?.gameState?.mapName ?? null);
  const cls = props.conn === "live" ? "live"
            : props.conn === "reconnecting" || props.conn === "connecting" ? "warn"
            : "bad";
  return (
    <div className="zz-live-badge" role="status" data-conn={props.conn}>
      <span className={"zz-live-dot " + cls} aria-hidden="true" />
      <b>{badgeText(props.delaySec, props.admin)}</b>
      <span className={"pill " + cls}>{LIVE_TR.connState[props.conn]}</span>
      {mapName && <span className="zz-live-map">{mapName}</span>}
    </div>
  );
}

function readPhase(tracker: PhaseTracker): { phase: Phase; pct: number } {
  const s = useViewerStore.getState();
  const buf = s.snapBuffer;
  return tracker.evaluate({
    count: buf.length,
    first: buf.length ? buf[0]!.t : 0,
    last: buf.length ? buf[buf.length - 1]!.t : 0,
    now: performance.now(),
    renderDelayMs: s.renderDelayMs,
  });
}

export function LiveStatusOverlay(props: {
  conn: ConnState; hidden: boolean; onRetry: () => void;
}) {
  const trackerRef = useRef<PhaseTracker | null>(null);
  if (trackerRef.current === null) trackerRef.current = createPhaseTracker();
  const [st, setSt] = useState(() => readPhase(trackerRef.current!));
  useEffect(() => {
    const id = window.setInterval(() => {
      const next = readPhase(trackerRef.current!);
      setSt((prev) => prev.phase === next.phase
        && Math.abs(prev.pct - next.pct) < 0.01 ? prev : next);
    }, 200);
    return () => window.clearInterval(id);
  }, []);

  if (props.hidden) return null;
  const { conn } = props;
  const noData = st.phase === "connecting";

  if (conn === "full") {
    return (
      <div className="buf-overlay" role="status">
        <div className="buf-card">
          <div className="buf-title">{LIVE_TR.fullTitle}</div>
          <div className="buf-sub">{LIVE_TR.fullSub}</div>
          <div className="buf-actions">
            <button className="buf-ok" onClick={props.onRetry}>{LIVE_TR.retryNow}</button>
          </div>
        </div>
      </div>
    );
  }
  if (conn === "ip_limit") {
    return (
      <div className="buf-overlay" role="status">
        <div className="buf-card">
          <div className="buf-title">{LIVE_TR.ipLimitTitle}</div>
          <div className="buf-sub">{LIVE_TR.ipLimitSub}</div>
          <div className="buf-actions">
            <button className="buf-ok" onClick={props.onRetry}>{LIVE_TR.retryNow}</button>
          </div>
        </div>
      </div>
    );
  }
  if (conn === "closed" && noData) {
    return (
      <div className="buf-overlay" role="status">
        <div className="buf-card">
          <div className="buf-title">{LIVE_TR.closedTitle}</div>
          <div className="buf-sub">{LIVE_TR.closedSub}</div>
        </div>
      </div>
    );
  }
  if (noData) {
    return (
      <div className="buf-overlay" role="status">
        <div className="buf-card">
          <div className="buf-ring" />
          <div className="buf-title">{LIVE_TR.connectingTitle}</div>
          <div className="buf-sub">{LIVE_TR.connectingSub}</div>
          <div className="buf-dots"><span /><span /><span /></div>
        </div>
      </div>
    );
  }
  if (st.phase === "warmup") {
    return (
      <div className="buf-overlay" role="status">
        <div className="buf-card">
          <div className="buf-ring" />
          <div className="buf-title">{LIVE_TR.warmupTitle}</div>
          <div className="buf-sub">{LIVE_TR.warmupSub}</div>
          <div className="buf-bar"><div className="buf-bar-fill"
               style={{ width: `${Math.round(st.pct * 100)}%` }} /></div>
        </div>
      </div>
    );
  }
  if (conn === "reconnecting" || conn === "closed" || st.phase === "stalled") {
    return (
      <div className="buf-banner" role="status">
        <span className="buf-spin" />
        <span className="buf-banner-txt">
          {conn === "closed" ? LIVE_TR.closedTitle
            : conn === "reconnecting" ? LIVE_TR.reconnectingBanner
            : LIVE_TR.stalledBanner}
        </span>
      </div>
    );
  }
  return null;
}
