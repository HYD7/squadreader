// "Maç bitti" layer state machine: pending -> ready -> countdown -> redirect,
// "Sonraki maçı izle", admin information-only, poll errors, safety of the url.
import {
  createEndMachine, END_POLL_MS, END_COUNTDOWN_SEC, END_MAX_POLLS, type EndState,
} from "./liveEnd.ts";
import { fakeTimers } from "./fakeTimers.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

eq(END_POLL_MS, 5000, "polls every 5 s");
eq(END_COUNTDOWN_SEC, 10, "countdown is 10 s");

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers });
}

function setup(reply: (n: number, url: string) => Response | Promise<Response>,
               redirect = true) {
  const timers = fakeTimers();
  const states: EndState[] = [];
  const urls: string[] = [];
  const navigated: string[] = [];
  let nexts = 0;
  let n = 0;
  const fetchImpl = (async (input: unknown) => {
    urls.push(String(input));
    return reply(++n, String(input));
  }) as typeof fetch;
  const m = createEndMachine({
    server: "zz-7787", redirect, timers, fetchImpl,
    onChange: (s) => states.push(s),
    onNext: () => { nexts++; },
    navigate: (u) => navigated.push(u),
  });
  return { m, timers, states, urls, navigated, nexts: () => nexts, polls: () => n };
}
const last = (s: EndState[]) => s[s.length - 1]!;

// --- pending until ready, polling every 5 s, then countdown and redirect
{
  const { m, timers, states, urls, navigated } = setup((n) =>
    n < 3 ? json({ state: "pending" }) : json({ state: "ready", url: "/maclar/42", replay: true }));
  m.show("tok123");
  await timers.advance(10);
  eq(last(states).phase, "pending", "pending right after show");
  eq(urls[0], "/canli/api/zz-7787/sonuc/tok123", "polls the result endpoint");
  await timers.advance(END_POLL_MS - 100);
  eq(urls.length, 1, "no poll before 5 s");
  await timers.advance(200);
  eq(urls.length, 2, "second poll at 5 s");
  eq(last(states).phase, "pending", "still pending");
  await timers.advance(END_POLL_MS);
  const s = last(states);
  eq(s.phase, "ready", "ready after the third poll");
  eq((s as { url: string }).url, "/maclar/42", "ready carries the url");
  eq((s as { hasReplay: boolean }).hasReplay, true, "ready carries replay flag");
  eq((s as { countdown: number }).countdown, 10, "countdown starts at 10");
  await timers.advance(4000);
  eq((last(states) as { countdown: number }).countdown, 6, "counts down once a second");
  eq(navigated.length, 0, "no redirect during the countdown");
  await timers.advance(6000);
  eq(navigated, ["/maclar/42"], "redirects when the countdown ends");
  const polled = urls.length;
  await timers.advance(30_000);
  eq(urls.length, polled, "no more polling after ready");
}

// --- "Sonraki maçı izle" during the countdown cancels the redirect
{
  const { m, timers, states, navigated, nexts } = setup(() =>
    json({ state: "ready", url: "/maclar/7" }));
  m.show("tok");
  await timers.advance(3000);
  eq(last(states).phase, "ready", "ready");
  m.next();
  eq(last(states).phase, "hidden", "layer hidden after next");
  eq(nexts(), 1, "source restart requested once");
  await timers.advance(30_000);
  eq(navigated.length, 0, "countdown cancelled, no redirect");
}

// --- "Maç sayfasına git" navigates at once
{
  const { m, timers, navigated } = setup(() => json({ state: "ready", url: "/maclar/9" }));
  m.show("tok");
  await timers.advance(50);
  m.goto();
  eq(navigated, ["/maclar/9"], "goto navigates immediately");
  await timers.advance(30_000);
  eq(navigated, ["/maclar/9"], "and only once");
}
{
  const { m, timers, navigated } = setup(() => json({ state: "pending" }));
  m.show("tok");
  await timers.advance(50);
  m.goto();
  eq(navigated.length, 0, "goto while pending does nothing");
  m.destroy();
}

// --- unsafe url from the server is not followed
for (const bad of ["https://evil.example/x", "//evil.example", "javascript:alert(1)", 5, null]) {
  const { m, timers, states, navigated } = setup(() => json({ state: "ready", url: bad }));
  m.show("tok");
  await timers.advance(15_000);
  eq(last(states).phase, "failed", `unsafe url ${JSON.stringify(bad)} -> failed`);
  eq(navigated.length, 0, "not followed");
}

// --- errors keep polling; 429 honours Retry-After
{
  const { m, timers, states, polls } = setup((n) => {
    if (n === 1) throw new TypeError("offline");
    if (n === 2) return new Response("", { status: 500 });
    if (n === 3) return new Response("", { status: 404 });
    if (n === 4) return json({}, 429, { "Retry-After": "12" });
    return json({ state: "ready", url: "/maclar/5" });
  });
  m.show("tok");
  await timers.advance(END_POLL_MS * 3 + 100);
  eq(polls(), 4, "network error, 500 and 404 are retried every 5 s");
  eq(last(states).phase, "pending", "still pending through the errors");
  await timers.advance(11_000);
  eq(polls(), 4, "429: waits for Retry-After");
  await timers.advance(2_000);
  eq(last(states).phase, "ready", "ready after Retry-After");
  m.destroy();
}

// --- gives up after the poll budget
{
  const { m, timers, states, polls } = setup(() => json({ state: "pending" }));
  m.show("tok");
  await timers.advance(END_POLL_MS * (END_MAX_POLLS + 2));
  eq(polls(), END_MAX_POLLS, "poll budget is END_MAX_POLLS");
  eq(last(states).phase, "failed", "failed after the budget");
}

// --- no token: information only, nothing polled
{
  const { m, timers, states, polls } = setup(() => json({ state: "ready", url: "/maclar/1" }));
  m.show(null);
  await timers.advance(20_000);
  eq(last(states), { phase: "info", hasToken: false }, "no token -> info");
  eq(polls(), 0, "nothing polled without a token");
}

// --- admin: information layer only, never navigates or polls
{
  const { m, timers, states, polls, navigated } = setup(
    () => json({ state: "ready", url: "/maclar/1" }), false);
  m.show("tok");
  await timers.advance(30_000);
  eq(last(states), { phase: "info", hasToken: true }, "admin -> info only");
  eq(polls(), 0, "admin never polls");
  eq(navigated.length, 0, "admin never redirects");
  m.hide();
  eq(last(states).phase, "hidden", "admin layer hides on the next match");
}

// --- a second show() replaces the first (stale poll results are ignored)
{
  let release: (r: Response) => void = () => {};
  const { m, timers, states, navigated } = setup((n) =>
    n === 1 ? new Promise<Response>((res) => { release = res; })
            : json({ state: "pending" }));
  m.show("old");
  await timers.advance(10);
  m.show("new");
  release(json({ state: "ready", url: "/maclar/old" }));
  await timers.advance(15_000);
  eq(last(states).phase, "pending", "stale result of the first show is ignored");
  eq(navigated.length, 0, "no redirect from the stale result");
  m.destroy();
}

console.log(`zz live end: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
