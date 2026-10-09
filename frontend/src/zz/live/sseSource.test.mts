// Administrator SSE source: event mapping (frame / match / reset / hb),
// reconnect with back-off after the browser gives up, silence watchdog, and the
// ticket staying in the URL only. Fake EventSource + timers.
import {
  createSseSource, ADMIN_RENDER_DELAY_MS, SSE_SILENCE_MS, SSE_GIVE_UP_AFTER,
  type EventSourceLike, type MessageEventLike,
} from "./sseSource.ts";
import type { SourceEvent } from "./liveUrls.ts";
import { fakeTimers } from "./fakeTimers.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

eq(ADMIN_RENDER_DELAY_MS, 2000, "admin render delay is 2000 ms (K7)");

class FakeES implements EventSourceLike {
  readyState = 0;
  onopen: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;
  private handlers = new Map<string, ((ev: MessageEventLike) => void)[]>();
  constructor(public url: string) {}
  addEventListener(type: string, fn: (ev: MessageEventLike) => void) {
    const l = this.handlers.get(type) ?? [];
    l.push(fn);
    this.handlers.set(type, l);
  }
  close() { this.closed = true; this.readyState = 2; }
  // test drivers
  open() { this.readyState = 1; this.onopen?.({}); }
  emit(type: string, data: unknown) {
    for (const fn of this.handlers.get(type) ?? []) fn({ data });
  }
  fail(closed: boolean) { this.readyState = closed ? 2 : 0; this.onerror?.({}); }
}

function setup() {
  const timers = fakeTimers();
  const events: SourceEvent[] = [];
  const streams: FakeES[] = [];
  const src = createSseSource({
    server: "zz-7787", ticket: "secret-ticket-123",
    onEvent: (e) => events.push(e), timers, random: () => 0.5,
    factory: (url) => { const s = new FakeES(url); streams.push(s); return s; },
  });
  return { timers, events, streams, src };
}
const conns = (evs: SourceEvent[]) =>
  evs.filter((e) => e.type === "conn").map((e) => (e as { state: string }).state);

// --- URL and connect
{
  const { events, streams, src } = setup();
  src.start();
  eq(streams.length, 1, "one EventSource opened");
  eq(streams[0]!.url,
     "/admin/api/squadreader/canli/zz-7787/akis?bilet=secret-ticket-123",
     "stream url carries the ticket in the query only");
  ok(streams[0]!.url.startsWith("/"), "same-origin path");
  streams[0]!.open();
  eq(conns(events), ["connecting", "live"], "connecting then live");
  ok(!JSON.stringify(events).includes("secret-ticket-123"), "the ticket is never in an event");
  src.stop();
}

// --- event mapping
{
  const { events, streams, src } = setup();
  src.start();
  const es = streams[0]!;
  es.open();
  es.emit("frame", JSON.stringify({ timestamp: "2026-09-30T10:00:00Z", players: [] }));
  es.emit("frame", "not json");
  es.emit("frame", "");
  es.emit("match", JSON.stringify({ matchId: "m-1", state: "ended" }));
  es.emit("match", JSON.stringify({ matchId: 77, state: "started" }));
  es.emit("match", JSON.stringify({ nope: 1 }));
  es.emit("reset", "{}");
  es.emit("reset", JSON.stringify({ epoch: 4 }));
  es.emit("hb", "");
  const lines = events.filter((e) => e.type === "line").map((e) => (e as { obj: unknown }).obj);
  eq(lines.length, 3, "one frame + two resets become lines (bad json dropped)");
  eq((lines[0] as { timestamp: string }).timestamp, "2026-09-30T10:00:00Z", "frame payload");
  eq(lines[1], { t: "reset" }, "reset without epoch");
  eq(lines[2], { t: "reset", epoch: 4 }, "reset with epoch");
  const matches = events.filter((e) => e.type === "match") as
    unknown as { matchId: string | null; state: string }[];
  eq(matches.map((m) => [m.matchId, m.state]), [["m-1", "ended"], ["77", "started"]], "match events");
  src.stop();
}

// --- network blip (browser retries itself): reconnecting, no new EventSource
{
  const { events, streams, src } = setup();
  src.start();
  streams[0]!.open();
  streams[0]!.fail(false);
  eq(conns(events).pop(), "reconnecting", "browser retry shows reconnecting");
  eq(streams.length, 1, "no second EventSource while the browser retries");
  streams[0]!.open();
  eq(conns(events).pop(), "live", "live again after the browser reconnects");
  src.stop();
}

// --- the browser gave up (CLOSED): reopen with growing back-off
{
  const { timers, events, streams, src } = setup();
  src.start();
  streams[0]!.open();
  streams[0]!.fail(true);
  ok(streams[0]!.closed, "dead stream is closed");
  eq(conns(events).pop(), "reconnecting", "reconnecting after a closed stream");
  await timers.advance(1000);
  eq(streams.length, 2, "reopened after the first back-off");
  ok(streams[1]!.url === streams[0]!.url, "same url on reconnect");
  streams[1]!.fail(true);
  await timers.advance(1000);
  eq(streams.length, 2, "second back-off is longer than 1 s");
  await timers.advance(2000);
  eq(streams.length, 3, "third stream after ~2 s");
  streams[2]!.open();
  eq(conns(events).pop(), "live", "live again");
  // a successful open resets the back-off
  streams[2]!.fail(true);
  await timers.advance(1000);
  eq(streams.length, 4, "back-off restarts at 1 s after a success");
  src.stop();
}

// --- many failures in a row without ever opening: state becomes closed, keeps trying
{
  const { timers, events, streams, src } = setup();
  src.start();
  for (let i = 0; i < SSE_GIVE_UP_AFTER + 1; i++) {
    streams[streams.length - 1]!.fail(true);
    await timers.advance(40_000);
  }
  ok(conns(events).includes("closed"), "closed after repeated failures");
  ok(streams.length > SSE_GIVE_UP_AFTER, "still retrying");
  src.stop();
}

// --- watchdog: silence for SSE_SILENCE_MS reconnects; hb keeps it alive
{
  const { timers, streams, src } = setup();
  src.start();
  streams[0]!.open();
  await timers.advance(SSE_SILENCE_MS - 5000);
  streams[0]!.emit("hb", "");
  await timers.advance(SSE_SILENCE_MS - 5000);
  eq(streams.length, 1, "hb resets the watchdog");
  await timers.advance(10_000);
  ok(streams[0]!.closed, "silent stream is closed by the watchdog");
  await timers.advance(2_000);
  eq(streams.length, 2, "a new stream is opened after silence");
  src.stop();
}

// --- stop closes the stream and cancels retries; restart opens a fresh one
{
  const { timers, streams, src } = setup();
  src.start();
  streams[0]!.open();
  src.stop();
  ok(streams[0]!.closed, "stop closes the stream");
  await timers.advance(120_000);
  eq(streams.length, 1, "nothing reopens after stop");
  src.restart();
  eq(streams.length, 2, "restart opens a new stream");
  src.stop();
}

// --- events of a stale stream are ignored
{
  const { events, streams, src } = setup();
  src.start();
  const old = streams[0]!;
  old.open();
  src.restart();
  const before = events.length;
  old.emit("frame", JSON.stringify({ timestamp: "x", players: [] }));
  eq(events.length, before, "old stream events are ignored");
  src.stop();
}

// --- expired admin ticket: 3 quick failures + past `e` -> ticket_expired
const NOW_MS = 1_800_000_000_000;
const mkTicket = (payload: unknown) =>
  `v1.${Buffer.from(typeof payload === "string" ? payload : JSON.stringify(payload))
    .toString("base64url")}.sig`;
function setupTicket(ticket: string) {
  const timers = fakeTimers();
  const events: SourceEvent[] = [];
  const streams: FakeES[] = [];
  const src = createSseSource({
    server: "zz-7787", ticket, now: () => NOW_MS,
    onEvent: (e) => events.push(e), timers, random: () => 0.5,
    factory: (url) => { const s = new FakeES(url); streams.push(s); return s; },
  });
  return { timers, events, streams, src };
}
async function failN(h: ReturnType<typeof setupTicket>, n: number) {
  for (let i = 0; i < n; i++) {
    h.streams[h.streams.length - 1]!.fail(true);
    await h.timers.advance(5_000);
  }
}
{
  const h = setupTicket(mkTicket({ u: "a", s: "zz-7787", e: NOW_MS / 1000 - 60 }));
  h.src.start();
  await failN(h, 2);
  ok(!conns(h.events).includes("ticket_expired"), "2 failures: not yet ticket_expired");
  await failN(h, 1);
  eq(conns(h.events).pop(), "ticket_expired", "expired ticket + 3 failures -> ticket_expired");
  const n = h.streams.length;
  await h.timers.advance(120_000);
  eq(h.streams.length, n, "no more reconnects after ticket_expired");
  ok(!JSON.stringify(h.events).includes("v1."), "ticket not in events");
  h.src.stop();
}
{
  const h = setupTicket(mkTicket({ u: "a", s: "zz-7787", e: NOW_MS / 1000 + 300 }));
  h.src.start();
  await failN(h, 3);
  ok(!conns(h.events).includes("ticket_expired"), "valid ticket: never ticket_expired");
  eq(conns(h.events).pop(), "connecting", "valid ticket: normal reconnect state");
  h.src.stop();
}
{
  const h = setupTicket(mkTicket({ u: "a", s: "zz-7787", e: NOW_MS / 1000 + 300 }));
  h.src.start();
  h.streams[0]!.open();
  await failN(h, 3);
  eq(conns(h.events).pop(), "reconnecting", "valid ticket after a live period: reconnecting");
  h.src.stop();
}
for (const bad of ["secret-ticket-123", "v1.!!!.sig", mkTicket("not json"), mkTicket({ u: "a" }),
                   mkTicket({ e: "soon" }), "v1..sig"]) {
  const h = setupTicket(bad);
  h.src.start();
  await failN(h, 4);
  ok(!conns(h.events).includes("ticket_expired"), `undecodable ticket keeps reconnecting: ${bad.slice(0, 12)}`);
  h.src.stop();
}

// --- ticket refresh through the parent page (iframe) + alert event
{
  type Msg = { origin?: unknown; source?: unknown; data?: unknown };
  const mkWin = (embedded: boolean) => {
    const posted: { msg: unknown; origin: string }[] = [];
    const handlers: ((ev: Msg) => void)[] = [];
    const parent = { postMessage: (msg: unknown, origin: string) => { posted.push({ msg, origin }); } };
    const win: Record<string, unknown> = {
      location: { origin: "https://zz.test" },
      addEventListener: (_t: string, fn: (ev: Msg) => void) => { handlers.push(fn); },
      removeEventListener: (_t: string, fn: (ev: Msg) => void) => {
        const i = handlers.indexOf(fn); if (i >= 0) handlers.splice(i, 1);
      },
    };
    win.parent = embedded ? parent : win;
    return { win, parent, posted, handlers,
             send: (m: Msg) => { for (const h of [...handlers]) h(m); } };
  };
  const setupRefresh = (embedded: boolean, expSec = NOW_MS / 1000 + 600) => {
    const w = mkWin(embedded);
    const timers = fakeTimers();
    const events: SourceEvent[] = [];
    const streams: FakeES[] = [];
    const alerts: unknown[] = [];
    let clock = NOW_MS;
    const src = createSseSource({
      server: "zz-7787", ticket: mkTicket({ u: "a", s: "zz-7787", e: expSec }),
      now: () => clock, onEvent: (e) => events.push(e), timers, random: () => 0.5,
      onAlert: (p) => alerts.push(p),
      win: w.win as never,
      factory: (url) => { const x = new FakeES(url); streams.push(x); return x; },
    });
    return { w, timers, events, streams, alerts, src, setClock: (v: number) => { clock = v; } };
  };
  async function quickFail(h: { streams: FakeES[]; timers: { advance(ms: number): Promise<void> } }, n: number) {
    for (let i = 0; i < n; i++) {
      h.streams[h.streams.length - 1]!.fail(true);
      await h.timers.advance(2_000);
    }
  }
  const fresh = mkTicket({ u: "a", s: "zz-7787", e: NOW_MS / 1000 + 1200 });

  // proactive request 60 s before expiry; reply reconnects with the new ticket
  {
    const h = setupRefresh(true);
    h.src.start();
    h.streams[0]!.open();
    for (let i = 0; i < 17; i++) { await h.timers.advance(30_000); h.streams[0]!.emit("hb", ""); }
    eq(h.w.posted.length, 0, "no request before expiry-60s");
    h.setClock(NOW_MS + 540_000);
    await h.timers.advance(30_000);
    eq(h.w.posted.length, 1, "one proactive request at expiry-60s");
    eq(h.w.posted[0]!.msg, { type: "zz-ticket-request" }, "request message");
    eq(h.w.posted[0]!.origin, "https://zz.test", "request targets own origin");
    // foreign origin / wrong source are ignored
    h.w.send({ origin: "https://evil.test", source: h.w.parent, data: { type: "zz-ticket", bilet: fresh } });
    h.w.send({ origin: "https://zz.test", source: {}, data: { type: "zz-ticket", bilet: fresh } });
    eq(h.streams.length, 1, "foreign origin / non-parent source ignored");
    h.w.send({ origin: "https://zz.test", source: h.w.parent, data: { type: "zz-ticket", bilet: fresh } });
    eq(h.streams.length, 2, "reconnected after zz-ticket");
    ok(h.streams[1]!.url.endsWith(`bilet=${encodeURIComponent(fresh)}`), "new ticket in the url");
    ok(h.streams[0]!.closed, "old stream closed");
    h.streams[1]!.open();
    eq(conns(h.events), ["connecting", "live", "connecting", "live"], "connecting -> live again");
    await h.timers.advance(60_000);
    eq(h.w.posted.length, 1, "no reply timeout side effects after success");
    h.src.stop();
    eq(h.w.handlers.length, 0, "listener removed on stop");
  }
  // no reply in 15 s after a failure on an expired ticket -> ticket_expired; one pending request
  {
    const h = setupRefresh(true, NOW_MS / 1000 - 60);
    h.src.start();
    await quickFail(h, 3);
    eq(h.w.posted.length, 1, "expired ticket: one request (deduplicated)");
    ok(!conns(h.events).includes("ticket_expired"), "waiting for the parent");
    await h.timers.advance(15_000);
    eq(conns(h.events).pop(), "ticket_expired", "no reply in 15 s -> ticket_expired");
    h.src.stop();
  }
  // zz-ticket-error -> ticket_expired
  {
    const h = setupRefresh(true, NOW_MS / 1000 - 60);
    h.src.start();
    await quickFail(h, 3);
    h.w.send({ origin: "https://zz.test", source: h.w.parent, data: { type: "zz-ticket-error" } });
    eq(conns(h.events).pop(), "ticket_expired", "zz-ticket-error -> ticket_expired");
    h.src.stop();
  }
  // expired ticket, parent answers -> back to live
  {
    const h = setupRefresh(true, NOW_MS / 1000 - 60);
    h.src.start();
    await quickFail(h, 3);
    h.w.send({ origin: "https://zz.test", source: h.w.parent, data: { type: "zz-ticket", bilet: fresh } });
    h.streams[h.streams.length - 1]!.open();
    eq(conns(h.events).pop(), "live", "live after reactive refresh");
    h.src.stop();
  }
  // not an iframe: nothing is posted, old behaviour
  {
    const h = setupRefresh(false, NOW_MS / 1000 - 60);
    h.src.start();
    await quickFail(h, 3);
    eq(h.w.posted.length, 0, "no request when not in an iframe");
    eq(conns(h.events).pop(), "ticket_expired", "not embedded: plain ticket_expired");
    h.src.stop();
  }
  // alert event
  {
    const h = setupRefresh(false);
    h.src.start();
    h.streams[0]!.open();
    h.streams[0]!.emit("alert", JSON.stringify({ kind: "x", n: 1 }));
    h.streams[0]!.emit("alert", "not json");
    eq(h.alerts, [{ kind: "x", n: 1 }], "alert reaches onAlert (bad json dropped)");
    h.src.stop();
  }
}

console.log(`zz live sseSource: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
