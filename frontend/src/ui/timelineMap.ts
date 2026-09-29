// Pure value <-> frame mapping for the replay timeline slider.
//
// The slider's value IS the frame index: min 0, max lastIdx, step 1. Every
// value the slider can hold is therefore a whole frame, so nothing is rounded
// between what the user picked and the frame that is shown.
//
// This replaces a 0..100 percent slider with step 0.05. It seeked to the first
// frame at or after `start + pct * duration`, then React wrote the new frame's
// exact percentage back into the input, the browser snapped that to the 0.05
// grid, and the `change` event that followed carried the snapped value: a
// second seek, sometimes one frame further (click on frame 19, land on 20).
// With whole-frame values the browser has nothing to snap, so the value React
// writes back is the value the browser keeps.

/** Largest slider value for `frameCount` frames (0 for zero or one frame). */
export function scrubMax(frameCount: number): number {
  return Math.max(0, Math.floor(frameCount) - 1);
}

/** Slider value for a frame index (clamped to a valid whole frame). */
export function idxToScrubValue(idx: number, frameCount: number): number {
  if (!Number.isFinite(idx)) return 0;
  return Math.max(0, Math.min(scrubMax(frameCount), Math.round(idx)));
}

/** Frame index for a slider value as the DOM reports it (a string). Null when
 *  the value is not a number, so a bad event can never seek to NaN. */
export function scrubValueToIdx(raw: string | number, frameCount: number): number | null {
  const v = typeof raw === "number" ? raw : parseFloat(raw);
  if (!Number.isFinite(v)) return null;
  return idxToScrubValue(v, frameCount);
}

/** The seek an `input`/`change` event should perform, or null when it would
 *  land on the frame already shown. The browser fires `input` while dragging
 *  and `change` on release, both with the same value: the second is a no-op. */
export function scrubSeekTarget(raw: string | number, currentIdx: number,
                                frameCount: number): number | null {
  const idx = scrubValueToIdx(raw, frameCount);
  return idx === null || idx === currentIdx ? null : idx;
}

/** Fill percentage for the slider track (CSS `--pct`). */
export function scrubPct(idx: number, frameCount: number): number {
  const max = scrubMax(frameCount);
  return max === 0 ? 0 : (idxToScrubValue(idx, frameCount) / max) * 100;
}

/** First frame whose time is >= `targetMs` (or the last frame). `timeAt(i)` is
 *  the time of frame i; times are non-decreasing. Used by the +-30 s jumps. */
export function frameIndexAtOrAfter(timeAt: (i: number) => number,
                                    frameCount: number, targetMs: number): number {
  let lo = 0, hi = Math.max(0, frameCount - 1);
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (timeAt(mid) < targetMs) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}
