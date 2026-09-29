// Bottom-fixed timeline scrubber. Shown only in replay mode after
// frames have been loaded. Drives `replay.currentIdx` /
// `replay.playing` / `replay.speed` on the viewer store; the rAF
// playback engine (`useReplayPlayback`) reads those and advances the
// playhead. Seeks ±30 s walk the frames[] timestamp list rather than
// the index alone — a 5-min gap (e.g. paused recorder) wouldn't be
// fairly represented by a fixed step count.

import { useViewerStore } from "../state/viewerStore";
import type { Snapshot } from "../state/types";
import { IS_ZZ } from "../zz/build";
import { L } from "../zz/l";
import { t } from "../zz/tr";
import {
  frameIndexAtOrAfter, idxToScrubValue, scrubMax, scrubPct, scrubSeekTarget,
} from "./timelineMap";

function fmtMMSS(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

function snapMs(s: Snapshot | undefined | null): number {
  if (!s || !s.timestamp) return 0;
  const t = Date.parse(s.timestamp);
  return Number.isFinite(t) ? t : 0;
}

const SPEEDS = [1, 2, 4, 8] as const;

export function TimelineBar() {
  const mode    = useViewerStore((s) => s.mode);
  const frames  = useViewerStore((s) => s.replay.frames);
  const idx     = useViewerStore((s) => s.replay.currentIdx);
  const playing = useViewerStore((s) => s.replay.playing);
  const speed   = useViewerStore((s) => s.replay.speed);
  const setReplay = useViewerStore((s) => s.setReplay);

  if (mode !== "replay" || frames.length === 0) return null;

  const lastIdx = frames.length - 1;
  const startMs = snapMs(frames[0]);
  const endMs   = snapMs(frames[lastIdx]);
  const curMs   = snapMs(frames[idx]);
  const durationMs = Math.max(1, endMs - startMs);
  const elapsedMs  = Math.max(0, curMs - startMs);

  // Seek to a specific frame index. Rebases playback anchors so play
  // resumes from the new position cleanly.
  const seekToIdx = (newIdx: number) => {
    const clamped = Math.max(0, Math.min(lastIdx, newIdx));
    setReplay((r) => ({ ...r, currentIdx: clamped,
                        baseWallMs: 0, baseSnapMs: 0 }));
  };

  // ±N-second jump along the timestamp axis (not the index axis).
  const seekDeltaMs = (deltaMs: number) => {
    seekToIdx(frameIndexAtOrAfter((i) => snapMs(frames[i]), frames.length,
                                  curMs + deltaMs));
  };

  // The slider value is the frame index itself (see timelineMap.ts), so a
  // click, a drag and the arrow keys all land on exactly the frame they name,
  // and the `change` that follows `input` is a no-op rather than a second seek.
  const onScrub = (e: React.ChangeEvent<HTMLInputElement>) => {
    const target = scrubSeekTarget(e.target.value,
      useViewerStore.getState().replay.currentIdx, frames.length);
    if (target !== null) seekToIdx(target);
  };

  const togglePlay = () => {
    setReplay((r) => ({
      ...r,
      playing: !r.playing,
      // Rebase anchors so resume starts from the current playhead.
      baseWallMs: 0,
      baseSnapMs: 0,
      // If at the end, pressing play rewinds to start.
      currentIdx: r.currentIdx >= lastIdx ? 0 : r.currentIdx,
    }));
  };

  const setSpeed = (sp: number) => {
    setReplay((r) => ({ ...r, speed: sp, baseWallMs: 0, baseSnapMs: 0 }));
  };

  return (
    <div id="timeline-bar"
         title={L("tl.hint", "Space: play/pause · F: Fit map · Tab: scoreboard")}>
      <button className="tb-btn" onClick={togglePlay}
              title={playing ? L("tl.pause", "pause (space)") : L("tl.play", "play (space)")}>
        {playing ? "⏸" : "▶"}
      </button>
      <button className="tb-btn" onClick={() => seekDeltaMs(-30_000)}
              title={L("tl.back", "back 30s")}>{L("tl.backLabel", "⏮ 30s")}</button>
      <button className="tb-btn" onClick={() => seekDeltaMs(30_000)}
              title={L("tl.forward", "forward 30s")}>{L("tl.forwardLabel", "30s ⏭")}</button>
      <input className="tb-scrub" type="range"
             min={0} max={scrubMax(frames.length)} step={1}
             value={idxToScrubValue(idx, frames.length)} onChange={onScrub}
             title={L("tl.scrub", "timeline (drag)")}
             style={{ "--pct": `${scrubPct(idx, frames.length)}%` } as React.CSSProperties} />
      <span className="tb-clock">
        {fmtMMSS(elapsedMs)} / {fmtMMSS(durationMs)}
      </span>
      <div className="tb-speeds" title={IS_ZZ ? t("tl.speed") : undefined}>
        {SPEEDS.map((sp) => (
          <button key={sp}
                  className={"tb-spd " + (speed === sp ? "active" : "")}
                  onClick={() => setSpeed(sp)}>
            {sp}×
          </button>
        ))}
      </div>
    </div>
  );
}
