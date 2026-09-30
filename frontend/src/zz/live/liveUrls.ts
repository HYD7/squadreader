// Zero Zone live viewer: same-origin URL builders and the event types shared by
// the two live sources. Pure (no DOM), so it runs under the node test runner.
//
// SECURITY: every URL is a root-absolute path of the page's own origin (no
// scheme, no host). The admin ticket (`bilet`) is only ever placed in the query
// string of the SSE URL; nothing here or in the sources logs it.
import type { ZzLiveKind } from "../mode";

/** Connection states shown to the viewer. */
export type ConnState = "connecting" | "live" | "reconnecting" | "full" | "ip_limit" | "closed"
  /** Admin SSE: the ticket (10 min) expired; only a page reload gets a new one. */
  | "ticket_expired";

/** What a live source reports to the feed controller. */
export type SourceEvent =
  | { type: "conn"; state: ConnState }
  | { type: "info"; info: LiveInfo }
  /** One parsed NDJSON line / SSE `frame` payload (full frame, pos frame or a
   *  marker: seg | gap | reset | end). Decoded by liveFrames.LiveDecoder. */
  | { type: "line"; obj: unknown }
  /** Admin SSE `match` event. */
  | { type: "match"; matchId: string | null; state: string };

export interface LiveInfo {
  serverId: string | null;
  epoch: number | null;
  /** Delay of the public feed in seconds (null for the admin feed). */
  delaySec: number | null;
  live: boolean;
  ended: boolean;
}

export interface LiveSource {
  start(): void;
  stop(): void;
  /** Start over from the newest data ("Sonraki maçı izle"). */
  restart(): void;
}

export type SourceListener = (ev: SourceEvent) => void;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
};

const API = "/canli/api";

export function statusUrl(server: string): string {
  return `${API}/${encodeURIComponent(server)}/durum`;
}
export function segmentUrl(server: string, seq: number): string {
  return `${API}/${encodeURIComponent(server)}/parca/${Math.trunc(seq)}`;
}
export function resultUrl(server: string, matchToken: string): string {
  return `${API}/${encodeURIComponent(server)}/sonuc/${encodeURIComponent(matchToken)}`;
}
/** Administrator SSE stream. `bilet` stays in the query string only. */
export function adminStreamUrl(server: string, ticket: string): string {
  return `/admin/api/squadreader/canli/${encodeURIComponent(server)}/akis`
    + `?bilet=${encodeURIComponent(ticket)}`;
}

/** Match-page redirect targets must be plain same-origin paths. */
export function isSafeSitePath(p: unknown): p is string {
  return typeof p === "string"
    && /^\/[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*(\?[A-Za-z0-9\-._~!$&'()*+,;=:@%/?]*)?$/.test(p)
    && !p.startsWith("//") && !p.includes("..");
}

export function isAdminKind(kind: ZzLiveKind): boolean {
  return kind === "admin-live";
}

/** "~10 dk" / "~45 sn" for the badge. */
export function formatDelay(delaySec: number | null | undefined): string | null {
  if (delaySec == null || !Number.isFinite(delaySec) || delaySec < 0) return null;
  if (delaySec < 90) return `~${Math.max(1, Math.round(delaySec))} sn`;
  return `~${Math.round(delaySec / 60)} dk`;
}
