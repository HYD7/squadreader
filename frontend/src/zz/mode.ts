// Zero Zone mode: pure helpers (no DOM, no import.meta) so they run under the
// node test runner. The build-time constants live in build.ts.
//
// The Zero Zone build is a replay-only viewer served locally under /tekrar/.
// It receives a Zero Zone match id (numeric) in `?mode=replay&id=<id>` and only
// talks to relative endpoints of its own origin.

/** Vite mode name selected by `npm run build:zerozone`. */
export const ZZ_MODE_NAME = "zerozone";

export function isZeroZoneMode(viteMode: string | undefined | null): boolean {
  return viteMode === ZZ_MODE_NAME;
}

/** Zero Zone match ids are positive integers (autoincrement), no leading zero. */
const ID_RE = /^[1-9][0-9]{0,14}$/;

export function isValidMatchId(id: string | null | undefined): id is string {
  return typeof id === "string" && ID_RE.test(id);
}

export type ZzLocation =
  | { ok: true; id: string }
  | { ok: false; reason: "no-id" | "bad-id" | "bad-mode" };

/** Parse `location.search`. `mode` must be absent or "replay"; `id` required. */
export function parseZzLocation(search: string): ZzLocation {
  const p = new URLSearchParams(search);
  const mode = p.get("mode");
  if (mode !== null && mode !== "replay") return { ok: false, reason: "bad-mode" };
  const id = p.get("id");
  if (id === null || id === "") return { ok: false, reason: "no-id" };
  if (!isValidMatchId(id)) return { ok: false, reason: "bad-id" };
  return { ok: true, id };
}

/** Relative endpoints the viewer may call (resolved against /tekrar/). */
export function metaUrl(id: string): string {
  return `./api/recording/${encodeURIComponent(id)}/meta`;
}
export function recordingUrl(id: string): string {
  return `./api/recording/${encodeURIComponent(id)}`;
}

/** Back / exit button target: absolute site path of the match page. */
export function backUrl(id: string | null | undefined): string {
  return isValidMatchId(id) ? `/maclar/${id}` : "/maclar";
}

/** Turkish messages. */
export const TR = {
  loading: "Tekrar yükleniyor…",
  loadingSub: "Maç kaydı indiriliyor ve hazırlanıyor…",
  notFound: "Kayıt bulunamadı",
  notFoundSub: "Bu maç için izlenebilir bir tekrar yok ya da erişim izniniz yok.",
  failed: "Tekrar yüklenemedi",
  failedSub: "Kayıt alınırken bir sorun oluştu. Bağlantınızı kontrol edip yeniden deneyin.",
  invalidId: "Geçersiz maç kimliği",
  invalidIdSub: "Adres, geçerli bir maç kimliği içermiyor. Tekrarı maç sayfasından açın.",
  retry: "Tekrar dene",
  back: "← Geri",
  backTitle: "Maç sayfasına dön",
  source: "Kaynak kodu",
  frames: "kare",
  mapMissing: "Bu harita için görsel bulunamadı",
} as const;

/** HTTP 404 → "Kayıt bulunamadı"; anything else (incl. network) → "Tekrar yüklenemedi". */
export type LoadErrorKind = "not-found" | "failed";
export function classifyHttpStatus(status: number | null | undefined): LoadErrorKind {
  return status === 404 ? "not-found" : "failed";
}
export function errorText(kind: LoadErrorKind): { title: string; sub: string } {
  return kind === "not-found"
    ? { title: TR.notFound, sub: TR.notFoundSub }
    : { title: TR.failed, sub: TR.failedSub };
}

/**
 * AGPL source link. Comes from VITE_ZZ_SOURCE_URL at build time; anything that
 * is not an absolute http(s) URL is treated as "not configured" (no link).
 */
export function normalizeSourceUrl(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim();
  if (!s) return null;
  try {
    const u = new URL(s);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}
