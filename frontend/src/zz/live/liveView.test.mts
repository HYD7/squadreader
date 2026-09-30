// Live viewer chrome: Turkish texts of the badge, the end-of-match layer and the
// connection layers (static render, no DOM needed).
import { createElement } from "react";
// The browser build of the server renderer needs no Node built-ins, so it
// bundles under the test runner (which has no `platform` externals); it ships
// without type declarations.
// @ts-ignore TS7016
import { renderToStaticMarkup } from "react-dom/server.browser";
import { LiveEndOverlay } from "./LiveEndOverlay.tsx";
import { LiveBadge } from "./LiveStatus.tsx";
import { LIVE_TR, badgeText, endReadyText } from "./liveText.ts";
import type { EndState } from "./liveEnd.ts";
import { navigateTop } from "./liveNavigate.ts";
import { createPhaseTracker, PHASE_HOLD_MS } from "./livePhase.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

const noop = () => {};
function end(state: EndState, admin = false): string {
  return renderToStaticMarkup(createElement(LiveEndOverlay, {
    state, admin, onNext: noop, onGoto: noop, onClose: noop,
  }));
}

// --- badge
eq(badgeText(600, false), "CANLI · ~10 dk gecikmeli", "badge text for a 10 minute delay");
eq(badgeText(null, false), "CANLI", "badge without a delay");
eq(badgeText(null, true), "CANLI · yönetici görünümü", "admin badge");
{
  const html = renderToStaticMarkup(createElement(LiveBadge,
    { conn: "live", delaySec: 600, admin: false }));
  ok(html.includes("CANLI · ~10 dk gecikmeli"), "badge shows the delay");
  ok(html.includes(">Canlı<"), "badge shows the connection state");
  ok(html.includes('data-conn="live"'), "badge exposes the state");
}
for (const [state, text] of [
  ["connecting", "Bağlanıyor"], ["live", "Canlı"], ["reconnecting", "Yeniden bağlanıyor"],
  ["full", "İzleyici sınırı dolu"], ["ip_limit", "Bu ağdan çok fazla izleyici"],
  ["closed", "Kapalı"],
] as const) {
  eq(LIVE_TR.connState[state], text, `connection text ${state}`);
  const html = renderToStaticMarkup(createElement(LiveBadge,
    { conn: state, delaySec: 600, admin: false }));
  ok(html.includes(text), `badge renders ${state}`);
}

// --- end layer
eq(end({ phase: "hidden" }), "", "hidden renders nothing");
{
  const html = end({ phase: "pending" });
  ok(html.includes("Maç bitti"), "pending: title");
  ok(html.includes("Maç kaydı hazırlanıyor…"), "pending: record being prepared");
  ok(html.includes("Sonraki maçı izle"), "pending: next match button");
  ok(!html.includes("Maç sayfasına git"), "pending: no match page button yet");
}
{
  const html = end({ phase: "ready", url: "/maclar/42", hasReplay: true, countdown: 7 });
  ok(html.includes(endReadyText(7)) && html.includes("7 sn"), "ready: countdown text");
  ok(html.includes("Sonraki maçı izle") && html.includes("Maç sayfasına git"), "ready: both buttons");
  ok(!html.includes("/maclar/42"), "ready: the url is not rendered into the markup");
}
{
  const html = end({ phase: "failed" });
  ok(html.includes("Maç kaydına henüz ulaşılamadı"), "failed: text");
  ok(html.includes('href="/maclar"'), "failed: link to the match list");
  ok(!html.includes("http"), "failed: no external link");
  ok(html.includes('target="_top"'), "failed: the list link leaves the iframe");
}
{
  const html = end({ phase: "info", hasToken: false });
  ok(html.includes("Sonraki maçı izle"), "info (public, no token): next match");
}
{
  const html = end({ phase: "info", hasToken: false }, true);
  ok(html.includes("Maç bitti"), "admin info: title");
  ok(html.includes("Sonraki maç başlayınca yayın kendiliğinden devam eder."), "admin info: text");
  ok(html.includes("Kapat"), "admin info: close button");
  ok(!html.includes("Sonraki maçı izle") && !html.includes("Maç sayfasına git")
     && !html.includes("href"), "admin: information only, no navigation");
}

// --- navigation goes to the top window (the viewer lives in an iframe)
{
  const seen: string[] = [];
  const top = { location: { assign: (u: string) => { seen.push("top:" + u); } } };
  const frameWin = { location: { assign: (u: string) => { seen.push("frame:" + u); } }, top };
  navigateTop("/maclar/5", frameWin);
  eq(seen, ["top:/maclar/5"], "redirect replaces the top window");

  const seen2: string[] = [];
  const alone = { location: { assign: (u: string) => { seen2.push(u); } }, top: null };
  navigateTop("/maclar/6", alone);
  eq(seen2, ["/maclar/6"], "no top: own window");

  const seen3: string[] = [];
  const blocked = {
    location: { assign: (u: string) => { seen3.push(u); } },
    get top(): never { throw new Error("SecurityError"); },
  };
  navigateTop("/maclar/7", blocked);
  eq(seen3, ["/maclar/7"], "top access throws: falls back to own window");

  const seen4: string[] = [];
  const badTop = { location: { assign: () => { throw new Error("SecurityError"); } } };
  navigateTop("/maclar/8", { location: { assign: (u: string) => { seen4.push(u); } }, top: badTop });
  eq(seen4, ["/maclar/8"], "top assign throws: falls back to own window");
}

// --- loading layer hysteresis (livePhase)
{
  const D = 6_000;
  const tr = createPhaseTracker();
  const at = (now: number, first: number, last: number, count = 3) =>
    tr.evaluate({ count, first, last, now, renderDelayMs: D });
  eq(at(1000, 0, 0, 0).phase, "connecting", "no frames: connecting");
  eq(at(3000, 2000, 3000).phase, "warmup", "renderClock before the oldest frame: warmup");
  eq(at(10_000, 2000, 9000).phase, "running", "renderClock inside the buffer: running");
  eq(at(16_000, 15_000, 19_000).phase, "running", "warmup-like hole after a draw is hidden");
  eq(at(21_000, 15_000, 26_000).phase, "running", "data is back");
  eq(at(28_000, 15_000, 20_000).phase, "running", "stall-like 7 s hole is hidden");
  eq(at(21_000 + PHASE_HOLD_MS, 15_000, 20_000).phase, "stalled", "outage >= 10 s: banner");
  ok(at(45_000, 20_000, 26_000).phase !== "warmup", "never the big warmup layer after a draw");
  eq(at(50_000, 0, 0, 0).phase, "connecting", "reset: connecting again");
  eq(at(51_000, 50_500, 51_000).phase, "warmup", "then warmup again");
}

// --- every live text is Turkish-looking and free of URLs
for (const [k, v] of Object.entries(LIVE_TR)) {
  if (typeof v === "string") {
    ok(v.trim().length > 0, `non-empty ${k}`);
    ok(!/https?:/.test(v), `no url in ${k}`);
  }
}

console.log(`zz live view: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
