// Turkish texts of the live viewer (kept out of tr.ts so the shared dictionary
// and its checks stay untouched). Pure module.
import { formatDelay, type ConnState } from "./liveUrls";

export const LIVE_TR = {
  connState: {
    connecting: "Bağlanıyor",
    live: "Canlı",
    reconnecting: "Yeniden bağlanıyor",
    full: "İzleyici sınırı dolu",
    ip_limit: "Bu ağdan çok fazla izleyici",
    closed: "Kapalı",
  } as Record<ConnState, string>,
  badgeLive: "CANLI",
  badgeAdmin: "CANLI · yönetici görünümü",
  connectingTitle: "Canlı yayına bağlanılıyor…",
  connectingSub: "Yayın verisi bekleniyor",
  warmupTitle: "Canlı harita hazırlanıyor",
  warmupSub: "Akıcı oynatma için veri tamponu dolduruluyor",
  reconnectingBanner: "Yeniden bağlanıyor…",
  stalledBanner: "Yayın verisi bekleniyor",
  fullTitle: "İzleyici sınırı dolu",
  fullSub: "Şu anda canlı yayını izleyen kişi sayısı üst sınırda. Birkaç saniye içinde kendiliğinden yeniden denenecek.",
  ipLimitTitle: "Bu ağdan çok fazla izleyici",
  ipLimitSub: "Aynı ağdan izlenebilecek canlı yayın sayısı üst sınırda. Diğer sekmeleri kapatın; birkaç saniye içinde kendiliğinden yeniden denenecek.",
  closedTitle: "Canlı yayın kapalı",
  closedSub: "Bu sunucu için şu anda canlı yayın yok. Yayın açılınca kendiliğinden devam edilir.",
  retryNow: "Şimdi dene",
  matchEnded: "Maç bitti",
  endPending: "Maç kaydı hazırlanıyor…",
  endPendingSub: "Kayıt hazır olunca maç sayfasına yönlendirileceksiniz.",
  endFailed: "Maç kaydına henüz ulaşılamadı",
  endFailedSub: "Kayıt daha sonra maç sayfasında görünecek.",
  endAdminSub: "Sonraki maç başlayınca yayın kendiliğinden devam eder.",
  endNoToken: "Maç sayfasından sonucu ve tekrarı görebilirsiniz.",
  nextMatch: "Sonraki maçı izle",
  gotoMatch: "Maç sayfasına git",
  matchesList: "Maç listesi",
  close: "Kapat",
} as const;

export function badgeText(delaySec: number | null | undefined, admin: boolean): string {
  if (admin) return LIVE_TR.badgeAdmin;
  const d = formatDelay(delaySec);
  return d ? `${LIVE_TR.badgeLive} · ${d} gecikmeli` : LIVE_TR.badgeLive;
}

export function endReadyText(seconds: number): string {
  return `Maç kaydı hazır. ${seconds} sn içinde maç sayfasına yönlendirileceksiniz.`;
}
