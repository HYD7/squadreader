// Zero Zone build: Turkish UI text, in ONE place.
//
// The viewer keeps its English text for the upstream build. Components that
// show user-visible text pick the Turkish string from this dictionary only when
// IS_ZZ is true (see l.ts). Pure module (no DOM, no import.meta.env access that
// can throw) so it runs under the node test runner.
//
// Not translated on purpose: map, weapon, vehicle, faction and role names (game
// data), Squad marker names ("Attack (SL)", "FOB request", ...: in-game
// terminology players already know), player names, and technical codes.
//
// Keys are dotted, grouped by the component that uses them. Values may contain
// {name} placeholders, filled by t(key, params).

export const TR_DICT = {
  // ---- top bar / controls
  "top.replay": "tekrar",
  "top.frame": "kare",
  "top.zoom": "yakınlık",
  "top.fit": "Sığdır",
  "top.fitTitle": "görünümü sıfırla (F)",
  "top.score": "skor",
  "top.scoreTitle": "skor tablosu (Tab)",
  "top.tickets": "Biletler",
  "top.ticketsTitle": "bilet kaybı zaman çizelgesi (G)",
  "clip.label": "Klip",
  "clip.start": "Haritayı WebM olarak kaydet (yalnızca tarayıcıda)",
  "clip.stop": "Kaydı durdur ve indir",

  // ---- settings menu
  "settings.title": "görüntü ayarları",
  "settings.numbers": "Numaralar",
  "settings.layers": "Harita katmanları",
  "layer.players": "Oyuncular",
  "layer.vehicles": "Araçlar",
  "layer.deployables": "FOB / HAB",
  "layer.markers": "İşaretçiler",
  "layer.projectiles": "Mermiler",
  "layer.spawners": "Doğuş noktaları",
  "layer.rallies": "Toplanma noktaları",
  "layer.slNumbers": "Takım lideri numaraları",
  "layer.squadNumbers": "Tüm oyuncu ve araç numaraları",

  // ---- match states (raw game values shown by the team bar / overlay)
  "state.InProgress": "Devam ediyor",
  "state.EnteringMap": "Harita yükleniyor",
  "state.WaitingToStart": "Başlangıç bekleniyor",
  "state.Warmup": "Isınma",
  "state.WaitingPostMatch": "Maç sonu",
  "state.PostMatch": "Maç sonu",
  "state.EndState": "Maç sonu",
  "state.LeavingMap": "Haritadan çıkılıyor",
  "state.Aborted": "İptal edildi",
  "phase.EnteringMap": "Harita yükleniyor",
  "phase.WaitingToStart": "Maça hazırlanılıyor",
  "phase.Warmup": "Isınma",
  "phase.over": "Maç bitti",
  "phase.LeavingMap": "Harita değişiyor",
  "phase.Aborted": "Maç iptal edildi",
  "phase.other": "Maç geçişi",
  "mo.next": "Sıradaki",
  "mo.subPost": "Yeni maç yakında başlıyor…",
  "mo.subPrep": "Oyuncular yakında sahaya iniyor…",
  "mo.winner": "KAZANAN",
  "mo.ticket": "bilet",

  // ---- player search
  "search.placeholder": "Oyuncu ara…",
  "search.clear": "Temizle",
  "search.empty": "Eşleşen oyuncu yok",
  "search.squad": "Squad {n}",
  "search.noSquad": "Squad yok",

  // ---- hover tooltip
  "tip.standing": "ayakta",
  "tip.squadShort": "squad",
  "tip.attached": "bağlı",
  "tip.lastHit": "son vuran: {name}",
  "tip.passengers": "yolcular ({n}):",
  "tip.owner": "sahip",
  "tip.team": "takım",
  "tip.capping": "ele geçiren",
  "tip.cap": "ele geçirme",
  "tip.locked": "kilitli",
  "tip.built": "inşa durumu",
  "tip.ready": "hazır",
  "tip.disabled": "devre dışı",
  "tip.squad": "Squad",
  "tip.placedBy": "Yerleştiren:",
  "tip.placerUnknown": "Yerleştiren: (çevrimdışı / bulunamadı)",
  "tip.projectile": "mermi",
  "tip.dmg": "hasar",
  "tip.impacted": "isabet etti",

  // ---- kill feed
  "kf.title": "Öldürmeler",
  "kf.toggle": "Göster / gizle",
  "kf.clear": "Temizle",
  "kf.empty": "Henüz öldürme yok",
  "kf.wounded": "Yaralandı (yere düştü)",
  "kf.suicide": "İntihar",
  "kf.approx":
    "yaklaşık: bu oyuncunun son görülen silahı, öldüren silah olmayabilir",
  "kf.headshot": "Kafadan vuruş",
  "kf.src.log": "günlük",
  "kf.src.memory": "bellek",
  "kf.src.both": "günlük+bellek",
  "kf.src.title": "Olay kaynağı",
  "kf.hs": "HS",
  "kf.dist": "{n} m",
  "kf.phrase.died": "öldü",
  "kf.phrase.fall": "düşerek öldü",
  "kf.phrase.drown": "boğuldu",
  "kf.phrase.heli": "helikopter kazasında öldü",
  "kf.phrase.runover": "ezildi",
  "kf.phrase.burn": "yanarak öldü",
  "kf.phrase.bleed": "kanamadan öldü",
  "kf.cause.fall": "Düşme hasarı",
  "kf.cause.drown": "Boğulma",
  "kf.cause.heli": "Helikopter çarpması",
  "kf.cause.runover": "Araç çarpması",
  "kf.cause.burn": "Yanma / ateş hasarı",
  "kf.cause.bleed": "Kanamadan öldü (medik zamanında yetişmedi)",
  "kf.dmg.gunfire": "Ateşli silah",
  "kf.dmg.frag": "Şarapnel",
  "kf.dmg.hat": "Tanksavar (HAT)",
  "kf.dmg.tank": "Tank mermisi",
  "kf.dmg.rocket": "Roket",
  "kf.dmg.explosion": "Patlama",
  "kf.dmg.thermite": "Termit",
  "kf.dmg.cookoff": "Mühimmat patlaması",

  // ---- timeline bar / playback controls
  "tl.hint": "Boşluk: oynat/duraklat · F: haritayı sığdır · Tab: skor tablosu",
  "tl.play": "oynat (boşluk)",
  "tl.pause": "duraklat (boşluk)",
  "tl.back": "30 sn geri",
  "tl.forward": "30 sn ileri",
  "tl.backLabel": "⏮ 30sn",
  "tl.forwardLabel": "30sn ⏭",
  "tl.scrub": "zaman çizelgesi (sürükle)",
  "tl.speed": "oynatma hızı",

  // ---- scoreboard
  "sb.title": "Skor Tablosu",
  "sb.hint": "Kapatmak için Tab",
  "sb.close": "kapat (Tab / Esc)",
  "sb.team": "TAKIM {n}",
  "sb.tickets": "Bilet",
  "sb.players": "Oyuncu",
  "sb.empty": "bu takımda oyuncu yok",
  "sb.unassigned": "Atanmamış ({n})",
  "sb.squad": "Squad {n}",
  "sb.lockedAria": "kilitli squad",
  "sb.lockedTitle": "Kilitli: yeni üyelere kapalı",
  "sb.sl": "Takım Lideri (SL)",
  "sb.ftl": "Ateş Timi Lideri (tahmini: kesin bir veri alanı yok)",
  "sb.col.rev": "Canlandırma",
  "sb.col.heal": "İyileştirme puanı",
  "sb.col.wnd": "Yere düşürme: etkisiz hale getirdiğiniz düşmanlar",
  "sb.col.k": "Öldürme",
  "sb.col.d": "Ölüm",
  "sb.col.vk": "Araç imhası",
  "sb.col.obj": "Hedef puanı",
  "sb.col.tw": "Takım çalışması puanı",
  "sb.col.cs": "Muharebe puanı",

  // ---- ticket timeline
  "tt.title": "Bilet Zaman Çizelgesi",
  "tt.vehicle": "araç",
  "tt.note": "toplamlar kesin · nedenler tahmini",
  "tt.noteTitle":
    "Çizgiler ve takım toplamları kesin veridir. Her kaybın ölüm / araç / kanama diye ayrılması en iyi tahmindir.",
  "tt.close": "kapat (G / Esc)",
  "tt.empty": "Bu kayıtta yeterli bilet verisi yok.",
  "tt.team": "Takım {n}",
  "tt.lost": "kayıp",
  "tt.deaths": "Ölümler",
  "tt.vehicles": "Araçlar",
  "tt.bleed": "Kanama",
  "tt.vehLosses": "Araç kayıpları ({n})",
  "tt.noVeh": "İmha edilen araç yok.",
  "tt.loss.vehicle": "{name} imha edildi",
  "tt.loss.death": "ölüm",
  "tt.loss.deaths": "{n} ölüm",
  "tt.loss.bleed": "kanama",

  // ---- player panel
  "pp.lost": "oyuncu kayıp",
  "pp.lostBody":
    "artık kayıtta yok: ayrıldı, takım değiştirdi ya da menzil dışında",
  "pp.close": "kapat",
  "pp.closeEsc": "kapat (esc)",
  "pp.talking": "Konuşuyor: {v}",
  "pp.team": "Takım",
  "pp.squad": "Squad",
  "pp.follow": "TAKİP",
  "pp.followOn": "TAKİP ✓",
  "pp.followTitle": "kamerayı bu oyuncuda tut",
  "pp.sta": "DAYANIKLILIK",
  "pp.row.ping": "PING",
  "pp.row.stance": "DURUŞ",
  "pp.row.status": "DURUM",
  "pp.row.voice": "SES",
  "pp.row.weapon": "SİLAH",
  "pp.row.action": "EYLEM",
  "pp.row.ammo": "MÜHİMMAT",
  "pp.life.down": "yere düştü, canlandırma bekliyor",
  "pp.life.bleeding": "kanıyor",
  "pp.life.healthy": "sağlıklı",
  "pp.voice.local": "Yerel",
  "pp.voice.squad": "Squad",
  "pp.voice.command": "Komuta",
  "pp.voice.toCommander": "Komutana",
  "pp.voice.commandSq": "Komutan → Squad {n}",
  "pp.stance.standing": "ayakta",
  "pp.stance.crouched": "çömelmiş",
  "pp.stance.prone": "yatıyor",
  "pp.badge.cam": "YÖNETİCİ KAMERASI",
  "pp.badge.cmdr": "KOMUTAN",
  "pp.badge.sl": "SL",
  "pp.badge.admin": "YÖNETİCİ",
  "pp.badge.bot": "BOT",
  "pp.badge.squad": "SQUAD",
  "pp.badge.solo": "TEK",
  "pp.badge.moving": "HAREKETLİ",
  "pp.badge.idle": "DURUYOR",
  "pp.badge.down": "YERDE",
  "pp.badge.bleeding": "KANIYOR",
  "pp.stat.kills": "Öldürme",
  "pp.stat.deaths": "Ölüm",
  "pp.stat.teamKills": "Takım öldürme",
  "pp.stat.vehicleKills": "Araç imhası",
  "pp.stat.wounds": "Alınan yara",
  "pp.stat.revive": "Canlandırma puanı",
  "pp.stat.heal": "İyileştirme puanı",
  "pp.stat.objective": "Hedef",
  "pp.stat.teamwork": "Takım çalışması",
  "pp.score": "SKOR",
  "pp.mag.rounds": "{n} mermi",
  "pp.mag.empty": "boş",

  // ---- vehicle panel
  "vp.lost": "araç kayıp",
  "vp.lostBody": "artık kayıtta yok: imha edildi, kayboldu ya da menzil dışında",
  "vp.close": "kapat",
  "vp.closeEsc": "kapat (esc)",
  "vp.followTitle": "bu aracı takip et",
  "vp.team": "Takım",
  "vp.status": "Durum",
  "vp.intact": "Sağlam",
  "vp.destroyed": "İmha edildi",
  "vp.type": "Tür",
  "vp.attached": "başka bir nesneye bağlı / park halinde",
  "vp.lastHit": "son vuran: {name}",
  "vp.resources": "KAYNAKLAR",
  "vp.ammo": "MÜHİMMAT",
  "vp.build": "İNŞA",
  "vp.altAmmo": "mühimmat",
  "vp.altSupply": "inşa malzemesi",
  "vp.components": "BİLEŞENLER",
  "vp.comp.engine": "Motor",
  "vp.comp.ammo": "Mühimmat",
  "vp.comp.rotor": "Rotor",
  "vp.comp.tail": "Kuyruk",
  "vp.comp.trackL": "Sol palet",
  "vp.comp.trackR": "Sağ palet",
  "vp.comp.turret": "Taret",
  "vp.crew": "MÜRETTEBAT VE SİLAHLAR ({o}/{t})",
  "vp.empty": "boş",
  "vp.weapon": "SİLAH",
  "vp.rounds": "mermi",
  "vp.mags": "{n} şarjör",
  "vp.inferred": "koltuklar konuma göre tahmin edildi",
  "vp.seat.driver": "SÜRÜCÜ",
  "vp.seat.gunner": "NİŞANCI",
  "vp.seat.n": "KOLTUK {n}",
  "vp.role.Driver": "SÜRÜCÜ",
  "vp.role.Gunner": "NİŞANCI",
  "vp.role.Passenger": "YOLCU",
  "vp.role.Commander": "KOMUTAN",
  "vp.role.Pilot": "PİLOT",
  "vp.role.Loader": "YÜKLEYİCİ",
  "vp.role.Crew": "MÜRETTEBAT",

  // ---- info panel (markers, FOB, spawners, rallies, zones, projectiles)
  "info.gone": "artık haritada değil",
  "info.goneBody": "bu nesne artık kayıtta yok: kayboldu ya da menzil dışında",
  "info.close": "Kapat (esc)",
  "info.deployable": "Yerleştirilebilir",
  "info.rally": "Toplanma Noktası",
  "info.zone": "Bölge",
  "info.projectile": "Mermi",
  "info.row.team": "TAKIM",
  "info.row.role": "ROL",
  "info.row.placer": "YERLEŞTİREN",
  "info.row.rawType": "HAM TÜR",
  "info.row.pos": "KONUM",
  "info.row.rate": "HIZ",
  "info.row.bleed": "KANAMA",
  "info.row.enemies": "DÜŞMAN",
  "info.row.built": "İNŞA",
  "info.row.class": "SINIF",
  "info.row.vehicle": "ARAÇ",
  "info.row.respawn": "YENİDEN DOĞUŞ",
  "info.row.actor": "AKTÖR",
  "info.row.squad": "SQUAD",
  "info.row.spawns": "DOĞUŞ",
  "info.row.flag": "BAYRAK",
  "info.row.owner": "SAHİP",
  "info.row.capping": "ELE GEÇİREN",
  "info.row.advantage": "AVANTAJ",
  "info.row.kind": "TÜR",
  "info.row.damage": "HASAR",
  "info.row.firer": "ATEŞ EDEN",
  "info.bar.hp": "HP",
  "info.bar.ammo": "MÜHİMMAT",
  "info.bar.build": "İNŞA",
  "info.bar.cap": "ELE GEÇİRME",
  "info.offline": "çevrimdışı / bulunamadı",
  "info.team": "Takım {n}",
  "info.rate": "mühimmat {a}/sn · inşa {b}/sn",
  "info.badge.sieged": "Kuşatıldı",
  "info.badge.spawning": "Doğuş açık",
  "info.badge.bleeding": "Kanıyor",
  "info.badge.overrun": "Ele geçirildi",
  "info.badge.ready": "Hazır",
  "info.badge.overlapped": "Engelli",
  "info.badge.locked": "Kilitli",
  "info.badge.tracer": "İz bırakan",
  "info.badge.impacted": "İsabet etti"
} as const;

export type TrKey = keyof typeof TR_DICT;

/**
 * English strings produced by shared, non-UI helpers (killfeed/diff.ts,
 * state/ticketTimeline.ts, matchState values) → dictionary key. The helpers stay
 * untouched so upstream behaviour and its tests are unchanged; the Zero Zone
 * components translate their OUTPUT with tEn().
 */
export const EN_TO_KEY: Readonly<Record<string, TrKey>> = {
  // deathCausePhrase()
  "died": "kf.phrase.died",
  "fell to death": "kf.phrase.fall",
  "drowned": "kf.phrase.drown",
  "died in a helicopter crash": "kf.phrase.heli",
  "was run over": "kf.phrase.runover",
  "burned to death": "kf.phrase.burn",
  "has bled out": "kf.phrase.bleed",
  // deathCauseFromDamageType().title
  "Fall damage": "kf.cause.fall",
  "Drowning": "kf.cause.drown",
  "Helicopter collision": "kf.cause.heli",
  "Vehicle collision": "kf.cause.runover",
  "Burning / fire damage": "kf.cause.burn",
  "Bled out (no medic in time)": "kf.cause.bleed",
  // damageTypeCategoryLabel()
  "Gunfire": "kf.dmg.gunfire",
  "Frag": "kf.dmg.frag",
  "HAT": "kf.dmg.hat",
  "Tank shell": "kf.dmg.tank",
  "Rocket": "kf.dmg.rocket",
  "Explosion": "kf.dmg.explosion",
  "Thermite": "kf.dmg.thermite",
  "Ammo cook-off": "kf.dmg.cookoff",
};

/** Match states shown raw by the team bar / match overlay. */
export const STATE_KEYS: Readonly<Record<string, TrKey>> = {
  InProgress: "state.InProgress",
  EnteringMap: "state.EnteringMap",
  WaitingToStart: "state.WaitingToStart",
  Warmup: "state.Warmup",
  WaitingPostMatch: "state.WaitingPostMatch",
  PostMatch: "state.PostMatch",
  EndState: "state.EndState",
  LeavingMap: "state.LeavingMap",
  Aborted: "state.Aborted",
};

// ---------------------------------------------------------------------------

/** Vite replaces import.meta.env.DEV statically; under the node test runner
 *  import.meta.env is undefined, the access throws and we fall back to false. */
function detectDev(): boolean {
  try {
    return !!import.meta.env.DEV;
  } catch {
    return false;
  }
}

let devWarnings = detectDev();
const warned = new Set<string>();

/** Test hook: force the development-mode warning on or off. */
export function setDevWarnings(on: boolean): void {
  devWarnings = on;
  warned.clear();
}

export type TrParams = Readonly<Record<string, string | number>>;

/**
 * Turkish text for `key`, with `{name}` placeholders filled from `params`.
 * An unknown key returns the key itself (a visible, greppable marker rather than
 * an empty label) and, in development mode, warns once per key.
 */
export function t(key: TrKey | (string & {}), params?: TrParams): string {
  const raw = Object.prototype.hasOwnProperty.call(TR_DICT, key)
    ? (TR_DICT as Record<string, string>)[key]
    : undefined;
  if (raw === undefined) {
    if (devWarnings && !warned.has(key)) {
      warned.add(key);
      console.warn(`[zz/tr] unknown translation key: ${key}`);
    }
    return key;
  }
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m);
}

/** Translate an English string emitted by a shared helper; unknown → unchanged. */
export function tEn(en: string): string {
  const key = EN_TO_KEY[en];
  return key ? t(key) : en;
}

/** Turkish match-state name; unknown states are shown as the raw game value. */
export function tState(state: string): string {
  const key = STATE_KEYS[state];
  return key ? t(key) : state;
}

/** Turkish label for a ticket-loss event (state/ticketTimeline.ts LossEvent). */
export function tLossLabel(ev: {
  cause: string; label: string; detail?: string;
}): string {
  if (ev.cause === "vehicle") return t("tt.loss.vehicle", { name: ev.detail ?? "?" });
  if (ev.cause === "bleed") return t("tt.loss.bleed");
  if (ev.cause === "death") {
    const m = /^(\d+) deaths$/.exec(ev.label);
    return m ? t("tt.loss.deaths", { n: m[1]! }) : t("tt.loss.death");
  }
  return ev.label;
}
