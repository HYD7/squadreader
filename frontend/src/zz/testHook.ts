// Read-only test hook for automated checks (Zero Zone build only).
//
// Exposes the playhead and the current entity positions as getters. There is
// deliberately no setter and no method that changes viewer state: the object
// is frozen and defined non-writable / non-configurable.
import { useViewerStore } from "../state/viewerStore";
import { replayClock } from "../state/replayClock";
import { liveDebug } from "./live/liveDebug";

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

/** Live feed state (`?mode=live` / `?mode=admin-live`). Read-only as well. */
export interface ZzLiveState {
  readonly kind: "live" | "admin-live" | null;
  readonly server: string | null;
  /** connecting | live | reconnecting | full | closed */
  readonly connection: string | null;
  readonly delaySec: number | null;
  /** hidden | info | pending | ready | failed */
  readonly endLayer: string;
  /** Frames handed to the store / lines dropped / frames still queued. */
  readonly frames: number;
  readonly dropped: number;
  readonly queued: number;
  readonly segments: number;
  readonly lastSeq: number | null;
  readonly epoch: number | null;
  /** Render delay of the canvas (ms). */
  readonly renderDelayMs: number;
  /** Timestamp of the newest frame handed to the store, ISO string. */
  readonly frameTimestamp: string | null;
  readonly players: ZzPlayerPos[];
  readonly vehicles: ZzVehiclePos[];
}

declare global {
  interface Window { __ZZ_REPLAY__?: ZzReplayState; __ZZ_LIVE__?: ZzLiveState }
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

export function installZzLiveHook(): void {
  const st = () => useViewerStore.getState();
  const hook: ZzLiveState = {
    get kind() { return liveDebug.kind; },
    get server() { return liveDebug.server; },
    get connection() { return liveDebug.conn; },
    get delaySec() { return liveDebug.delaySec; },
    get endLayer() { return liveDebug.end; },
    get frames() { return liveDebug.frames; },
    get dropped() { return liveDebug.dropped; },
    get queued() { return liveDebug.queued; },
    get segments() { return liveDebug.segments; },
    get lastSeq() { return liveDebug.lastSeq; },
    get epoch() { return liveDebug.epoch; },
    get renderDelayMs() { return st().renderDelayMs; },
    get frameTimestamp() { return st().curSnap?.timestamp ?? null; },
    get players() {
      return (st().curSnap?.players ?? []).map((p) => ({
        id: p.eosId ?? p.name ?? null,
        name: p.name ?? null,
        teamId: p.teamId ?? null,
        x: p.soldier?.position?.x ?? null,
        y: p.soldier?.position?.y ?? null,
        z: p.soldier?.position?.z ?? null,
      }));
    },
    get vehicles() {
      return (st().curSnap?.vehicles ?? []).map((v) => ({
        id: v.id,
        team: v.team ?? null,
        x: v.position?.x ?? null,
        y: v.position?.y ?? null,
        z: v.position?.z ?? null,
      }));
    },
  };
  Object.defineProperty(window, "__ZZ_LIVE__", {
    value: Object.freeze(hook),
    writable: false,
    configurable: false,
    enumerable: false,
  });
}
