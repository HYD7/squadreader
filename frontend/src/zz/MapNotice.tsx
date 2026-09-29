// Small Turkish notice for a replay whose map picture is not available: the
// map is unknown to the fallback table, or its texture could not be loaded
// (for example a 404). The viewer keeps working on the plain grid; this only
// tells the viewer why there is no map underneath.
import { useEffect, useState } from "react";
import { mapTexture } from "../canvas/icons";
import { resolveMapLayer } from "../canvas/mapLayer";
import { useViewerStore } from "../state/viewerStore";
import { TR } from "./mode";

export function MapNotice() {
  // Both selectors return primitives, so the component re-renders only when
  // the answer changes, not on every replay frame.
  const hasFrame = useViewerStore((s) => s.curSnap != null);
  const texture = useViewerStore(
    (s) => resolveMapLayer(s.curSnap?.gameState)?.texture ?? null);
  const [failedTexture, setFailedTexture] = useState<string | null>(null);

  useEffect(() => {
    if (!texture) return;
    const img = mapTexture(texture);
    if (img._failed || (img.complete && img.naturalWidth === 0)) {
      setFailedTexture(texture);
      return;
    }
    const onError = () => setFailedTexture(texture);
    img.addEventListener("error", onError);
    return () => img.removeEventListener("error", onError);
  }, [texture]);

  if (!hasFrame) return null;
  if (texture !== null && failedTexture !== texture) return null;
  return <div className="zz-map-notice" role="status">{TR.mapMissing}</div>;
}
