// Zero Zone viewer root: replay (`?mode=replay&id=`) and live (`?mode=live&server=`,
// `?mode=admin-live&server=&bilet=`). No recording picker, no stats pages, no
// analytics. Everything it loads is same-origin.
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { MapCanvas } from "../canvas/MapCanvas";
import type { Hit } from "../canvas/hitTest";
import { TopBar } from "../ui/TopBar";
import { TeamBar } from "../ui/TeamBar";
import { MatchOverlay } from "../ui/MatchOverlay";
import { BufferOverlay } from "../ui/BufferOverlay";
import { PlayerSearch } from "../ui/PlayerSearch";
import { Tooltip } from "../ui/Tooltip";
import { PlayerPanel, playerKey } from "../ui/PlayerPanel";
import { InfoPanel } from "../ui/InfoPanel";
import { KillFeed } from "../ui/KillFeed";
import { Scoreboard } from "../ui/Scoreboard";
import { TicketTimeline } from "../ui/TicketTimeline";
import { TimelineBar } from "../ui/TimelineBar";
import { useReplayLoader } from "../api/replay";
import { useReplayPlayback } from "../api/playback";
import { useKillFeed } from "../killfeed/useKillFeed";
import { useReplayKillFeed } from "../killfeed/useReplayKillFeed";
import { useViewerStore } from "../state/viewerStore";
import { SourceLink } from "./SourceLink";
import { MapNotice } from "./MapNotice";
import { ZzMessage } from "./ZzMessage";
import { useZzGate } from "./useZzGate";
import {
  errorText, parseZzLocation, playerSelectMessage, shouldNotifyParent, TR,
  type ZzLiveLocation,
} from "./mode";
import { useLiveFeed } from "./live/useLiveFeed";
import { LiveBadge, LiveStatusOverlay } from "./live/LiveStatus";
import { LiveEndOverlay } from "./live/LiveEndOverlay";
import { LiveAlertStrip } from "./live/LiveAlertStrip";
import { parseAlert, pushAlert, type AlertItem } from "./live/alertState";

// Same lazy split as upstream: the loadout catalog loads on first vehicle click.
const VehiclePanel = lazy(() =>
  import("../ui/VehiclePanel").then((m) => ({ default: m.VehiclePanel })));

/** Shared viewer layout. `overlays` / `topExtra` / `bottom` differ between the
 *  replay and the live viewer; the map, panels and hotkeys are the same. */
function ViewerLayout(props: {
  live: boolean;
  topExtra?: ReactNode;
  overlays: ReactNode;
  bottom?: ReactNode;
  /** Live: the "Maç bitti" layer replaces the shared match-phase card. */
  hideMatchOverlay?: boolean;
}) {
  const [tooltip, setTooltip] = useState<{ hit: Hit | null; x: number; y: number }>(
    { hit: null, x: 0, y: 0 });
  const selectedVehicleId    = useViewerStore((s) => s.selectedVehicleId);
  const setSelectedVehicleId = useViewerStore((s) => s.setSelectedVehicleId);
  const setSelectedPlayerKey = useViewerStore((s) => s.setSelectedPlayerKey);
  const setSelectedInfo      = useViewerStore((s) => s.setSelectedInfo);
  const toggleScoreboard     = useViewerStore((s) => s.toggleScoreboard);
  const setScoreboardVisible = useViewerStore((s) => s.setScoreboardVisible);
  const toggleTimeline       = useViewerStore((s) => s.toggleTimeline);
  const setTimelineVisible   = useViewerStore((s) => s.setTimelineVisible);

  const onHover = useCallback((hit: Hit | null, x: number, y: number) => {
    setTooltip({ hit, x, y });
  }, []);
  const onLeave = useCallback(() => {
    setTooltip({ hit: null, x: 0, y: 0 });
  }, []);
  const onClick = useCallback((hit: Hit | null) => {
    const h = hit?.hit;
    if (!h) {
      setSelectedVehicleId(null);
      setSelectedPlayerKey(null);
      setSelectedInfo(null);
      return;
    }
    if (h.type === "vehicle") {
      setSelectedVehicleId(h.e.id);
      setSelectedPlayerKey(null);
    } else if (h.type === "player") {
      setSelectedPlayerKey(playerKey(h.e));
      setSelectedVehicleId(null);
    } else {
      setSelectedInfo({ kind: h.type, id: h.e.id });
    }
  }, [setSelectedVehicleId, setSelectedPlayerKey, setSelectedInfo]);

  // Same hotkeys as upstream: Esc closes panels, Tab scoreboard, G tickets,
  // Space play/pause.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName ?? "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "Escape") {
        setSelectedVehicleId(null);
        setSelectedPlayerKey(null);
        setSelectedInfo(null);
        setScoreboardVisible(false);
        setTimelineVisible(false);
      } else if (e.key === "Tab") {
        e.preventDefault();
        toggleScoreboard();
      } else if (e.key === "g" || e.key === "G") {
        if (useViewerStore.getState().replay.frames.length) toggleTimeline();
      } else if (e.key === " " || e.code === "Space") {
        const s = useViewerStore.getState();
        if (s.replay.frames.length) {
          e.preventDefault();
          s.setReplay((r) => ({
            ...r,
            playing: !r.playing,
            baseWallMs: 0, baseSnapMs: 0,
            currentIdx: r.currentIdx >= r.frames.length - 1 ? 0 : r.currentIdx,
          }));
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setSelectedVehicleId, setSelectedPlayerKey, setSelectedInfo,
      setScoreboardVisible, toggleScoreboard, toggleTimeline, setTimelineVisible]);

  return (
    <div id="root-grid" className={props.live ? "zz-live-root" : undefined}>
      <div id="stage">
        <MapCanvas onHover={onHover} onLeave={onLeave} onClick={onClick} />
        {/* Desktop: `display: contents`, so the three keep their own absolute
            positions. Narrow screens: one stacked column (see zz.css). */}
        <div className="zz-topstack">
          {props.topExtra}
          <TopBar />
          <TeamBar />
          <PlayerSearch />
          <MapNotice />
        </div>
        {props.overlays}
        {!props.hideMatchOverlay && <MatchOverlay />}
        <Tooltip {...tooltip} />
        {selectedVehicleId &&
          <Suspense fallback={null}><VehiclePanel /></Suspense>}
        <PlayerPanel />
        <InfoPanel />
        <KillFeed />
        {props.bottom}
      </div>
      <Scoreboard />
      <TicketTimeline />
      <SourceLink />
    </div>
  );
}

function Viewer() {
  useReplayLoader();
  useReplayPlayback();
  useKillFeed();
  useReplayKillFeed();
  return (
    <ViewerLayout live={false} overlays={<BufferOverlay />} bottom={<TimelineBar />} />
  );
}

/** Live viewer: no timeline / seek / speed controls (they only render in replay
 *  mode), a "CANLI" badge, Turkish connection layers and the end-of-match layer. */
function LiveViewer({ loc }: { loc: ZzLiveLocation }) {
  const admin = loc.kind === "admin-live";
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const alertId = useRef(0);
  const onAlert = useCallback((payload: unknown) => {
    const item = parseAlert(payload, Date.now(), ++alertId.current);
    if (item) setAlerts((l) => pushAlert(l, item));
  }, []);
  const feed = useLiveFeed(loc, admin ? onAlert : undefined);

  // Admin-live only: tell the embedding page which player is selected.
  const selKey = useViewerStore((s) => s.selectedPlayerKey);
  const selPlayer = useViewerStore((s) =>
    selKey ? (s.curSnap?.players ?? []).find((p) => playerKey(p) === selKey) ?? null : null);
  const selAlias = selPlayer?.eosId ?? null;
  const selName = selPlayer?.name ?? null;
  useEffect(() => {
    if (!shouldNotifyParent(loc.kind, window)) return;
    const msg = selKey == null ? playerSelectMessage(null)
      : playerSelectMessage({ eosId: selAlias, name: selName });
    if (msg) window.parent.postMessage(msg, window.location.origin);
  }, [loc.kind, selKey, selAlias, selName]);

  const ended = feed.end.phase !== "hidden";
  return (
    <ViewerLayout
      live
      hideMatchOverlay={ended}
      topExtra={<LiveBadge conn={feed.conn} delaySec={feed.delaySec} admin={feed.admin} />}
      overlays={
        <>
          {admin && <LiveAlertStrip items={alerts} onChange={setAlerts} />}
          <LiveStatusOverlay conn={feed.conn} hidden={ended} onRetry={feed.onRetry} />
          <LiveEndOverlay state={feed.end} admin={feed.admin} onNext={feed.onNext}
                          onGoto={feed.onGoto} onClose={feed.onClose} />
        </>
      }
    />
  );
}

function Gated({ id }: { id: string }) {
  const { gate, retry } = useZzGate(id);
  if (gate.state === "checking") {
    return <ZzMessage title={TR.loading} sub={TR.loadingSub} spinner />;
  }
  if (gate.state === "error") {
    const t = errorText(gate.kind);
    return (
      <ZzMessage title={t.title} sub={t.sub}>
        {gate.kind === "failed" && (
          <div className="buf-actions">
            <button className="buf-ok" onClick={retry}>{TR.retry}</button>
          </div>
        )}
      </ZzMessage>
    );
  }
  return <Viewer />;
}

export default function ZzApp() {
  // Location is read once; the viewer never rewrites it.
  const [loc] = useState(() => parseZzLocation(window.location.search));
  const setMode = useViewerStore((s) => s.setMode);
  const setReplay = useViewerStore((s) => s.setReplay);
  const [seeded, setSeeded] = useState(false);

  // Seed the store with the mode (and the match id) before the viewer mounts, so
  // the first viewer render already is in the right mode.
  useEffect(() => {
    if (!loc.ok) return;
    if ("kind" in loc) {
      setMode("live");
    } else {
      setReplay((r) => ({ ...r, id: loc.id, frames: [], currentIdx: 0,
                          playing: false, speed: 1, baseWallMs: 0, baseSnapMs: 0 }));
      setMode("replay");
    }
    setSeeded(true);
  }, [loc, setMode, setReplay]);

  if (!loc.ok) {
    return loc.reason === "bad-mode"
      ? <ZzMessage title={TR.invalidAddress} sub={TR.invalidAddressSub} />
      : <ZzMessage title={TR.invalidId} sub={TR.invalidIdSub} />;
  }
  if (!seeded) return <ZzMessage title={TR.loading} spinner />;
  if ("kind" in loc) return <LiveViewer loc={loc} />;
  return <Gated id={loc.id} />;
}
