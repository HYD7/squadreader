// resolveMapLayer: a `layer` block that is present but incomplete is completed
// from the map name; nothing is invented when the map is not known.
import { resolveMapLayer } from "./mapLayer.ts";
import { fallbackMap } from "./mapFallback.ts";

let passed = 0, failed = 0;
function ok(cond: unknown, msg: string) {
  if (cond) { passed++; } else { failed++; console.error("  FAIL:", msg); }
}

const fall = fallbackMap("Fallujah")!;
const fallTl = [fall.topLeft.x, fall.topLeft.y];
const fallBr = [fall.bottomRight.x, fall.bottomRight.y];

// A complete layer is used as the recording gave it.
{
  const r = resolveMapLayer({
    mapName: "Fallujah",
    layer: { texture: "Custom", topLeft: [-10, -20], bottomRight: { x: 30, y: 40 } },
  });
  ok(r?.source === "layer", "complete layer is used as is");
  ok(r?.texture === "Custom", "layer texture kept");
  ok(r?.topLeft[0] === -10 && r?.bottomRight[1] === 40, "both corner shapes read");
}

// The reported bug: layer object exists but has no texture / bounds.
{
  const r = resolveMapLayer({ mapName: "Fallujah", layer: { name: "Fallujah_RAAS_v1" } });
  ok(r?.source === "fallback", "name-only layer is completed from the map name");
  ok(r?.texture === fall.texture, "texture comes from the fallback");
  ok(JSON.stringify(r?.topLeft) === JSON.stringify(fallTl), "top-left from the fallback");
  ok(JSON.stringify(r?.bottomRight) === JSON.stringify(fallBr), "bottom-right from the fallback");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah", layer: { texture: "Fallujah" } });
  ok(r?.source === "fallback" && r.texture === "Fallujah", "texture without bounds: bounds from the fallback");
  ok(JSON.stringify(r?.topLeft) === JSON.stringify(fallTl), "bounds are the fallback's own");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah", layer: { topLeft: [0, 0], bottomRight: [5, 5] } });
  ok(r?.source === "fallback", "bounds without texture: the matched fallback pair is used");
  ok(r?.bottomRight[0] === fall.bottomRight.x,
     "a fallback texture is never paired with the layer's own bounds");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah", layer: { texture: "X", topLeft: [0, 0] } });
  ok(r?.source === "fallback", "one corner missing counts as missing bounds");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah",
    layer: { texture: "X", topLeft: [0, 0], bottomRight: [0, 0] } });
  ok(r?.source === "fallback", "degenerate (zero-area) bounds count as missing");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah",
    layer: { texture: "X", topLeft: [NaN, 0], bottomRight: [1, 1] } });
  ok(r?.source === "fallback", "non-finite bounds count as missing");
}
{
  const r = resolveMapLayer({ mapName: "Fallujah", layer: { texture: "   " } });
  ok(r?.source === "fallback", "blank texture counts as missing");
}

// No layer at all: as before.
{
  ok(resolveMapLayer({ mapName: "Fallujah", layer: null })?.texture === "Fallujah", "null layer -> fallback");
  ok(resolveMapLayer({ mapName: "FCL Narva" })?.texture === "Narva", "no layer -> fallback by map name");
}

// Nothing invented for an unknown map.
{
  ok(resolveMapLayer({ mapName: "OCBT Shchyhliivka AAS v1", layer: { name: "x" } }) === null,
     "unknown map, incomplete layer -> nothing");
  ok(resolveMapLayer({ mapName: "OCBT Shchyhliivka AAS v1", layer: { texture: "X" } }) === null,
     "unknown map, texture but no bounds -> nothing (no made-up bounds)");
  ok(resolveMapLayer({ mapName: null, layer: null }) === null, "no name, no layer -> nothing");
  ok(resolveMapLayer(null) === null, "no game state -> nothing");
  ok(resolveMapLayer(undefined) === null, "undefined game state -> nothing");
}

// Map name missing but the layer names the map.
{
  const r = resolveMapLayer({ mapName: null, layer: { mapName: "Narva" } });
  ok(r?.texture === "Narva", "layer.mapName is a second source of the map name");
}

console.log(`mapLayer: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
