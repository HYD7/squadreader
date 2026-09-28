// Read-only test hook for automated checks (Zero Zone build only).
//
// Exposes the playhead and the current entity positions as getters. There is
// deliberately no setter and no method that changes viewer state: the object
// is frozen and defined non-writable / non-configurable.
import { useViewerStore } from "../state/viewerStore";
import { replayClock } from "../state/replayClock";

export interface ZzPlayerPos {
  /** eosId ?? name — the same identity the canvas matches on. */
  id: string | null;
  name: string | null;
  teamId: number | null;
  x: number | null;
  y: number | null;
  z: number | null;
}
export interface ZzVehiclePos {
  id: string;
  team: number | null;
  x: number | null;
  y: number | null;
  z: number | null;
}

export interface ZzReplayState {
  readonly id: string | null;
  readonly loaded: boolean;
  readonly currentIdx: number;
  readonly totalFrames: number;
  readonly playing: boolean;
  readonly speed: number;
  /** Continuous playhead (ms since epoch), null while not driving the canvas. */
  readonly playheadMs: number | null;
  /** Timestamp of frames[currentIdx], ISO string. */
  readonly frameTimestamp: string | null;
  /** Player positions of frames[currentIdx]. */
  readonly players: ZzPlayerPos[];
  /** Vehicle positions of frames[currentIdx]. */
  readonly vehicles: ZzVehiclePos[];
}

declare global {
  interface Window { __ZZ_REPLAY__?: ZzReplayState }
}

export function installZzReplayHook(): void {
  const st = () => useViewerStore.getState();
  const frame = () => {
    const r = st().replay;
    return r.frames[r.currentIdx] ?? null;
  };
  const hook: ZzReplayState = {
    get id() { return st().replay.id; },
    get loaded() { return st().replay.frames.length > 0; },
    get currentIdx() { return st().replay.currentIdx; },
    get totalFrames() { return st().replay.frames.length; },
    get playing() { return st().replay.playing; },
    get speed() { return st().replay.speed; },
    get playheadMs() { return replayClock.valid ? replayClock.ms : null; },
    get frameTimestamp() { return frame()?.timestamp ?? null; },
    get players() {
      return (frame()?.players ?? []).map((p) => ({
        id: p.eosId ?? p.name ?? null,
        name: p.name ?? null,
        teamId: p.teamId ?? null,
        x: p.soldier?.position?.x ?? null,
        y: p.soldier?.position?.y ?? null,
        z: p.soldier?.position?.z ?? null,
      }));
    },
    get vehicles() {
      return (frame()?.vehicles ?? []).map((v) => ({
        id: v.id,
        team: v.team ?? null,
        x: v.position?.x ?? null,
        y: v.position?.y ?? null,
        z: v.position?.z ?? null,
      }));
    },
  };
  Object.defineProperty(window, "__ZZ_REPLAY__", {
    value: Object.freeze(hook),
    writable: false,
    configurable: false,
    enumerable: false,
  });
}
