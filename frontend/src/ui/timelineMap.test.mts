// Timeline slider mapping: a click, a drag and the arrow keys must each land
// on exactly the frame they name, and the `change` event that follows `input`
// must not seek a second time. Framework-free, like the tests beside it.
//
// The "browser" below follows the HTML range-input rules that caused the bug:
// on a value write the input snaps to the nearest step from `min` and clamps to
// [min, max]; a pointer position maps linearly onto [min, max] and is snapped
// the same way; arrow keys move one step, PageUp/PageDown by a larger step.
import {
  frameIndexAtOrAfter, idxToScrubValue, scrubMax, scrubPct, scrubSeekTarget,
  scrubValueToIdx,
} from "./timelineMap.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}

interface Range { min: number; max: number; step: number; }

function sanitize(v: number, r: Range): number {
  const snapped = r.min + Math.round((v - r.min) / r.step) * r.step;
  return Math.max(r.min, Math.min(r.max, snapped));
}
const rangeFor = (n: number): Range => ({ min: 0, max: scrubMax(n), step: 1 });

// The old slider, kept only to show the bug this replaces is real: 0..100,
// step 0.05, value = the frame's exact percentage. React writes the frame's
// percentage back, the browser snaps it to the 0.05 grid, and the `change`
// that follows seeks again with the snapped value.
{
  const n = 231, times = Array.from({ length: n }, (_, i) => i * 250);
  const dur = times[n - 1]! - times[0]!;
  const r: Range = { min: 0, max: 100, step: 0.05 };
  const seekOld = (pct: number) =>
    frameIndexAtOrAfter((i) => times[i]!, n, times[0]! + (pct / 100) * dur);
  let drifted = 0;
  for (let i = 1; i < n; i++) {
    const exact = (times[i]! - times[0]!) / dur * 100;
    const kept = sanitize(exact, r);
    if (seekOld(kept) !== i) drifted++;
  }
  ok(drifted > 0, `old percent slider really drifts (${drifted} of ${n - 1} frames)`);
}

for (const n of [1, 2, 3, 20, 231, 4000, 86_400]) {
  const r = rangeFor(n);
  ok(r.max === Math.max(0, n - 1), `max is the last frame index (n=${n})`);

  // Every frame: value -> frame -> value is the identity, and the browser has
  // nothing to snap (so the value React writes back is the value it keeps).
  let idempotent = true, roundTrip = true;
  for (let i = 0; i < n; i++) {
    const v = idxToScrubValue(i, n);
    if (v !== i) roundTrip = false;
    if (sanitize(v, r) !== v) idempotent = false;
    if (scrubValueToIdx(String(v), n) !== i) roundTrip = false;
  }
  ok(roundTrip, `frame -> slider value -> frame is exact (n=${n})`);
  ok(idempotent, `the browser does not snap a frame's own value (n=${n})`);

  // Mouse click / drag: the pointer position (any fraction of the track) gives
  // a browser value; the frame shown must be exactly that value.
  let clickOk = true;
  const samples = Math.min(n * 7, 5000);
  for (let k = 0; k <= samples; k++) {
    const frac = k / samples;
    const browserValue = sanitize(r.min + frac * (r.max - r.min), r);
    const idx = scrubValueToIdx(String(browserValue), n);
    if (idx !== browserValue) clickOk = false;
  }
  ok(clickOk, `a click or drag position lands on the frame the browser reports (n=${n})`);

  // `input` then `change` with the same value: exactly one seek, to the right frame.
  let wrong = 0, doubleSeeks = 0;
  for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 300))) {
    let cur = (i + Math.floor(n / 2)) % n;
    const value = String(sanitize(i, r));
    const first = scrubSeekTarget(value, cur, n);          // input
    if (first !== null) cur = first;
    if (cur !== i) wrong++;
    const kept = sanitize(idxToScrubValue(cur, n), r);     // React writes it back
    const second = scrubSeekTarget(String(kept), cur, n);  // change
    if (second !== null) doubleSeeks++;
  }
  ok(wrong === 0, `input seeks to the right frame (n=${n})`);
  ok(doubleSeeks === 0, `the change after input does not seek again (n=${n})`);

  // Keyboard: one arrow press = one frame; Home/End = first/last; PageUp/Down
  // moves by whole frames too.
  if (n > 3) {
    const mid = Math.floor(n / 2);
    const right = scrubValueToIdx(String(sanitize(mid + r.step, r)), n);
    const left = scrubValueToIdx(String(sanitize(mid - r.step, r)), n);
    ok(right === mid + 1, `ArrowRight is the next frame (n=${n})`);
    ok(left === mid - 1, `ArrowLeft is the previous frame (n=${n})`);
    ok(scrubValueToIdx(String(r.min), n) === 0, `Home is frame 0 (n=${n})`);
    ok(scrubValueToIdx(String(r.max), n) === n - 1, `End is the last frame (n=${n})`);
    const page = Math.max(1, Math.round((r.max - r.min) / 10));
    const pg = scrubValueToIdx(String(sanitize(mid + page, r)), n)!;
    ok(Number.isInteger(pg) && pg === Math.min(n - 1, mid + page),
       `PageUp is a whole number of frames (n=${n})`);
  }
}

// The reported case: target frame 19 must not become 20.
{
  const n = 231;
  ok(scrubValueToIdx("19", n) === 19, "frame 19 stays frame 19");
  ok(scrubSeekTarget("19", 5, n) === 19, "one seek to 19 from 5");
  ok(scrubSeekTarget("19", 19, n) === null, "the follow-up change is not a seek");
}

// Bad input never seeks anywhere odd.
{
  ok(scrubValueToIdx("", 10) === null, "empty value is ignored");
  ok(scrubValueToIdx("abc", 10) === null, "non-numeric value is ignored");
  ok(scrubValueToIdx("NaN", 10) === null, "NaN is ignored");
  ok(scrubValueToIdx("Infinity", 10) === null, "Infinity is ignored");
  ok(scrubValueToIdx("-5", 10) === 0, "below range clamps to the first frame");
  ok(scrubValueToIdx("99", 10) === 9, "above range clamps to the last frame");
  ok(scrubValueToIdx(4.4, 10) === 4 && scrubValueToIdx(4.6, 10) === 5,
     "a fractional value rounds to the nearest frame");
  ok(scrubMax(0) === 0 && scrubMax(1) === 0, "no frames / one frame: max 0");
  ok(idxToScrubValue(NaN, 10) === 0, "NaN index shows as 0");
}

// Track fill.
{
  ok(scrubPct(0, 11) === 0, "fill is 0% at the first frame");
  ok(scrubPct(10, 11) === 100, "fill is 100% at the last frame");
  ok(scrubPct(5, 11) === 50, "fill is half way at the middle frame");
  ok(scrubPct(0, 1) === 0, "a single frame does not divide by zero");
}

// +-30 s jumps: first frame at or after the target time.
{
  const times = [0, 1000, 2000, 5000, 5000, 9000];
  const at = (t: number) => frameIndexAtOrAfter((i) => times[i]!, times.length, t);
  ok(at(-100) === 0, "before the start -> first frame");
  ok(at(1000) === 1, "exact hit");
  ok(at(1001) === 2, "between frames -> the later one");
  ok(at(5000) === 3, "duplicate times -> the first of them");
  ok(at(99999) === 5, "past the end -> last frame");
  ok(frameIndexAtOrAfter(() => 0, 0, 5) === 0, "no frames -> 0");
}

console.log(`timelineMap: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
