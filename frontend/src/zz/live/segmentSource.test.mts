// Public segment source: status parsing, ordered segment fetch, gap / reset /
// end handling, 429 Retry-After, "sınır dolu", back-off. Fake fetch + timers.
import { gzipSync } from "node:zlib";
import {
  createSegmentSource, parseStatus, parseNdjson, parseRetryAfterMs, bodyToText,
  START_BACK_SEGMENTS, SEGMENT_POLL_MS,
} from "./segmentSource.ts";
import {
  statusUrl, segmentUrl, resultUrl, adminStreamUrl, isSafeSitePath, formatDelay,
  type SourceEvent,
} from "./liveUrls.ts";
import { fakeTimers } from "./fakeTimers.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

// --- URL builders: same-origin paths only
eq(statusUrl("zz-7787"), "/canli/api/zz-7787/durum", "status url");
eq(segmentUrl("zz-7787", 42), "/canli/api/zz-7787/parca/42", "segment url");
eq(resultUrl("zz-7787", "ab12"), "/canli/api/zz-7787/sonuc/ab12", "result url");
eq(adminStreamUrl("zz-7787", "t k/n"),
   "/admin/api/squadreader/canli/zz-7787/akis?bilet=t%20k%2Fn", "admin url encodes the ticket");
for (const u of [statusUrl("a"), segmentUrl("a", 1), resultUrl("a", "b"), adminStreamUrl("a", "b")])
  ok(u.startsWith("/") && !u.startsWith("//") && !/^[a-z]+:/i.test(u), `same-origin path ${u}`);
ok(isSafeSitePath("/maclar/42"), "match path is safe");
for (const bad of ["//evil.example/x", "https://evil.example/", "javascript:alert(1)",
                   "maclar/1", "/a/../b", "/a\\b", "", null, 5, "/x y"])
  ok(!isSafeSitePath(bad), `unsafe redirect ${JSON.stringify(bad)}`);
eq(formatDelay(600), "~10 dk", "600 s is ~10 dk");
eq(formatDelay(180), "~3 dk", "180 s is ~3 dk");
eq(formatDelay(45), "~45 sn", "45 s");
eq(formatDelay(null), null, "unknown delay");

// --- parsing helpers
eq(parseStatus(null), null, "null status");
eq(parseStatus("x"), null, "string status");
{
  const st = parseStatus({ serverId: "s", epoch: 3, latestSeq: 9, oldestSeq: 2, delaySec: 600,
                           live: true, ended: false, viewerToken: "vt", full: false });
  eq(st?.latestSeq, 9, "latestSeq"); eq(st?.oldestSeq, 2, "oldestSeq");
  eq(st?.viewerToken, "vt", "viewerToken"); eq(st?.full, false, "full");
  eq(parseStatus({ full: true })?.full, true, "full flag");
}
eq(parseRetryAfterMs("7", 5000), 7000, "Retry-After seconds");
eq(parseRetryAfterMs(null, 5000), 5000, "no Retry-After");
eq(parseRetryAfterMs("Wed, 21 Oct 2026 07:28:00 GMT", 5000), 5000, "date form ignored");
eq(parseRetryAfterMs("0", 5000), 1000, "Retry-After floor is 1 s");
eq(parseRetryAfterMs("99999", 5000), 120000, "Retry-After ceiling");
{
  const r = parseNdjson('{"a":1}\n\nnot json\n{"b":2}\n');
  eq(r.lines, [{ a: 1 }, { b: 2 }], "ndjson lines"); eq(r.bad, 1, "one bad line");
}

// --- fake server
type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;
function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers });
}
function ndjson(lines: unknown[], gz = false): Response {
  const text = lines.map((l) => JSON.stringify(l)).join("\n") + "\n";
  return new Response(gz ? gzipSync(Buffer.from(text)) : Buffer.from(text), { status: 200 });
}
function frame(ms: number, tag?: string): Record<string, unknown> {
  return { timestamp: new Date(ms).toISOString(), players: [], vehicles: [], tag };
}

function setup(handler: Handler, settleMs = 0) {
  const timers = fakeTimers(settleMs);
  const events: SourceEvent[] = [];
  const calls: { url: string; headers: Record<string, string> }[] = [];
  const fetchImpl = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, headers: (init?.headers ?? {}) as Record<string, string> });
    return handler(url, init);
  }) as typeof fetch;
  const src = createSegmentSource({
    server: "zz-7787", onEvent: (e) => events.push(e), fetchImpl, timers,
    random: () => 0.5,
  });
  return { timers, events, calls, src };
}
const lines = (evs: SourceEvent[]) =>
  evs.filter((e) => e.type === "line").map((e) => (e as { obj: unknown }).obj);
const conns = (evs: SourceEvent[]) =>
  evs.filter((e) => e.type === "conn").map((e) => (e as { state: string }).state);
const tOf = (o: unknown) => (o as { t?: string }).t;

// --- starts START_BACK_SEGMENTS behind the newest, reads in order, gunzips
{
  const served: number[] = [];
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      return json({ serverId: "zz-7787", epoch: 1, latestSeq: 10, oldestSeq: 1, delaySec: 600,
                    live: true, ended: false, viewerToken: "vt1" });
    }
    const m = /\/parca\/(\d+)/.exec(url);
    if (m) {
      const seq = Number(m[1]);
      served.push(seq);
      return ndjson([{ t: "seg", seq }, frame(1000 * seq)], true);   // gzip body
    }
    return new Response("nope", { status: 404 });
  }, 25);
  src.start();
  await timers.advance(100);
  eq(served, [10 - START_BACK_SEGMENTS + 1, 9, 10], "starts 3 segments behind, in order");
  eq(conns(events), ["connecting", "live"], "connecting then live");
  const infos = events.filter((e) => e.type === "info");
  eq((infos[0] as unknown as { info: { delaySec: number } }).info.delaySec, 600,
     "info carries delaySec");
  eq(lines(events).length, 6, "every line of every segment is emitted");
  eq(src.stats().segments, 3, "three segments read");
  src.stop();
}

// --- next poll picks up only new segments; viewer token is sent back
{
  let latest = 5;
  const served: number[] = [];
  const { timers, calls, src } = setup((url) => {
    if (url.includes("/durum")) {
      return json({ serverId: "zz-7787", epoch: 1, latestSeq: latest, oldestSeq: 1, delaySec: 600,
                    live: true, ended: false, viewerToken: "tok-abc" });
    }
    const m = /\/parca\/(\d+)/.exec(url);
    if (m) { served.push(Number(m[1])); return ndjson([frame(1000 * Number(m[1]))]); }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(50);
  eq(served, [3, 4, 5], "first cycle");
  latest = 7;
  await timers.advance(SEGMENT_POLL_MS + 1000);
  eq(served, [3, 4, 5, 6, 7], "second cycle fetches only 6 and 7");
  const seg = calls.filter((c) => c.url.includes("/parca/")).pop()!;
  ok(seg.url.includes("v=tok-abc"), "viewer token sent as ?v=");
  eq(seg.headers["X-Viewer-Token"], "tok-abc", "viewer token sent as header");
  ok(calls.every((c) => c.url.startsWith("/canli/api/zz-7787/")), "only same-origin /canli/api paths");
  src.stop();
}

// --- gap: the server buffer moved past us -> gap marker, jump to oldest
{
  let latest = 5, oldest = 1;
  const served: number[] = [];
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      return json({ epoch: 1, latestSeq: latest, oldestSeq: oldest, delaySec: 600, live: true });
    }
    const m = /\/parca\/(\d+)/.exec(url);
    if (m) { served.push(Number(m[1])); return ndjson([frame(1000 * Number(m[1]))]); }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(50);
  latest = 40; oldest = 30;
  await timers.advance(SEGMENT_POLL_MS + 1000);
  ok(lines(events).some((o) => tOf(o) === "gap"), "gap marker emitted");
  ok(served.includes(30), "resumes at the oldest available segment");
  ok(!served.includes(6), "the lost segments are not requested");
  src.stop();
}

// --- segment 404 (aged out) -> gap, recompute from the next status
{
  let fail = true;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) return json({ epoch: 1, latestSeq: 5, oldestSeq: 1, live: true });
    if (fail && url.includes("/parca/4")) { fail = false; return new Response("", { status: 404 }); }
    return ndjson([frame(1000)]);
  });
  src.start();
  await timers.advance(2000);
  ok(lines(events).some((o) => tOf(o) === "gap"), "404 segment is a gap");
  src.stop();
}

// --- epoch change -> reset marker, start over
{
  let epoch = 1;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) return json({ epoch, latestSeq: 8, oldestSeq: 1, live: true });
    return ndjson([frame(1000)]);
  });
  src.start();
  await timers.advance(50);
  epoch = 2;
  await timers.advance(SEGMENT_POLL_MS + 1000);
  const resets = lines(events).filter((o) => tOf(o) === "reset");
  eq(resets.length, 1, "one reset on epoch change");
  eq((resets[0] as { epoch: number }).epoch, 2, "reset carries the new epoch");
  src.stop();
}

// --- end marker: stops polling; restart resumes AFTER the end segment
{
  const served: number[] = [];
  let latest = 6;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) return json({ epoch: 1, latestSeq: latest, oldestSeq: 1, live: true });
    const m = /\/parca\/(\d+)/.exec(url);
    const seq = Number(m![1]);
    served.push(seq);
    if (seq === 5) return ndjson([frame(5000), { t: "end", matchToken: "tok123" }, frame(5500, "after-end")]);
    return ndjson([frame(1000 * seq)]);
  });
  src.start();
  await timers.advance(50);
  const ls = lines(events);
  const endIdx = ls.findIndex((o) => tOf(o) === "end");
  ok(endIdx >= 0, "end marker emitted");
  eq((ls[endIdx] as { matchToken: string }).matchToken, "tok123", "end carries the token");
  ok(!ls.slice(endIdx + 1).some((o) => (o as { tag?: string }).tag === "after-end"),
     "nothing after the end marker is emitted");
  eq(src.stats().ended, true, "source is ended");
  eq(served, [4, 5], "stopped at the end segment");
  await timers.advance(SEGMENT_POLL_MS * 3);
  eq(served, [4, 5], "no polling after the end marker");
  // Next match: restart. The end segment must not be read again.
  latest = 8;
  src.restart();
  await timers.advance(50);
  eq(served, [4, 5, 6, 7, 8], "restart reads only the segments after the end");
  src.stop();
}

// --- 429 honours Retry-After; nothing is fetched in between
{
  let n = 0;
  const { timers, src } = setup((url) => {
    if (url.includes("/durum")) {
      n++;
      if (n === 1) return json({}, 429, { "Retry-After": "20" });
      return json({ epoch: 1, latestSeq: 0, oldestSeq: 0, live: true });
    }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(19_000);
  eq(n, 1, "no retry before Retry-After");
  await timers.advance(2_000);
  eq(n, 2, "retries after Retry-After");
  src.stop();
}

// --- viewer limit (real contract): 503 {error:'full', retryAfterSec} + Retry-After
{
  let full = true;
  let segs = 0;
  const dur: number[] = [];
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      dur.push(timers.now());
      return full
        ? json({ full: true, error: "full", retryAfterSec: 30 }, 503, { "Retry-After": "7" })
        : json({ epoch: 1, latestSeq: 5, oldestSeq: 1, live: true });
    }
    segs++;
    return ndjson([frame(1000)]);
  });
  src.start();
  await timers.advance(100);
  ok(conns(events).includes("full"), "503 full is reported as the full state");
  eq(segs, 0, "no segment fetched while full");
  eq(dur.length, 1, "one status request so far");
  await timers.advance(6_000);
  eq(dur.length, 1, "no retry before Retry-After (7 s)");
  full = false;
  await timers.advance(2_000);
  ok(dur.length >= 2, "retries after Retry-After");
  ok(segs > 0, "recovers once a slot opens");
  eq(conns(events).pop(), "live", "back to live");
  src.stop();
}

// --- viewer limit: no Retry-After header -> body retryAfterSec is used
{
  let n = 0;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      n++;
      return json({ error: "full", retryAfterSec: 12 }, 503);
    }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(11_000);
  eq(n, 1, "waits retryAfterSec from the body");
  await timers.advance(2_000);
  eq(n, 2, "retries after it");
  eq(conns(events).filter((c) => c === "full").length, 1, "state reported once");
  src.stop();
}

// --- 200 {full:true} (old shape) is still understood
{
  const { timers, events, src } = setup((url) =>
    url.includes("/durum") ? json({ full: true, epoch: 1, latestSeq: 5, oldestSeq: 1 })
                           : ndjson([frame(1000)]));
  src.start();
  await timers.advance(100);
  ok(conns(events).includes("full"), "200 with full:true is the full state");
  src.stop();
}

// --- "Şimdi dene": restart() retries at once, even mid-wait
{
  let n = 0;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      n++;
      return n === 1 ? json({ error: "full", retryAfterSec: 60 }, 503)
                     : json({ epoch: 1, latestSeq: 0, oldestSeq: 0, live: true });
    }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(100);
  eq(conns(events).pop(), "full", "waiting on full");
  src.restart();
  await timers.advance(100);
  eq(n, 2, "restart retries without waiting for Retry-After");
  eq(conns(events).pop(), "live", "live after the retry");
  src.stop();
}

// --- 429 ip_limit: same screen, its own state; waits Retry-After
{
  let ipLimited = true;
  let n = 0;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      n++;
      return ipLimited
        ? json({ error: "ip_limit", retryAfterSec: 15 }, 429, { "Retry-After": "15" })
        : json({ epoch: 1, latestSeq: 0, oldestSeq: 0, live: true });
    }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(100);
  eq(conns(events).pop(), "ip_limit", "429 ip_limit is its own state");
  await timers.advance(14_000);
  eq(n, 1, "no retry before Retry-After");
  ipLimited = false;
  await timers.advance(2_000);
  eq(n, 2, "retries after Retry-After");
  eq(conns(events).pop(), "live", "recovers");
  src.stop();
}

// --- a plain 503 (no error body) is an ordinary failure with back-off
{
  const { timers, events, src } = setup(() => new Response("oops", { status: 503 }));
  src.start();
  await timers.advance(100);
  ok(!conns(events).includes("full"), "an unrelated 503 is not 'full'");
  eq(conns(events).pop(), "connecting", "shown as connecting");
  src.stop();
}

// --- a segment answered with 503 full switches state and waits Retry-After
{
  let segs = 0;
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) return json({ epoch: 1, latestSeq: 5, oldestSeq: 5, live: true });
    segs++;
    return json({ error: "full", retryAfterSec: 5 }, 503, { "Retry-After": "5" });
  });
  src.start();
  await timers.advance(100);
  eq(conns(events).pop(), "full", "segment 503 full");
  eq(segs, 1, "one segment attempt");
  await timers.advance(4_000);
  eq(segs, 1, "waits Retry-After (not the 250 ms catch-up)");
  await timers.advance(2_000);
  ok(segs >= 2, "tries again after it");
  src.stop();
}

// --- 404 on status -> closed
{
  const { timers, events, src } = setup(() => new Response("", { status: 404 }));
  src.start();
  await timers.advance(100);
  eq(conns(events), ["connecting", "closed"], "404 status is closed");
  src.stop();
}

// --- network errors after a success -> reconnecting, retried with back-off
{
  let up = true;
  const attempts: number[] = [];
  const { timers, events, src } = setup((url) => {
    if (url.includes("/durum")) {
      attempts.push(timers.now());
      if (up) return json({ epoch: 1, latestSeq: 0, oldestSeq: 0, live: true });
      throw new TypeError("network down");
    }
    return new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(100);
  up = false;
  await timers.advance(SEGMENT_POLL_MS + 1000);
  ok(conns(events).includes("reconnecting"), "network error after success is reconnecting");
  const failTimes = attempts.filter((t) => t > 4000);
  await timers.advance(60_000);
  const later = attempts.filter((t) => t > failTimes[0]!);
  ok(later.length >= 4, "keeps retrying");
  const gaps = later.slice(1).map((t, i) => t - later[i]!);
  ok(gaps[gaps.length - 1]! > gaps[0]!, "back-off grows between attempts");
  ok(Math.max(...gaps) <= 30_000 * 1.2 + 1, "back-off is capped near 30 s");
  src.stop();
}

// --- stop() halts everything: no more requests after it
{
  let n = 0;
  const { timers, src } = setup((url) => {
    n++;
    return url.includes("/durum") ? json({ epoch: 1, latestSeq: 0, oldestSeq: 0, live: true })
                                  : new Response("", { status: 404 });
  });
  src.start();
  await timers.advance(100);
  src.stop();
  const k = n;
  await timers.advance(60_000);
  eq(n, k, "no request after stop()");
}

// --- gzip is decoded even when the browser did not (magic-byte sniff), and a
//     plain body passes through
{
  const gz = new Response(gzipSync(Buffer.from('{"x":1}\n')));
  eq(await bodyToText(gz), '{"x":1}\n', "gzip body is inflated");
  eq(await bodyToText(new Response('{"y":2}\n')), '{"y":2}\n', "plain body passes through");
}

console.log(`zz live segmentSource: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
