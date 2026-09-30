// Zero Zone mode helpers: mode detection, ?mode=replay&id= parsing, endpoint and
// back URLs, error classification, source-link validation.
import {
  isZeroZoneMode, isValidMatchId, parseZzLocation, metaUrl, recordingUrl,
  backUrl, classifyHttpStatus, errorText, normalizeSourceUrl, TR,
  isValidServerId, isValidTicket, isLiveLocation,
} from "./mode.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

// --- mode detection
ok(isZeroZoneMode("zerozone"), "zerozone mode detected");
ok(!isZeroZoneMode("production"), "production is not zerozone");
ok(!isZeroZoneMode("apple"), "apple is not zerozone");
ok(!isZeroZoneMode(undefined) && !isZeroZoneMode(null) && !isZeroZoneMode(""),
   "missing mode is not zerozone");

// --- id validation
for (const good of ["1", "42", "123456", "999999999999999"])
  ok(isValidMatchId(good), `valid id ${good}`);
for (const bad of ["", "0", "007", "-1", "1.5", "abc", "12a", " 12", "12 ",
                   "1e3", "../x", "1/2", "1234567890123456", "١٢٣"])
  ok(!isValidMatchId(bad), `invalid id ${JSON.stringify(bad)}`);
ok(!isValidMatchId(null) && !isValidMatchId(undefined), "null/undefined id invalid");

// --- location parsing
eq(parseZzLocation("?mode=replay&id=42"), { ok: true, id: "42" }, "mode=replay&id");
eq(parseZzLocation("?id=42"), { ok: true, id: "42" }, "id without mode");
eq(parseZzLocation("?id=42&mode=replay&x=1"), { ok: true, id: "42" }, "extra params ignored");
eq(parseZzLocation(""), { ok: false, reason: "no-id" }, "empty search");
eq(parseZzLocation("?mode=replay"), { ok: false, reason: "no-id" }, "no id");
eq(parseZzLocation("?mode=replay&id="), { ok: false, reason: "no-id" }, "empty id");
eq(parseZzLocation("?mode=replay&id=abc"), { ok: false, reason: "bad-id" }, "non numeric id");
eq(parseZzLocation("?mode=replay&id=0"), { ok: false, reason: "bad-id" }, "zero id");
eq(parseZzLocation("?mode=replay&id=-5"), { ok: false, reason: "bad-id" }, "negative id");
eq(parseZzLocation("?mode=replay&id=12%2F34"), { ok: false, reason: "bad-id" }, "encoded slash");
eq(parseZzLocation("?mode=live&id=42"), { ok: false, reason: "bad-mode" }, "live mode refused");
eq(parseZzLocation("?mode=home&id=42"), { ok: false, reason: "bad-mode" }, "home mode refused");

// --- live modes
eq(parseZzLocation("?mode=live&server=zz-7787"),
   { ok: true, kind: "live", server: "zz-7787" }, "mode=live&server");
eq(parseZzLocation("?server=zz-7787&mode=live&x=1"),
   { ok: true, kind: "live", server: "zz-7787" }, "live: order and extra params ignored");
eq(parseZzLocation("?mode=live&server=a.b_c:1-2"),
   { ok: true, kind: "live", server: "a.b_c:1-2" }, "live: dot, underscore, colon, dash allowed");
eq(parseZzLocation("?mode=admin-live&server=zz-7787&bilet=abc.DEF-123"),
   { ok: true, kind: "admin-live", server: "zz-7787", ticket: "abc.DEF-123" },
   "mode=admin-live&server&bilet");
eq(parseZzLocation("?mode=admin-live&server=zz-7787&bilet=a%2Fb%3D"),
   { ok: true, kind: "admin-live", server: "zz-7787", ticket: "a/b=" },
   "admin-live: ticket is decoded");
for (const q of [
  "?mode=live", "?mode=live&server=", "?mode=live&server=a%2Fb", "?mode=live&server=a%20b",
  "?mode=live&server=..%2Fx", "?mode=live&server=" + "x".repeat(65),
  "?mode=live&server=a%3Cb", "?mode=live&server=a%2Bb", "?mode=live&id=42",
  "?mode=admin-live&server=bad%2Fone&bilet=t", "?mode=admin-live&server=zz-7787",
  "?mode=admin-live&server=zz-7787&bilet=", "?mode=admin-live&bilet=t",
  "?mode=admin-live&server=zz-7787&bilet=a%20b",
  "?mode=admin-live&server=zz-7787&bilet=" + "t".repeat(2049),
  "?mode=Live&server=zz-7787", "?mode=admin&server=zz-7787",
]) eq(parseZzLocation(q), { ok: false, reason: "bad-mode" }, `refused: ${q.slice(0, 60)}`);
eq(parseZzLocation("?mode=live&server=" + "x".repeat(64)),
   { ok: true, kind: "live", server: "x".repeat(64) }, "64-char server accepted");
ok(isValidServerId("zz-7787") && !isValidServerId("") && !isValidServerId(null)
   && !isValidServerId("a b"), "isValidServerId");
ok(isValidTicket("abc123") && !isValidTicket("") && !isValidTicket(undefined)
   && !isValidTicket("a b"), "isValidTicket");
ok(isLiveLocation(parseZzLocation("?mode=live&server=s"))
   && !isLiveLocation(parseZzLocation("?id=4"))
   && !isLiveLocation(parseZzLocation("?mode=live")), "isLiveLocation");
// replay locations keep their exact shape
eq(parseZzLocation("?mode=replay&id=42"), { ok: true, id: "42" }, "replay shape unchanged");
eq(TR.invalidAddress, "Geçersiz adres", "invalid address text");

// --- URLs
eq(metaUrl("42"), "./api/recording/42/meta", "meta url");
eq(recordingUrl("42"), "./api/recording/42", "recording url");
ok(metaUrl("42").startsWith("./") && recordingUrl("42").startsWith("./"),
   "endpoints are relative");
eq(backUrl("42"), "/maclar/42", "back url");
eq(backUrl(null), "/maclar", "back url without id");
eq(backUrl("x/../y"), "/maclar", "back url refuses a bad id");

// --- error classification and Turkish text
eq(classifyHttpStatus(404), "not-found", "404 is not-found");
for (const s of [400, 401, 403, 410, 500, 502, 503, 0])
  eq(classifyHttpStatus(s), "failed", `status ${s} is failed`);
eq(classifyHttpStatus(null), "failed", "network error is failed");
eq(classifyHttpStatus(undefined), "failed", "undefined status is failed");
eq(errorText("not-found").title, "Kayıt bulunamadı", "404 title");
eq(errorText("failed").title, "Tekrar yüklenemedi", "generic title");
eq(TR.loading, "Tekrar yükleniyor…", "loading text");
eq(TR.source, "Kaynak kodu", "source label");

// --- source link (VITE_ZZ_SOURCE_URL)
eq(normalizeSourceUrl(undefined), null, "unset -> no link");
eq(normalizeSourceUrl(""), null, "empty -> no link");
eq(normalizeSourceUrl("   "), null, "blank -> no link");
eq(normalizeSourceUrl("javascript:alert(1)"), null, "javascript: refused");
eq(normalizeSourceUrl("data:text/html,x"), null, "data: refused");
eq(normalizeSourceUrl("not a url"), null, "garbage refused");
eq(normalizeSourceUrl("/relative"), null, "relative refused");
eq(normalizeSourceUrl("https://example.org/repo"), "https://example.org/repo",
   "https accepted");
eq(normalizeSourceUrl(" https://example.org/repo "), "https://example.org/repo",
   "trimmed");
eq(normalizeSourceUrl(42), null, "non-string -> no link");

console.log(`zz mode: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
