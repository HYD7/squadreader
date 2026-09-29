// Zero Zone replay viewer root. Replay-only: no live mode, no recording picker,
// no stats pages, no analytics. Everything it loads is same-origin and relative.
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
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
import { errorText, parseZzLocation, TR } from "./mode";

// Same lazy split as upstream: the loadout catalog loads on first vehicle click.
const VehiclePanel = lazy(() =>
  import("../ui/VehiclePanel").then((m) => ({ default: m.VehiclePanel })));

function Viewer() {
  useReplayLoader();
  useReplayPlayback();
  useKillFeed();
  useReplayKillFeed();

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
    <div id="root-grid">
      <div id="stage">
        <MapCanvas onHover={onHover} onLeave={onLeave} onClick={onClick} />
        {/* Desktop: `display: contents`, so the three keep their own absolute
            positions. Narrow screens: one stacked column (see zz.css). */}
        <div className="zz-topstack">
          <TopBar />
          <TeamBar />
          <PlayerSearch />
          <MapNotice />
        </div>
        <BufferOverlay />
        <MatchOverlay />
        <Tooltip {...tooltip} />
        {selectedVehicleId &&
          <Suspense fallback={null}><VehiclePanel /></Suspense>}
        <PlayerPanel />
        <InfoPanel />
        <KillFeed />
        <TimelineBar />
      </div>
      <Scoreboard />
      <TicketTimeline />
      <SourceLink />
    </div>
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

  // Seed the store with the match id before the viewer mounts, so the first
  // viewer render already is in replay mode.
  useEffect(() => {
    if (!loc.ok) return;
    setReplay((r) => ({ ...r, id: loc.id, frames: [], currentIdx: 0,
                        playing: false, speed: 1, baseWallMs: 0, baseSnapMs: 0 }));
    setMode("replay");
    setSeeded(true);
  }, [loc, setMode, setReplay]);

  if (!loc.ok) {
    return <ZzMessage title={TR.invalidId} sub={TR.invalidIdSub} />;
  }
  if (!seeded) return <ZzMessage title={TR.loading} spinner />;
  return <Gated id={loc.id} />;
}
