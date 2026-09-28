// Preflight for the Zero Zone viewer: asks the (tiny) meta endpoint whether the
// recording is available, so an HTTP 404 can be told apart from any other
// failure ("Kayıt bulunamadı" vs "Tekrar yüklenemedi"). The shared loader only
// records "failed", not why. Same-origin relative URL only.
import { useCallback, useEffect, useState } from "react";
import { classifyHttpStatus, metaUrl, type LoadErrorKind } from "./mode";

export type ZzGate =
  | { state: "checking" }
  | { state: "ready" }
  | { state: "error"; kind: LoadErrorKind };

export function useZzGate(id: string): { gate: ZzGate; retry: () => void } {
  const [gate, setGate] = useState<ZzGate>({ state: "checking" });
  const [nonce, setNonce] = useState(0);
  const retry = useCallback(() => {
    setGate({ state: "checking" });
    setNonce((n) => n + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(metaUrl(id), { credentials: "same-origin" })
      .then((r) => {
        if (cancelled) return;
        setGate(r.ok ? { state: "ready" }
                     : { state: "error", kind: classifyHttpStatus(r.status) });
      })
      .catch(() => {
        if (!cancelled) setGate({ state: "error", kind: "failed" });
      });
    return () => { cancelled = true; };
  }, [id, nonce]);

  return { gate, retry };
}
