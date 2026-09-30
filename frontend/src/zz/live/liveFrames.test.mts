// Live frames: decoding (full / position / markers), position -> full frame
// reconstruction, playout pacing, the sink (gap / reset / end handling) and the
// store hooks the live path relies on (render delay, resetLive).
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  LiveDecoder, PlayoutQueue, createLiveSink, isValidMatchToken, LATE_RESYNC_MS,
  type LiveItem, type LiveStoreApi,
} from "./liveFrames.ts";
import { useViewerStore, RENDER_DELAY_MS, clampRenderDelayMs } from "../../state/viewerStore.ts";
import type { Snapshot } from "../../state/types.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

const T0 = Date.parse("2026-09-30T10:00:00.000Z");
const iso = (ms: number) => new Date(T0 + ms).toISOString();

function fullFrame(ms: number, x = 10, y = 20): Record<string, unknown> {
  return {
    timestamp: iso(ms), tick: ms, server: "s", schemaVersion: 1,
    gameState: { mapName: "Fallujah", layer: "Fallujah_RAAS_v1", matchState: "InProgress" },
    teams: [], squads: [],
    players: [{
      name: "Player 1", eosId: "px1", teamId: 1, roleId: null, stats: {},
      soldier: { position: { x, y, z: 5 }, health: 100, yaw: 0 },
    }],
    vehicles: [{ id: "v1", team: 1, health: 900, position: { x: 50, y: 60, z: 0 }, yaw: 0 }],
    markers: [{ id: "m" }], damageEvents: [{ victim: "x" }],
  };
}
function posFrame(ms: number, x: number, y: number): Record<string, unknown> {
  return {
    t: "pos", tick: ms, timestamp: iso(ms),
    players: [{ id: "px1", x, y, z: 6, h: 80, yaw: 1.5 }],
    vehicles: [{ id: "v1", x: 55, y: 65, h: 800, yaw: 2, team: 1 }],
  };
}

// --- decoder: full frame passes, pos frame becomes a full frame
{
  const d = new LiveDecoder();
  const a = d.push(fullFrame(0));
  ok(a?.kind === "frame", "full frame decoded");
  const b = d.push(posFrame(250, 11, 21));
  ok(b?.kind === "frame", "pos frame decoded");
  const snap = (b as Extract<LiveItem, { kind: "frame" }>).frame as unknown as Snapshot;
  eq(snap.players[0]!.soldier!.position, { x: 11, y: 21, z: 6 }, "pos spliced into the full frame");
  eq(snap.players[0]!.soldier!.health, 80, "pos health");
  eq(snap.players[0]!.name, "Player 1", "identity kept from the full frame");
  eq(snap.vehicles[0]!.position!.x, 55, "vehicle position");
  eq(snap.timestamp, iso(250), "pos timestamp used");
  eq(snap.damageEvents, [], "pos frame carries no kill events (no double count)");
  ok(snap.gameState === (a as Extract<LiveItem, { kind: "frame" }>).frame.gameState,
     "discrete state shared by reference");
  eq((b as Extract<LiveItem, { kind: "frame" }>).mediaMs, T0 + 250, "mediaMs");
}

// --- decoder: pos before any full frame is dropped and counted
{
  const d = new LiveDecoder();
  eq(d.push(posFrame(0, 1, 1)), null, "orphan pos dropped");
  eq(d.dropped, 1, "counted");
  eq(d.push(null), null, "null line"); eq(d.push("x"), null, "string line");
  eq(d.push([1]), null, "array line"); eq(d.push({ foo: 1 }), null, "unknown shape");
  eq(d.push({ t: "weird" }), null, "unknown marker");
  eq(d.push({ timestamp: iso(0) }), null, "no players array");
  eq(d.push({ timestamp: "nope", players: [] }), null, "bad timestamp");
  eq(d.push({ t: "pos", timestamp: iso(0), players: "x", vehicles: [] }), null, "bad pos shape");
}

// --- decoder: out-of-order frames dropped; markers
{
  const d = new LiveDecoder();
  ok(d.push(fullFrame(1000)) !== null, "first");
  eq(d.push(fullFrame(500)), null, "older frame dropped");
  ok(d.push(fullFrame(1000)) !== null, "equal timestamp is fine");
  eq(d.push({ t: "seg", seq: 3, epoch: 1 }), null, "seg marker is informational");
  eq(d.push({ t: "end", matchToken: "abc123" }), { kind: "end", matchToken: "abc123" }, "end");
  eq(d.push({ t: "end", matchToken: "bad token!" }), { kind: "end", matchToken: null }, "bad token -> null");
  eq(d.push({ t: "end" }), { kind: "end", matchToken: null }, "missing token -> null");
  ok(isValidMatchToken("a1_b-2.c") && !isValidMatchToken("") && !isValidMatchToken("x/y")
     && !isValidMatchToken(5), "token validation");
}

// --- decoder: gap and reset forget the base frame and the clock
{
  const d = new LiveDecoder();
  d.push(fullFrame(5000));
  eq(d.push({ t: "gap" }), { kind: "gap" }, "gap item");
  eq(d.push(posFrame(5250, 1, 1)), null, "pos after a gap needs a new full frame");
  ok(d.push(fullFrame(100)) !== null, "older timestamps are fine after a gap (clock reset)");
  eq(d.push({ t: "reset", epoch: 7 }), { kind: "reset", epoch: 7 }, "reset item with epoch");
  eq(d.push({ t: "reset" }), { kind: "reset", epoch: null }, "reset without epoch");
  eq(d.push(posFrame(300, 1, 1)), null, "pos after a reset needs a new full frame");
}

// --- playout queue: paced by the frames' own timestamps
function frameItem(ms: number): LiveItem {
  return { kind: "frame", frame: fullFrame(ms) as unknown as Snapshot, mediaMs: T0 + ms };
}
{
  const q = new PlayoutQueue();
  for (let i = 0; i < 5; i++) q.push(frameItem(i * 250));
  eq(q.drain(1000).length, 1, "the first frame plays at once");
  eq(q.nextDueMs(1000), 250, "next one is due in 250 ms");
  eq(q.drain(1200).length, 0, "not yet");
  eq(q.drain(1250).length, 1, "second at +250");
  eq(q.drain(1750).length, 2, "the rest as they come due");
  eq(q.size, 1, "one left");
  eq(q.drain(5000).length, 1, "last");
  eq(q.nextDueMs(5000), null, "empty queue has no due time");
}

// --- playout queue: a late segment re-anchors instead of bursting
{
  const q = new PlayoutQueue();
  q.push(frameItem(0));
  eq(q.drain(0).length, 1, "first frame");
  // The next segment arrives 10 s late: 4 frames, 250 ms apart.
  for (let i = 1; i <= 4; i++) q.push(frameItem(i * 250));
  const got = q.drain(10_000);
  eq(got.length, 1, "only the first late frame is released, not a burst");
  eq(q.nextDueMs(10_000), 250, "the rest keep their natural spacing");
  eq(q.drain(10_250).length, 1, "next at +250 after re-anchoring");
  ok(LATE_RESYNC_MS >= 1000, "resync threshold is sane");
}

// --- playout queue: gap / reset markers release in order and re-anchor
{
  const q = new PlayoutQueue();
  q.push(frameItem(0));
  q.push({ kind: "gap" });
  q.push(frameItem(3_600_000));            // an hour later in media time
  q.push(frameItem(3_600_250));
  const out = q.drain(0);
  eq(out.map((i) => i.kind), ["frame", "gap", "frame"], "frame, gap, then the new base frame at once");
  eq(q.nextDueMs(0), 250, "spacing continues from the new base");
}

// --- sink (paced): frames reach the store on schedule; gap/reset reset the store
function fakeStore(delay = 6000) {
  const log: string[] = [];
  const ingested: string[] = [];
  const api: LiveStoreApi = {
    ingest: (s) => { ingested.push(s.timestamp); log.push("ingest"); },
    reset: () => { log.push("reset"); },
    renderDelayMs: () => delay,
  };
  return { api, log, ingested };
}
{
  const { api, log, ingested } = fakeStore();
  const ends: (string | null)[] = [];
  const disc: string[] = [];
  const sink = createLiveSink({
    store: api, paced: true, now: () => 0,
    handlers: { onEnd: (t) => ends.push(t), onDiscontinuity: (k) => disc.push(k) },
  });
  sink.push(fullFrame(0));
  sink.push(posFrame(250, 11, 21));
  sink.push(posFrame(500, 12, 22));
  sink.pump(0);
  eq(ingested.length, 1, "first frame at once");
  sink.pump(250);
  eq(ingested.length, 2, "second at +250");
  eq(sink.nextDueMs(250), 250, "next due in 250");
  sink.pump(500);
  eq(ingested, [iso(0), iso(250), iso(500)], "frames arrive in order");
  // gap: store is reset, then the new stream starts
  sink.push({ t: "gap" });
  sink.push(fullFrame(60_000));
  sink.pump(600);
  eq(log.filter((l) => l === "reset").length, 1, "gap resets the store");
  eq(disc, ["gap"], "discontinuity reported");
  eq(ingested[ingested.length - 1], iso(60_000), "frame after the gap is shown at once");
  sink.push({ t: "reset", epoch: 2 });
  sink.pump(700);
  eq(disc, ["gap", "reset"], "reset reported");
  eq(log.filter((l) => l === "reset").length, 2, "reset resets the store again");
  // pos frame after reset without a full frame: dropped
  sink.push(posFrame(61_000, 1, 1));
  sink.pump(800);
  eq(sink.stats().dropped >= 1, true, "orphan pos after reset dropped");

  // end: fires only after the canvas delay (frames still on screen)
  sink.push(fullFrame(62_000));
  sink.push({ t: "end", matchToken: "tok1" });
  sink.pump(900);
  eq(ends, [], "end is not shown right away");
  sink.pump(900 + 5999);
  eq(ends, [], "still waiting for the drawn frames to catch up");
  sink.pump(900 + 6000);
  eq(ends, ["tok1"], "end shown after the render delay");
  // the same end marker again (restart re-read) is ignored
  sink.push({ t: "end", matchToken: "tok1" });
  sink.pump(20_000);
  sink.pump(30_000);
  eq(ends, ["tok1"], "same end marker once");
  sink.push({ t: "end", matchToken: "tok2" });
  sink.pump(31_000);
  sink.pump(40_000);
  eq(ends, ["tok1", "tok2"], "a different match ends separately");
  sink.hardReset();
  eq(sink.stats().queued, 0, "hardReset empties the queue");
}

// --- sink (admin, unpaced): straight to the store; match events
{
  const { api, log, ingested } = fakeStore(2000);
  let t = 0;
  const matches: string[] = [];
  const sink = createLiveSink({
    store: api, paced: false, now: () => t,
    handlers: { onEnd: () => {}, onMatch: (id, s) => matches.push(`${id}:${s}`) },
  });
  sink.push(fullFrame(0));
  eq(ingested, [iso(0)], "admin frame goes straight to the store");
  sink.push(posFrame(250, 1, 1));
  eq(ingested.length, 2, "admin pos frame too");
  t = 100;
  sink.match("m1", "ended");
  eq(matches, [], "ended is deferred by the 2 s render delay");
  sink.pump(2099);
  eq(matches, [], "not yet");
  sink.pump(2100);
  eq(matches, ["m1:ended"], "shown after the delay");
  sink.match("m2", "started");
  eq(matches, ["m1:ended", "m2:started"], "started is immediate");
  eq(log.filter((l) => l === "reset").length, 1, "a new match resets the store");
  sink.push(posFrame(500, 1, 1));
  eq(ingested.length, 2, "pos before the next full frame is dropped after a match start");
  sink.push({ t: "reset" });
  eq(log.filter((l) => l === "reset").length, 2, "admin reset resets the store at once");
}

// --- store: render delay defaults to 6000 and is settable/clamped
{
  eq(RENDER_DELAY_MS, 6000, "default render delay unchanged (6000 ms)");
  const st = () => useViewerStore.getState();
  eq(st().renderDelayMs, 6000, "store default is 6000");
  st().setRenderDelayMs(2000);
  eq(st().renderDelayMs, 2000, "admin delay 2000");
  st().setRenderDelayMs(5);
  eq(st().renderDelayMs, 500, "clamped low");
  st().setRenderDelayMs(9e9);
  eq(st().renderDelayMs, 30000, "clamped high");
  st().setRenderDelayMs(Number.NaN);
  eq(st().renderDelayMs, 6000, "NaN falls back to the default");
  eq(clampRenderDelayMs(1234.4), 1234, "rounded");
  st().setRenderDelayMs(RENDER_DELAY_MS);
}

// --- store: ingestLive through the live path, then resetLive wipes it
{
  const st = () => useViewerStore.getState();
  st().setMode("live");
  const d = new LiveDecoder();
  const item = d.push(fullFrame(0)) as Extract<LiveItem, { kind: "frame" }>;
  st().ingestLive(item.frame);
  ok(st().curSnap !== null && st().snapBuffer.length === 1, "ingestLive fed the store");
  st().pushKillFeed([{ id: "k" } as never]);
  st().setSelectedPlayerKey("px1");
  let modes: string[] = [];
  const unsub = useViewerStore.subscribe((s) => { modes.push(s.mode); });
  st().resetLive();
  unsub();
  eq(st().curSnap, null, "resetLive clears curSnap");
  eq(st().prevSnap, null, "and prevSnap");
  eq(st().snapBuffer.length, 0, "and the render buffer");
  eq(st().killFeed.length, 0, "and the kill feed");
  eq(st().selectedPlayerKey, null, "and the selection");
  eq(st().mode, "live", "mode is live again");
  ok(modes.includes("home") && modes[modes.length - 1] === "live",
     "mode flips out of and back to live (re-seeds useKillFeed)");
  st().setMode("home");
}

// --- MapCanvas reads the delay from the store, not the constant
{
  const root = existsSync(join(process.cwd(), "src", "canvas"))
    ? join(process.cwd(), "src") : join(process.cwd(), "frontend", "src");
  const src = readFileSync(join(root, "canvas", "MapCanvas.tsx"), "utf8");
  ok(/performance\.now\(\) - s\.renderDelayMs/.test(src), "MapCanvas uses s.renderDelayMs");
  ok(!/import \{[^}]*RENDER_DELAY_MS[^}]*\} from/.test(src),
     "MapCanvas no longer imports the constant");
}

console.log(`zz live frames: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
