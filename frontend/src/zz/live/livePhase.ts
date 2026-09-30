// Loading-layer phase of the live view, with hysteresis. Pure (time and buffer
// numbers are passed in), so it runs under the node test runner.
//
// Before the first map draw the layers are honest: "bağlanılıyor" (no data),
// "hazırlanıyor" (buffer filling). Once the map has been drawn, short holes
// between 5 s segments must not bring the big layer back: they are silent; only
// a long outage (>= PHASE_HOLD_MS without renderable data) shows the small
// "Yayın verisi bekleniyor" banner.

export type Phase = "connecting" | "warmup" | "stalled" | "running";

/** 2 segments (2 x 5 s). */
export const PHASE_HOLD_MS = 10_000;

export interface PhaseInput {
  /** Number of buffered frames. */
  count: number;
  /** Arrival time of the oldest / newest buffered frame (ms). */
  first: number;
  last: number;
  now: number;
  renderDelayMs: number;
}

export interface PhaseTracker {
  evaluate(i: PhaseInput): { phase: Phase; pct: number };
}

export function createPhaseTracker(holdMs: number = PHASE_HOLD_MS): PhaseTracker {
  let drawn = false;          // the map has been drawn at least once
  let lastRunningAt = 0;      // last moment the canvas had data to show
  return {
    evaluate(i) {
      if (i.count === 0) {
        drawn = false;        // reset / new match: start honest again
        return { phase: "connecting", pct: 0 };
      }
      const renderClock = i.now - i.renderDelayMs;
      const starved = renderClock < i.first || renderClock > i.last;
      if (!starved) {
        drawn = true;
        lastRunningAt = i.now;
        return { phase: "running", pct: 1 };
      }
      if (drawn) {
        if (i.now - lastRunningAt < holdMs) return { phase: "running", pct: 1 };
        return { phase: "stalled", pct: 1 };
      }
      if (renderClock < i.first) {
        return { phase: "warmup",
                 pct: Math.max(0, Math.min(1, (i.now - i.first) / i.renderDelayMs)) };
      }
      return { phase: "stalled", pct: 1 };
    },
  };
}
