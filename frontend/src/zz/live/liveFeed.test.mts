// The whole live pipeline (source -> sink -> store -> end layer) with fake
// fetch, EventSource, timers and store: the same wiring useLiveFeed uses.
import { createLiveFeed, PUBLIC_RENDER_DELAY_MS, type LiveFeedStore } from "./liveFeed.ts";
import { ADMIN_RENDER_DELAY_MS, type EventSourceLike, type MessageEventLike } from "./sseSource.ts";
import { fakeTimers } from "./fakeTimers.ts";
import { liveDebug } from "./liveDebug.ts";
import { RENDER_DELAY_MS } from "../../state/viewerStore.ts";
import type { ZzLiveLocation } from "../mode.ts";
import type { ConnState } from "./liveUrls.ts";
import type { EndState } from "./liveEnd.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

eq(PUBLIC_RENDER_DELAY_MS, RENDER_DELAY_MS, "public canvas delay is the unchanged default");

const T0 = Date.parse("2026-09-30T10:00:00.000Z");
const iso = (ms: number) => new Date(T0 + ms).toISOString();
function full(ms: number): Record<string, unknown> {
  return {
    timestamp: iso(ms), tick: ms, server: "s", schemaVersion: 1,
    gameState: { mapName: "Fallujah", matchState: "InProgress" }, teams: [], squads: [],
    players: [{ name: "Player 1", eosId: "px1", teamId: 1, stats: {},
                soldier: { position: { x: 1, y: 2, z: 3 }, health: 100, yaw: 0 } }],
    vehicles: [], damageEvents: [],
  };
}
function pos(ms: number, x: number): Record<string, unknown> {
  return { t: "pos", timestamp: iso(ms), players: [{ id: "px1", x, y: 2, z: 3 }], vehicles: [] };
}

function fakeStore() {
  const ingested: { ts: string; x: number | null | undefined }[] = [];
  let resets = 0;
  let delay = 6000;
  const statuses: string[] = [];
  const store: LiveFeedStore = {
    ingest: (s) => ingested.push({ ts: s.timestamp, x: s.players[0]?.soldier?.position?.x }),
    reset: () => { resets++; },
    renderDelayMs: () => delay,
    setRenderDelayMs: (ms) => { delay = ms; },
    setStatus: (s) => { statuses.push(s); },
  };
  return { store, ingested, resets: () => resets, delay: () => delay, statuses };
}

// ===== public feed ========================================================
{
  const timers = fakeTimers();
  const fs = fakeStore();
  let latest = 3;
  let sonucCalls = 0;
  let sonucReady = false;
  const served: number[] = [];
  const navigated: string[] = [];
  const conns: ConnState[] = [];
  const ends: EndState[] = [];
  const delays: (number | null)[] = [];

  const body = (lines: unknown[]) => new Response(
    lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  const fetchImpl = (async (input: unknown) => {
    const url = String(input);
    if (url.includes("/durum")) {
      return new Response(JSON.stringify({
        serverId: "zz-7787", epoch: 1, latestSeq: latest, oldestSeq: 1, delaySec: 600,
        live: true, ended: false, viewerToken: "vt" }));
    }
    const m = /\/parca\/(\d+)/.exec(url);
    if (m) {
      const seq = Number(m[1]);
      served.push(seq);
      const base = (seq - 1) * 1000;
      const lines: unknown[] = [{ t: "seg", seq }, full(base), pos(base + 250, 10 + seq),
                                pos(base + 500, 20 + seq), pos(base + 750, 30 + seq)];
      if (seq === 4) lines.push({ t: "end", matchToken: "tokA" });
      return body(lines);
    }
    if (url.includes("/sonuc/tokA")) {
      sonucCalls++;
      return new Response(JSON.stringify(sonucReady
        ? { state: "ready", url: "/maclar/42", replay: true } : { state: "pending" }));
    }
    return new Response("", { status: 404 });
  }) as typeof fetch;

  const loc: ZzLiveLocation = { ok: true, kind: "live", server: "zz-7787" };
  const feed = createLiveFeed(loc, {
    store: fs.store, timers, fetchImpl, now: () => timers.now(),
    navigate: (u) => navigated.push(u),
  }, {
    onConn: (s) => conns.push(s), onDelay: (d) => delays.push(d), onEnd: (s) => ends.push(s),
  });
  feed.start();
  eq(fs.delay(), 6000, "public feed keeps the default canvas delay");
  eq(fs.resets(), 1, "the store is reset at start");
  await timers.advance(10);
  eq(served, [1, 2, 3], "started 3 segments back, in order");
  eq(conns.slice(-1)[0], "live", "connection is live");
  eq(delays[0], 600, "delay comes from the status");
  eq(fs.ingested.length, 1, "only the first frame at once (the rest is paced)");
  eq(fs.ingested[0]!.ts, iso(0), "first frame first");
  await timers.advance(750);
  eq(fs.ingested.map((f) => f.ts), [iso(0), iso(250), iso(500), iso(750)],
     "position frames follow at their own 250 ms rhythm");
  eq(fs.ingested.map((f) => f.x), [1, 11, 21, 31], "position frames are folded into full frames");
  await timers.advance(2200);
  eq(fs.ingested.length, 12, "3 segments x 4 frames after ~3 s");
  eq(liveDebug.frames, 12, "debug counter follows");
  eq(liveDebug.segments, 3, "debug segment count");
  eq(liveDebug.conn, "live", "debug connection");
  eq(liveDebug.kind, "live", "debug kind");

  // segment 4 arrives with the end marker
  latest = 4;
  await timers.advance(5500);
  eq(served, [1, 2, 3, 4], "new segment fetched");
  eq(sonucCalls, 0, "no result polling before the frames have been shown");
  eq(ends.filter((e) => e.phase !== "hidden").length, 0, "end layer not shown yet");
  // frames of segment 4 are due at media 3000..3750; the layer waits the canvas delay
  await timers.advance(6500);
  ok(ends.some((e) => e.phase === "pending"), "end layer shows pending");
  ok(sonucCalls >= 1, "result endpoint polled");
  eq(navigated.length, 0, "no redirect while pending");
  sonucReady = true;
  await timers.advance(5200);
  const ready = ends[ends.length - 1] as Extract<EndState, { phase: "ready" }>;
  eq(ready.phase, "ready", "ready");
  eq(ready.url, "/maclar/42", "ready url");
  await timers.advance(10_500);
  eq(navigated, ["/maclar/42"], "redirects after the 10 s countdown");
  const servedBefore = served.length;
  await timers.advance(30_000);
  eq(served.length, servedBefore, "source stays stopped after the end marker");

  // Next match: layer hides, store reset, polling resumes after the end segment.
  const resetsBefore = fs.resets();
  latest = 6;
  feed.next();
  ok(fs.resets() > resetsBefore, "next match resets the store");
  eq(ends[ends.length - 1]!.phase, "hidden", "layer hidden");
  await timers.advance(100);
  eq(served.slice(servedBefore), [5, 6], "resumes after the end segment (no re-read of 4)");
  await timers.advance(20_000);
  eq(ends.filter((e) => e.phase === "pending").length, 1, "the old end marker does not reopen the layer");
  feed.stop();
  const n = served.length;
  await timers.advance(60_000);
  eq(served.length, n, "stop() ends all polling");
}

// ===== admin feed =========================================================
class FakeES implements EventSourceLike {
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  private handlers = new Map<string, ((ev: MessageEventLike) => void)[]>();
  constructor(public url: string) {}
  addEventListener(t: string, fn: (ev: MessageEventLike) => void) {
    this.handlers.set(t, [...(this.handlers.get(t) ?? []), fn]);
  }
  close() { this.readyState = 2; }
  open() { this.readyState = 1; this.onopen?.({}); }
  emit(t: string, data: unknown) { for (const fn of this.handlers.get(t) ?? []) fn({ data }); }
}
{
  const timers = fakeTimers();
  const fs = fakeStore();
  const ends: EndState[] = [];
  const conns: ConnState[] = [];
  const navigated: string[] = [];
  let fetches = 0;
  let es!: FakeES;
  const loc: ZzLiveLocation = { ok: true, kind: "admin-live", server: "zz-7787", ticket: "tkt-1" };
  const feed = createLiveFeed(loc, {
    store: fs.store, timers, now: () => timers.now(),
    fetchImpl: (async () => { fetches++; return new Response("{}"); }) as typeof fetch,
    eventSourceFactory: (url) => { es = new FakeES(url); return es; },
    navigate: (u) => navigated.push(u),
  }, { onConn: (s) => conns.push(s), onDelay: () => {}, onEnd: (s) => ends.push(s) });
  feed.start();
  eq(fs.delay(), ADMIN_RENDER_DELAY_MS, "admin canvas delay is 2000 ms");
  eq(es.url, "/admin/api/squadreader/canli/zz-7787/akis?bilet=tkt-1", "admin stream url");
  es.open();
  eq(conns.slice(-1)[0], "live", "admin live");
  es.emit("frame", JSON.stringify(full(0)));
  es.emit("frame", JSON.stringify(pos(250, 44)));
  eq(fs.ingested.map((f) => f.x), [1, 44], "admin frames go straight to the store");
  es.emit("match", JSON.stringify({ matchId: "m9", state: "ended" }));
  eq(ends.filter((e) => e.phase !== "hidden").length, 0, "info layer waits for the drawn frames");
  await timers.advance(2100);
  eq(ends[ends.length - 1], { phase: "info", hasToken: false }, "admin info layer");
  await timers.advance(60_000);
  eq(fetches, 0, "admin never polls the result endpoint");
  eq(navigated.length, 0, "admin is never redirected");
  const resets = fs.resets();
  es.emit("match", JSON.stringify({ matchId: "m10", state: "started" }));
  eq(ends[ends.length - 1]!.phase, "hidden", "next match hides the layer");
  ok(fs.resets() > resets, "next match resets the store");
  const r2 = fs.resets();
  es.emit("reset", "{}");
  ok(fs.resets() > r2, "admin reset event resets the store");
  feed.stop();
}

console.log(`zz live feed: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
