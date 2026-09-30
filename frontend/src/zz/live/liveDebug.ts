// Read-only snapshot of the live feed for the test hook (window.__ZZ_LIVE__).
// The feed hook writes it; nothing reads it except testHook.ts.
import type { ConnState } from "./liveUrls";
import type { EndState } from "./liveEnd";

export interface LiveDebug {
  kind: "live" | "admin-live" | null;
  server: string | null;
  conn: ConnState | null;
  delaySec: number | null;
  end: EndState["phase"];
  frames: number;
  dropped: number;
  queued: number;
  segments: number;
  lastSeq: number | null;
  epoch: number | null;
}

export const liveDebug: LiveDebug = {
  kind: null, server: null, conn: null, delaySec: null, end: "hidden",
  frames: 0, dropped: 0, queued: 0, segments: 0, lastSeq: null, epoch: null,
};

export function patchLiveDebug(p: Partial<LiveDebug>): void {
  Object.assign(liveDebug, p);
}
