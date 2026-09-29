// Which map picture to draw, and where — decided in one place.
//
// The recorder normally attaches a `layer` block (texture + world bounds). It
// can be missing altogether (a community-named scrim layer), or present but
// incomplete: an older recording, or a layer the recorder did not recognise,
// can carry `{ name }` alone. `drawMapTexture` used to look at the fallback
// only when the whole block was absent, so an incomplete block drew nothing.
//
// A layer that is incomplete is completed from the MAP name, using the shipped
// fallback table (whose texture and bounds are a matched pair: the whole-map
// image with the whole-map extent). The pair is taken whole, never mixed with
// a half from the layer — a texture with somebody else's bounds would put the
// picture in the wrong place. No bound is ever made up: when the fallback does
// not know the map either, the result is null and the canvas draws its grid.
import type { GameState } from "../state/types";
import { fallbackMap } from "./mapFallback";
import { coord } from "./worldToScreen";

export interface MapLayer {
  texture: string;
  topLeft: [number, number];
  bottomRight: [number, number];
  /** "layer" = the recording said so; "fallback" = completed from the map name. */
  source: "layer" | "fallback";
}

function finitePair(p: [number, number] | null): [number, number] | null {
  return p && Number.isFinite(p[0]) && Number.isFinite(p[1]) ? p : null;
}

export function resolveMapLayer(
  gs: Pick<GameState, "mapName" | "layer"> | null | undefined,
): MapLayer | null {
  const layer = gs?.layer ?? null;
  const texture = typeof layer?.texture === "string" ? layer.texture.trim() : "";
  const tl = layer ? finitePair(coord(layer.topLeft)) : null;
  const br = layer ? finitePair(coord(layer.bottomRight)) : null;
  // A degenerate box (one corner repeated) has no area to draw into.
  const boxOk = !!tl && !!br && tl[0] !== br[0] && tl[1] !== br[1];
  if (texture && tl && br && boxOk) {
    return { texture, topLeft: tl, bottomRight: br, source: "layer" };
  }

  for (const name of [gs?.mapName, layer?.mapName, layer?.name]) {
    const fb = fallbackMap(name);
    if (fb) {
      return {
        texture: fb.texture,
        topLeft: [fb.topLeft.x, fb.topLeft.y],
        bottomRight: [fb.bottomRight.x, fb.bottomRight.y],
        source: "fallback",
      };
    }
  }
  return null;
}
