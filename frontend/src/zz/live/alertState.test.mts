import { parseAlert, pushAlert, expireAlerts, dismissAlert, ruleKey, ALERT_TTL_MS } from "./alertState.ts";
let passed = 0, failed = 0;
function ok(c: unknown, m: string) { if (c) passed++; else { failed++; console.error("  FAIL:", m); } }

ok(parseAlert(null, 0, 1) === null && parseAlert({ rule: "x" }, 0, 1) === null, "invalid dropped");
const a = parseAlert({ rule: "teamkill", severity: "high", text: "X", attacker: { name: "A" }, victim: { name: "V" }, at: 1 }, 100, 1)!;
ok(a.severity === "high" && a.attacker === "A" && a.victim === "V" && a.expiresAt === 100 + ALERT_TTL_MS, "parsed");
ok(parseAlert({ rule: "r", severity: "zzz", text: "t" }, 0, 2)!.severity === "warn", "severity default warn");
let l = [] as ReturnType<typeof pushAlert>;
for (let i = 1; i <= 5; i++) l = pushAlert(l, parseAlert({ rule: "r", text: "t" + i }, i * 1000, i)!);
ok(l.length === 3 && l[0]!.id === 5 && l[2]!.id === 3, "max 3, newest first");
ok(expireAlerts(l, 3000 + ALERT_TTL_MS - 1).length === 3, "not yet expired");
ok(expireAlerts(l, 3000 + ALERT_TTL_MS).length === 2, "oldest expired after 15 s");
ok(dismissAlert(l, 4).map((x) => x.id).join() === "5,3", "dismiss");
ok(ruleKey("teamkill") === "alert.rule.teamkill" && ruleKey("q") === "alert.rule.default", "rule key");
console.log(`alertState: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
