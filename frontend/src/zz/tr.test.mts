// Zero Zone Turkish dictionary: every key has a non-empty Turkish value, t()
// behaves on unknown keys, every key referenced by the components exists, and
// the English strings of the shared helpers all have a translation.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  TR_DICT, EN_TO_KEY, STATE_KEYS, t, tEn, tState, tLossLabel, setDevWarnings,
} from "./tr.ts";
import {
  damageTypeCategoryLabel, deathCauseFromDamageType, deathCausePhrase,
} from "../killfeed/diff.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}
function eq(a: unknown, b: unknown, msg: string) {
  ok(JSON.stringify(a) === JSON.stringify(b),
     `${msg} (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`);
}

const dict = TR_DICT as Record<string, string>;
const keys = Object.keys(dict);
ok(keys.length > 100, `dictionary is populated (${keys.length} keys)`);

// --- every key has a non-empty Turkish value
for (const k of keys) {
  const v = dict[k];
  ok(typeof v === "string" && v.trim().length > 0, `non-empty value for ${k}`);
  ok(v !== k, `value differs from key for ${k}`);
  ok(!/\b(TODO|FIXME|XXX)\b/.test(v), `no placeholder text in ${k}`);
}
// Values that contain Turkish letters or are known loan/short forms are fine;
// what must not happen is an English sentence left in: spot-check a few keys.
for (const [k, en] of [
  ["kf.title", "Kills"], ["sb.title", "Scoreboard"], ["tt.title", "Ticket Timeline"],
  ["tl.play", "play (space)"], ["settings.layers", "Map layers"],
  ["search.placeholder", "Search players…"],
] as const) {
  ok(dict[k] !== en, `${k} is translated`);
}

// --- {placeholders} in a value are all named tokens
for (const k of keys) {
  const stray = dict[k]!.replace(/\{\w+\}/g, "").match(/[{}]/);
  ok(!stray, `balanced placeholders in ${k}`);
}

// --- t()
eq(t("kf.title"), dict["kf.title"], "t() returns the dictionary value");
eq(t("search.squad", { n: 3 }), "Squad 3", "t() fills {n}");
eq(t("sb.unassigned", { n: 5 }), "Atanmamış (5)", "t() fills {n} mid-string");
eq(t("vp.crew", { o: 2, t: 4 }), "MÜRETTEBAT VE SİLAHLAR (2/4)", "t() fills several params");
eq(t("tip.lastHit", {}), "son vuran: {name}", "missing param leaves the placeholder");

const warnings: string[] = [];
const realWarn = console.warn;
console.warn = (...a: unknown[]) => { warnings.push(a.join(" ")); };
try {
  setDevWarnings(true);
  eq(t("no.such.key"), "no.such.key", "unknown key returns the key");
  eq(t("no.such.key"), "no.such.key", "unknown key again returns the key");
  eq(warnings.length, 1, "dev mode warns once per unknown key");
  ok(/no\.such\.key/.test(warnings[0] ?? ""), "the warning names the key");
  eq(t("toString"), "toString", "prototype names are not dictionary keys");
  eq(warnings.length, 2, "another unknown key warns");
  eq(t("kf.title"), dict["kf.title"], "known key does not warn");
  eq(warnings.length, 2, "no warning for a known key");
  setDevWarnings(false);
  eq(t("other.unknown.key"), "other.unknown.key", "unknown key outside dev mode");
  eq(warnings.length, 2, "no warning outside dev mode");
} finally {
  console.warn = realWarn;
}

// --- helpers for shared English strings
for (const [en, key] of Object.entries(EN_TO_KEY)) {
  ok(key in dict, `EN_TO_KEY target exists: ${en} -> ${key}`);
  ok(tEn(en) !== en, `tEn translates "${en}"`);
}
eq(tEn("something new"), "something new", "tEn passes unknown text through");
for (const [state, key] of Object.entries(STATE_KEYS)) {
  ok(key in dict, `STATE_KEYS target exists: ${state}`);
  eq(tState(state), dict[key], `tState(${state})`);
}
eq(tState("SomeFutureState"), "SomeFutureState", "unknown match state shown raw");

// The English strings emitted by the kill-feed helpers are all covered.
const damageTypes = [
  "DamageType_Fall", "FallingDamage", "Underwater", "Drowning", "HelicopterCollision",
  "VehicleCollision", "BurningDamage", "Wounded", "SmallArms", "Fragmentation", "HAT",
  "Kinetic", "ExplosiveRocket", "Explosives", "Thermite", "AmmoBox", "Mystery",
];
for (const dt of damageTypes) {
  const phrase = deathCausePhrase(dt);
  ok(tEn(phrase) !== phrase, `death phrase "${phrase}" (${dt}) has a translation`);
  const cause = deathCauseFromDamageType(dt);
  if (cause) ok(tEn(cause.title) !== cause.title, `cause title "${cause.title}" translated`);
  const cat = damageTypeCategoryLabel(dt);
  if (cat) ok(tEn(cat) !== cat, `damage category "${cat}" translated`);
}

// --- ticket-loss labels
eq(tLossLabel({ cause: "vehicle", label: "BTR-80 destroyed", detail: "BTR-80" }),
   "BTR-80 imha edildi", "vehicle loss label");
eq(tLossLabel({ cause: "death", label: "death" }), "ölüm", "single death label");
eq(tLossLabel({ cause: "death", label: "4 deaths" }), "4 ölüm", "several deaths label");
eq(tLossLabel({ cause: "bleed", label: "bleed" }), "kanama", "bleed label");
eq(tLossLabel({ cause: "other", label: "x" }), "x", "unknown cause keeps the label");

// --- every literal key used by a component exists in the dictionary
function findSrc(): string {
  for (const c of [join(process.cwd(), "src"), join(process.cwd(), "frontend", "src")]) {
    if (existsSync(join(c, "zz", "tr.ts"))) return c;
  }
  throw new Error("cannot locate frontend/src from " + process.cwd());
}
const src = findSrc();
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...walk(p));
    else if (/\.(tsx?)$/.test(e.name) && !/\.test\.mts$/.test(e.name)) out.push(p);
  }
  return out;
}
let used = 0;
for (const file of walk(src)) {
  if (file.endsWith("tr.ts")) continue;
  const text = readFileSync(file, "utf8");
  for (const m of text.matchAll(/\b(?:L|t)\(\s*"([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+)"/g)) {
    used++;
    ok(m[1]! in dict, `${file.slice(src.length + 1)}: unknown key ${m[1]}`);
  }
}
ok(used > 100, `found the components' key references (${used})`);

// Dynamic keys: layer.<LayerKey>, sb.col.<col>, vp.role.<Role>, pp.stance.<s>.
const store = readFileSync(join(src, "state", "viewerStore.ts"), "utf8");
const labels = /LAYER_LABELS: Record<LayerKey, string> = \{([\s\S]*?)\};/.exec(store);
ok(!!labels, "found LAYER_LABELS");
const layerKeys = [...(labels?.[1] ?? "").matchAll(/^\s*(\w+):/gm)].map((m) => m[1]!);
ok(layerKeys.length >= 9, `found layer keys (${layerKeys.length})`);
for (const k of layerKeys) ok(`layer.${k}` in dict, `layer.${k} translated`);
for (const c of ["rev", "heal", "wnd", "k", "d", "vk", "obj", "tw", "cs"]) {
  ok(`sb.col.${c}` in dict, `sb.col.${c} translated`);
}
for (const r of ["Driver", "Gunner", "Passenger", "Commander", "Pilot", "Loader", "Crew"]) {
  ok(`vp.role.${r}` in dict, `vp.role.${r} translated`);
}
for (const s of ["standing", "crouched", "prone"]) ok(`pp.stance.${s}` in dict, `pp.stance.${s}`);

console.log(`zz tr: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
