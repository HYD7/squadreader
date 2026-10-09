// Zero Zone mode: pure helpers (no DOM, no import.meta) so they run under the
// node test runner. The build-time constants live in build.ts.
//
// The Zero Zone build is a viewer served locally under /tekrar/. It receives a
// Zero Zone match id (numeric) in `?mode=replay&id=<id>`, or a live feed in
// `?mode=live&server=<id>` / `?mode=admin-live&server=<id>&bilet=<ticket>`, and
// only talks to same-origin paths.

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

/** Live mode names. `live` = public, delayed, segment polling; `admin-live` =
 *  administrator view over SSE, needs a short-lived ticket (`bilet`). */
export type ZzLiveKind = "live" | "admin-live";

/** Server ids (`zzServerId`) are short ASCII tokens, safe in a URL path. */
const SERVER_RE = /^[A-Za-z0-9._:-]{1,64}$/;

export function isValidServerId(s: string | null | undefined): s is string {
  return typeof s === "string" && SERVER_RE.test(s);
}

/** The ticket is opaque; only its size and character class are checked (no
 *  control characters, no whitespace) before it goes into a query string. */
const TICKET_RE = /^[!-~]{1,2048}$/;

export function isValidTicket(s: string | null | undefined): s is string {
  return typeof s === "string" && TICKET_RE.test(s);
}

export type ZzLocation =
  | { ok: true; id: string }
  | { ok: true; kind: "live"; server: string }
  | { ok: true; kind: "admin-live"; server: string; ticket: string }
  | { ok: false; reason: "no-id" | "bad-id" | "bad-mode" };

export type ZzLiveLocation = Extract<ZzLocation, { kind: ZzLiveKind }>;

export function isLiveLocation(loc: ZzLocation): loc is ZzLiveLocation {
  return loc.ok && "kind" in loc;
}

/**
 * Parse `location.search`.
 *  - replay: `mode` absent or "replay"; `id` required.
 *  - live: `?mode=live&server=<zzServerId>`
 *  - admin-live: `?mode=admin-live&server=<zzServerId>&bilet=<ticket>`
 * A live mode with a missing/invalid server (or a missing ticket) is `bad-mode`.
 */
export function parseZzLocation(search: string): ZzLocation {
  const p = new URLSearchParams(search);
  const mode = p.get("mode");
  if (mode === "live" || mode === "admin-live") {
    const server = p.get("server");
    if (!isValidServerId(server)) return { ok: false, reason: "bad-mode" };
    if (mode === "live") return { ok: true, kind: "live", server };
    const ticket = p.get("bilet");
    if (!isValidTicket(ticket)) return { ok: false, reason: "bad-mode" };
    return { ok: true, kind: "admin-live", server, ticket };
  }
  if (mode !== null && mode !== "replay") return { ok: false, reason: "bad-mode" };
  const id = p.get("id");
  if (id === null || id === "") return { ok: false, reason: "no-id" };
  if (!isValidMatchId(id)) return { ok: false, reason: "bad-id" };
  return { ok: true, id };
}

/** Message sent to the embedding page (ZZ admin panel) when the administrator
 *  selects a player in the admin-live viewer. `alias` = the sanitized player
 *  alias (the frame's eosId field); null = selection cleared. */
export interface ZzPlayerSelectMessage {
  type: "zz-player-select";
  alias: string | null;
  name?: string;
}

/** True only for the admin-live kind inside an embedding parent window.
 *  Replay and public live never notify the parent. */
export function shouldNotifyParent(
  kind: "replay" | "live" | "admin-live" | null | undefined,
  win: { parent?: unknown } | null | undefined,
): boolean {
  return kind === "admin-live" && !!win && !!win.parent && win.parent !== win;
}

export function playerSelectMessage(
  p: { eosId?: string | null; name?: string | null } | null | undefined,
): ZzPlayerSelectMessage | null {
  if (p == null) return { type: "zz-player-select", alias: null };
  if (typeof p.eosId !== "string" || p.eosId === "") return null;
  return { type: "zz-player-select", alias: p.eosId, name: p.name ?? p.eosId };
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
  invalidAddress: "Geçersiz adres",
  invalidAddressSub: "Adres tanınmayan bir kip ya da geçersiz bir sunucu bilgisi içeriyor. Canlı yayını sitedeki bağlantıdan açın.",
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
