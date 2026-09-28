#!/usr/bin/env bash
# yedek-geri-al.sh - SquadReader (Zero Zone dağıtımı) yedek ve geri alma.
#
# Kullanım:
#   yedek-geri-al.sh yedek   <port> <hedef-dizin> [--token-yedekle]
#   yedek-geri-al.sh geri-al <port> <yedek-dizini> [--dry-run]
#
# Örnek:
#   sudo /opt/sqreader/src/deploy/zerozone/yedek-geri-al.sh yedek 7787 /var/backups/sqreader
#   sudo /opt/sqreader/src/deploy/zerozone/yedek-geri-al.sh geri-al 7787 /var/backups/sqreader/sqreader-7787-20260901T180000Z --dry-run
#
# YEDEK:
#  * stats DB: `sqlite3 .backup` (çalışan DB'nin TUTARLI kopyası; düz cp DEĞİL).
#  * yalnız FİNALİZE kayıtlar (.sqrx + .meta.json; sidecar'da recordingState=finalized).
#    Aktif (yazılmakta olan) ve doğrulanmamış kayıt kopyalanmaz.
#  * export_state.json (kayıt anahtarı + Zero Zone onayları), export_audit.log
#    (denetim günlüğü), retention_state.json: VARSA (sqreader/export_state.py;
#    paralel görevle geliyor, adlar SON SÜRÜMDE doğrulanmalı). Yerleri: config
#    export_state_dir, yoksa kayıt dizininin üst dizini (veri dizini).
#  * config (/etc/sqreader/<port>.config.json ve .env). TOKEN yedeğe ALINMAZ;
#    yalnız --token-yedekle ile, ayrı SIR/ dizinine ve açık uyarıyla.
#  * bütünlük: `PRAGMA integrity_check` (yedek DB üzerinde) ve SHA256SUMS listesi.
#
# GERİ AL:
#  * servis ÇALIŞIYORSA REDDEDER (kendisi durdurmaz, başlatmaz).
#  * SHA256SUMS + integrity_check + server_id denetimi geçmeden hiçbir şeye dokunmaz.
#  * mevcut veriyi SİLMEZ; <veri>/.geri-al-oncesi-<zaman>/ altına TAŞIR.
#    (export_audit.log eklemeli bir denetim izidir: yerinde BIRAKILIR, geri yüklenmez.)
#  * --dry-run: hiçbir şey değiştirmez, yalnız ne yapacağını yazar.
#  * config'i geri YÜKLEMEZ (root gerekir); farkı gösterir.
#
# Bu betik oyun sunucusuna/konteynerine hiçbir koşulda dokunmaz.
#
# Yetki: sqreader kullanıcısı olarak çalışır; root ile çağrılırsa kendini
# `runuser`/`setpriv` ile sqreader'a çevirir (DB ve kayıt dosyaları sqreader'ındır;
# root olarak yazılırsa -wal/-shm dosyaları root'a ait olur ve reader açamaz).
# Yedek hedef dizini sqreader tarafından yazılabilir olmalıdır.
#
# DOĞRULANMADI: bu betik Windows geliştirme makinesinde yazıldı; `bash -n` ve
# sahte sqlite3/systemctl ile sınırlı yerel deneme yapıldı. Gerçek çalıştırma
# "çalıştırılmadı: Linux host gerekli".

set -u
set -o pipefail
umask 0077

KULLANICI="${SQREADER_KULLANICI:-sqreader}"
SR_HOME="${SQREADER_HOME:-/opt/sqreader}"
ETC_DIR="${SQREADER_ETC:-/etc/sqreader}"
VERI_KOK="${SQREADER_VERI_KOK:-/var/lib/sqreader}"

kullanim() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

hata_cik() { printf 'HATA: %s\n' "$*" >&2; exit 1; }
bilgi()    { printf '  %s\n' "$*"; }
uyari()    { printf 'UYARI: %s\n' "$*" >&2; }
var_mi()   { command -v "$1" >/dev/null 2>&1; }

# --- argümanlar ---------------------------------------------------------------
[ $# -ge 1 ] || { kullanim >&2; exit 2; }
ALT="$1"; shift
case "$ALT" in
    yedek|geri-al) ;;
    -h|--yardim|--help) kullanim; exit 0 ;;
    *) echo "bilinmeyen alt komut: $ALT" >&2; kullanim >&2; exit 2 ;;
esac

PORT=""; HEDEF=""; TOKEN_YEDEKLE=0; DRY=0
POZ=()
for arg in "$@"; do
    case "$arg" in
        --token-yedekle) TOKEN_YEDEKLE=1 ;;
        --dry-run) DRY=1 ;;
        -h|--yardim|--help) kullanim; exit 0 ;;
        -*) echo "bilinmeyen seçenek: $arg" >&2; exit 2 ;;
        *) POZ+=("$arg") ;;
    esac
done
[ "${#POZ[@]}" -eq 2 ] || { echo "kullanım: $0 $ALT <port> <dizin>" >&2; exit 2; }
PORT="${POZ[0]}"; HEDEF="${POZ[1]}"
case "$PORT" in ""|*[!0-9]*) echo "port rakam olmalı: $PORT" >&2; exit 2 ;; esac
[ "$ALT" = "geri-al" ] || [ "$DRY" = 0 ] || { echo "--dry-run yalnız geri-al için" >&2; exit 2; }
[ "$ALT" = "yedek" ] || [ "$TOKEN_YEDEKLE" = 0 ] || { echo "--token-yedekle yalnız yedek için" >&2; exit 2; }
# sqlite3 .backup komutu tek tırnak içinde yol alır: yolda tırnak/satır sonu olmasın.
case "$HEDEF" in *"'"*|*$'\n'*) echo "dizin yolunda tek tırnak/satır sonu olamaz" >&2; exit 2 ;; esac

# --- yetki: sqreader kullanıcısı ----------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    id "$KULLANICI" >/dev/null 2>&1 || hata_cik "kullanıcı yok: $KULLANICI"
    if var_mi runuser; then
        exec runuser -u "$KULLANICI" -- "$BASH" "$0" "$ALT" "$@"
    elif var_mi setpriv; then
        exec setpriv --reuid="$KULLANICI" --regid="$KULLANICI" --init-groups -- "$BASH" "$0" "$ALT" "$@"
    else
        hata_cik "root olarak çalıştırılamaz (dosyalar root'a ait olur) ve runuser/setpriv yok. sudo -u $KULLANICI ile çalıştırın."
    fi
fi

CONFIG="$ETC_DIR/$PORT.config.json"
ENV_DOSYASI="$ETC_DIR/$PORT.env"
TOKEN_DOSYASI="$ETC_DIR/$PORT.export-token"
VERI="$VERI_KOK/$PORT"
BIRIM="sqreader@$PORT.service"
DB="$VERI/stats/player_stats.db"
KAYITLAR="$VERI/recordings"

for arac in sha256sum find cp mv mkdir; do
    var_mi "$arac" || hata_cik "gerekli araç yok: $arac"
done
var_mi sqlite3 || hata_cik "sqlite3 komut satırı aracı yok (Debian/Ubuntu: apt install sqlite3)"

PY=""
for aday in "$SR_HOME/venv/bin/python" python3 python; do
    if var_mi "$aday" && "$aday" -c 'import json' >/dev/null 2>&1; then PY="$aday"; break; fi
done
[ -n "$PY" ] || hata_cik "JSON okumak için çalışan python bulunamadı"

config_metin() {
    # config_metin <anahtar>: config'teki metin değeri (yoksa/metin değilse boş)
    "$PY" - "$CONFIG" "$1" <<'PYEOF' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        v = json.load(f).get(sys.argv[2])
    print(v if isinstance(v, str) else "")
except Exception:
    print("")
PYEOF
}
config_server_id() { config_metin server_id; }

# Durum dizini: config export_state_dir, yoksa kayıt dizininin üst dizini.
durum_dizini() {
    local d
    d="$(config_metin export_state_dir)"
    case "$d" in
        "") printf '%s' "$VERI" ;;
        /*) printf '%s' "$d" ;;
        *) printf '%s' "$VERI" ;;
    esac
}
DURUM_DIZINI="$(durum_dizini)"
DURUM_DOSYALARI="export_state.json export_audit.log retention_state.json"

db_kontrol() {
    # db_kontrol <dosya>: integrity_check "ok" mı?
    local sonuc
    sonuc="$(sqlite3 -readonly -bail "$1" 'PRAGMA integrity_check;' 2>&1)" || { echo "$sonuc"; return 1; }
    [ "$sonuc" = "ok" ] || { echo "$sonuc"; return 1; }
    return 0
}

# ============================================================== YEDEK
yedek() {
    [ -d "$VERI" ] || hata_cik "veri dizini yok: $VERI"
    [ -f "$DB" ] || hata_cik "stats DB yok: $DB"
    [ -d "$HEDEF" ] || hata_cik "hedef dizin yok: $HEDEF (önce oluşturun ve sqreader'a yazma izni verin)"
    [ -w "$HEDEF" ] || hata_cik "hedef dizin yazılamıyor ($KULLANICI): $HEDEF"

    local zaman ad son
    zaman="$(date -u +%Y%m%dT%H%M%SZ)"
    ad="sqreader-$PORT-$zaman"
    son="$HEDEF/$ad"
    GECICI="$HEDEF/.$ad.tmp"
    [ ! -e "$son" ] && [ ! -e "$GECICI" ] || hata_cik "hedef zaten var: $son"
    mkdir -m 0700 "$GECICI" || hata_cik "geçici yedek dizini oluşturulamadı"
    # Başarısızlıkta YALNIZ kendi oluşturduğumuz geçici dizini kaldır.
    trap '[ -n "${GECICI:-}" ] && rm -rf -- "$GECICI"' EXIT

    local sid
    sid="$(config_server_id)"
    [ -n "$sid" ] || uyari "config'ten server_id okunamadı ($CONFIG); yedeğin server_id denetimi atlanacak"

    echo "== Yedek: port $PORT -> $son"
    mkdir -p "$GECICI/stats" "$GECICI/recordings" "$GECICI/config" || hata_cik "dizin oluşturulamadı"

    # 1. stats DB: çalışan DB'nin tutarlı kopyası
    echo "-- stats DB (sqlite3 .backup)"
    sqlite3 -bail "$DB" ".timeout 30000" ".backup '$GECICI/stats/player_stats.db'" \
        || hata_cik "sqlite3 .backup başarısız"
    # Yedek DB'yi tek dosyalık (DELETE günlük kipi) yap: -wal/-shm gerektirmez,
    # salt-okunur açılıp doğrulanabilir.
    sqlite3 -bail "$GECICI/stats/player_stats.db" 'PRAGMA journal_mode=DELETE;' >/dev/null         || hata_cik "yedek DB günlük kipi değiştirilemedi"
    local ic
    if ic="$(db_kontrol "$GECICI/stats/player_stats.db")"; then
        bilgi "integrity_check: ok"
    else
        hata_cik "yedek DB integrity_check başarısız: $ic"
    fi
    local sids
    sids="$(sqlite3 -readonly "$GECICI/stats/player_stats.db" 'SELECT server_id, COUNT(*) FROM matches GROUP BY server_id;' 2>/dev/null | tr '\n' ' ')"
    bilgi "matches (server_id|sayı): ${sids:-tablo yok/boş}"
    if [ -n "$sid" ] && [ -n "$sids" ]; then
        # yalnız config'teki server_id'nin verisi beklenir
        local yabanci
        yabanci="$(sqlite3 -readonly "$GECICI/stats/player_stats.db" "SELECT COUNT(*) FROM matches WHERE server_id <> '$(printf '%s' "$sid" | sed "s/'/''/g")';" 2>/dev/null)"
        [ "${yabanci:-0}" = "0" ] || hata_cik "DB config server_id ($sid) dışında $yabanci maç satırı içeriyor; yanlış dizin olabilir"
    fi
    # advisory önbellek (yer imi/yerleşimci önbelleği) varsa
    [ -f "$VERI/stats/placer_cache.json" ] && cp -p -- "$VERI/stats/placer_cache.json" "$GECICI/stats/" 2>/dev/null

    # 2. finalize kayıtlar
    echo "-- finalize kayıtlar"
    local kopya=0 atlanan=0 f id meta durum
    if [ -d "$KAYITLAR" ]; then
        while IFS= read -r -d '' f; do
            id="${f##*/}"; id="${id%.sqrx}"
            case "$id" in
                ""|*[!A-Za-z0-9._-]*) atlanan=$((atlanan + 1)); uyari "kayıt adı beklenmeyen karakter içeriyor, atlandı"; continue ;;
            esac
            meta="$KAYITLAR/$id.meta.json"
            if [ ! -f "$meta" ]; then atlanan=$((atlanan + 1)); continue; fi
            durum="$("$PY" - "$meta" <<'PYEOF' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    fin = d.get("recordingState") == "finalized" and not d.get("inProgress", False)
    print("finalized" if fin else "diger")
except Exception:
    print("diger")
PYEOF
)"
            if [ "$durum" = "finalized" ]; then
                cp -p -- "$f" "$GECICI/recordings/$id.sqrx" && cp -p -- "$meta" "$GECICI/recordings/$id.meta.json" \
                    || hata_cik "kayıt kopyalanamadı: $id"
                # aktif olmadığı için değişmemeli; boyut denetimi
                [ "$(stat -c %s "$f")" = "$(stat -c %s "$GECICI/recordings/$id.sqrx")" ] \
                    || hata_cik "kayıt boyutu kopyada farklı (yazılıyor olabilir): $id"
                kopya=$((kopya + 1))
            else
                atlanan=$((atlanan + 1))
            fi
        done < <(find "$KAYITLAR" -maxdepth 1 -type f -name '*.sqrx' -print0 | sort -z)
    fi
    bilgi "kopyalanan finalize kayıt: $kopya; atlanan (aktif/doğrulanmamış/sidecar yok): $atlanan"

    # 3. export_state.json / export_audit.log / retention_state.json (VARSA).
    # DOĞRULANMADI: bu adlar sqreader/export_state.py'den alındı (çalışma ağacında,
    # paralel görev); son sürümde doğrulanmalı.
    echo "-- export durumu / denetim günlüğü (varsa): $DURUM_DIZINI"
    local bulundu=0 aday
    mkdir -p "$GECICI/export_state"
    for aday in $DURUM_DOSYALARI; do
        if [ -f "$DURUM_DIZINI/$aday" ]; then
            cp -p -- "$DURUM_DIZINI/$aday" "$GECICI/export_state/" && bulundu=$((bulundu + 1))
        fi
    done
    if [ "$bulundu" -gt 0 ]; then
        bilgi "kopyalanan dosya: $bulundu"
    else
        bilgi "yok (atlandı)"; rmdir "$GECICI/export_state" 2>/dev/null
    fi

    # 4. config (token HARİÇ)
    echo "-- config"
    [ -f "$CONFIG" ] && cp -p -- "$CONFIG" "$GECICI/config/" || uyari "config kopyalanamadı: $CONFIG"
    [ -f "$ENV_DOSYASI" ] && cp -p -- "$ENV_DOSYASI" "$GECICI/config/" || uyari "env dosyası kopyalanamadı: $ENV_DOSYASI"
    if [ "$TOKEN_YEDEKLE" = 1 ]; then
        uyari "TOKEN yedekleniyor: SIR/ dizini bir GİZLİ BİLGİ içerir. Yedeğin tamamını şifreli/erişimi kısıtlı yerde tutun; Zero Zone tarafındaki kopyayla birlikte döndürmeyi (rotate) düşünün."
        mkdir -m 0700 "$GECICI/SIR" && cp -p -- "$TOKEN_DOSYASI" "$GECICI/SIR/" \
            || hata_cik "token kopyalanamadı"
    else
        bilgi "token yedeğe ALINMADI (kasıtlı). Kaybolursa yeniden üretilir: KURULUM-TR.md bölüm 5."
    fi

    # 5. manifest + sha256 listesi
    {
        echo "format=sqreader-yedek-1"
        echo "port=$PORT"
        echo "server_id=$sid"
        echo "olusturma_utc=$zaman"
        echo "ana_makine=$(hostname 2>/dev/null)"
        echo "kayit_sayisi=$kopya"
        echo "token_dahil=$TOKEN_YEDEKLE"
    } > "$GECICI/MANIFEST.txt"
    ( cd "$GECICI" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS ) \
        || hata_cik "SHA256SUMS üretilemedi"
    ( cd "$GECICI" && sha256sum -c --quiet SHA256SUMS ) || hata_cik "SHA256SUMS kendi kendini doğrulayamadı"
    bilgi "SHA256SUMS: $(wc -l < "$GECICI/SHA256SUMS") dosya"

    mv -- "$GECICI" "$son" || hata_cik "yedek son adına taşınamadı"
    trap - EXIT
    echo "TAMAM: yedek hazır: $son"
    echo "Sıra hatırlatması: ÖNCE bu reader yedeği, SONRA Zero Zone veritabanı yedeği (KURULUM-TR.md bölüm 15)."
}

# ============================================================ GERİ AL
geri_al() {
    local y="$HEDEF"
    [ -d "$y" ] || hata_cik "yedek dizini yok: $y"
    [ -f "$y/MANIFEST.txt" ] && [ -f "$y/SHA256SUMS" ] || hata_cik "geçerli yedek değil (MANIFEST.txt/SHA256SUMS yok): $y"
    [ -f "$y/stats/player_stats.db" ] || hata_cik "yedekte stats/player_stats.db yok"
    [ "$DRY" = 1 ] && echo "== DRY-RUN: hiçbir şey değiştirilmeyecek"

    echo "== Geri al: $y -> $VERI (port $PORT)"

    # 1. servis DURMUŞ olmalı (kendisi durdurmaz/başlatmaz)
    var_mi systemctl || hata_cik "systemctl yok: servisin durduğu doğrulanamıyor, reddediliyor"
    local durum
    durum="$(systemctl is-active "$BIRIM" 2>/dev/null)"
    case "$durum" in
        inactive|failed|"") ;;
        *) hata_cik "$BIRIM şu an '$durum'. Geri alma reddedildi. Servisi ELLE durdurun (systemctl stop $BIRIM), sonra tekrar deneyin. Bu betik durdurmaz." ;;
    esac
    bilgi "servis durumu: ${durum:-bilinmiyor} (durmuş)"
    if var_mi fuser && [ -f "$DB" ] && fuser "$DB" >/dev/null 2>&1; then
        hata_cik "stats DB başka bir süreç tarafından açık ($DB). Geri alma reddedildi."
    fi

    # 2. yedek bütünlüğü
    ( cd "$y" && sha256sum -c --quiet SHA256SUMS ) || hata_cik "SHA256SUMS doğrulaması BAŞARISIZ: yedek bozuk ya da değiştirilmiş"
    bilgi "SHA256SUMS: ok"
    local ic
    if ic="$(db_kontrol "$y/stats/player_stats.db")"; then bilgi "integrity_check: ok"; else hata_cik "yedek DB integrity_check başarısız: $ic"; fi

    local m_port m_sid c_sid
    m_port="$(sed -n 's/^port=//p' "$y/MANIFEST.txt" | head -n1)"
    m_sid="$(sed -n 's/^server_id=//p' "$y/MANIFEST.txt" | head -n1)"
    [ "$m_port" = "$PORT" ] || hata_cik "yedek port $m_port için, istenen $PORT"
    c_sid="$(config_server_id)"
    if [ -n "$m_sid" ] && [ -n "$c_sid" ] && [ "$m_sid" != "$c_sid" ]; then
        hata_cik "yedeğin server_id'si ($m_sid) mevcut config'inkinden ($c_sid) farklı; farklı sunucunun verisi geri alınmaz"
    fi
    bilgi "server_id: yedek=${m_sid:-?} config=${c_sid:-?}"

    [ -d "$VERI" ] || hata_cik "veri dizini yok: $VERI"
    [ -w "$VERI" ] || hata_cik "veri dizini yazılamıyor ($KULLANICI): $VERI"

    # 3. plan
    local zaman kenar
    zaman="$(date -u +%Y%m%dT%H%M%SZ)"
    kenar="$VERI/.geri-al-oncesi-$zaman"
    echo "-- plan"
    bilgi "mevcut veri kenara taşınacak: $kenar"
    bilgi "geri yüklenecek: stats/player_stats.db, stats/placer_cache.json (varsa), recordings/* (yedekteki), export_state.json + retention_state.json (varsa; export_audit.log yerinde kalır)"
    bilgi "config GERİ YÜKLENMEZ (root gerekir); yedekteki kopya: $y/config/"

    if [ "$DRY" = 1 ]; then
        local n
        n="$(find "$y/recordings" -maxdepth 1 -type f -name '*.sqrx' 2>/dev/null | wc -l)"
        bilgi "yedekte $n kayıt var"
        [ -f "$DB" ] && bilgi "mevcut DB kenara taşınırdı: $DB (+ -wal/-shm)"
        echo "DRY-RUN bitti: hiçbir değişiklik yapılmadı."
        return 0
    fi

    # 4. kenara taşı (silme yok)
    mkdir -m 0700 "$kenar" || hata_cik "kenar dizini oluşturulamadı: $kenar"
    mkdir -p "$kenar/stats" "$kenar/recordings" || hata_cik "kenar alt dizinleri oluşturulamadı"
    local dosya
    for dosya in "$DB" "$DB-wal" "$DB-shm" "$VERI/stats/placer_cache.json"; do
        if [ -e "$dosya" ]; then
            mv -- "$dosya" "$kenar/stats/" || hata_cik "kenara taşınamadı: $dosya (kenar: $kenar)"
        fi
    done
    mkdir -p "$kenar/export_state" || hata_cik "kenar alt dizini oluşturulamadı"
    for dosya in export_state.json retention_state.json; do
        # export_audit.log eklemeli denetim izidir: yerinde bırakılır (yedektekiyle ÜZERİNE YAZILMAZ).
        if [ -f "$y/export_state/$dosya" ] && [ -e "$DURUM_DIZINI/$dosya" ]; then
            mv -- "$DURUM_DIZINI/$dosya" "$kenar/export_state/" || hata_cik "kenara taşınamadı: $dosya"
        fi
    done

    # 5. geri yükle
    mkdir -p "$VERI/stats" "$KAYITLAR" || hata_cik "hedef dizinler oluşturulamadı"
    umask 0027
    cp -p -- "$y/stats/player_stats.db" "$DB.yeni" || hata_cik "DB kopyalanamadı (mevcut veri kenarda: $kenar)"
    mv -- "$DB.yeni" "$DB" || hata_cik "DB yerine konamadı (mevcut veri kenarda: $kenar)"
    [ -f "$y/stats/placer_cache.json" ] && cp -p -- "$y/stats/placer_cache.json" "$VERI/stats/"
    for dosya in export_state.json retention_state.json; do
        if [ -f "$y/export_state/$dosya" ]; then
            mkdir -p "$DURUM_DIZINI" && cp -p -- "$y/export_state/$dosya" "$DURUM_DIZINI/$dosya"                 || hata_cik "$dosya geri yüklenemedi (mevcut olan kenarda: $kenar)"
        fi
    done
    [ -f "$y/export_state/export_state.json" ] && bilgi "export_state.json geri yüklendi: Zero Zone onayları yedekteki hâline döndü (eski onaylar kayıptır; onaysız kayıt retention'da korunur)"
    local f id ustune=0 yeni=0 ayni=0
    while IFS= read -r -d '' f; do
        id="${f##*/}"
        if [ -e "$KAYITLAR/$id" ]; then
            if cmp -s -- "$f" "$KAYITLAR/$id"; then ayni=$((ayni + 1)); continue; fi
            mv -- "$KAYITLAR/$id" "$kenar/recordings/" || hata_cik "kenara taşınamadı: $id"
            ustune=$((ustune + 1))
        else
            yeni=$((yeni + 1))
        fi
        cp -p -- "$f" "$KAYITLAR/$id" || hata_cik "kayıt geri yüklenemedi: $id (mevcut veri kenarda: $kenar)"
    done < <(find "$y/recordings" -maxdepth 1 -type f \( -name '*.sqrx' -o -name '*.meta.json' \) -print0 | sort -z)
    bilgi "kayıtlar: $yeni yeni, $ayni zaten aynı, $ustune farklı olanın eskisi kenara alındı"

    if ic="$(db_kontrol "$DB")"; then bilgi "geri yüklenen DB integrity_check: ok"; else hata_cik "geri yüklenen DB integrity_check BAŞARISIZ: $ic (eski veri kenarda: $kenar)"; fi

    if [ -f "$y/config/$PORT.config.json" ] && [ -r "$CONFIG" ]; then
        if ! cmp -s -- "$y/config/$PORT.config.json" "$CONFIG"; then
            uyari "yedekteki config mevcut config'ten FARKLI. Gerekirse root ile elle karşılaştırın: diff $y/config/$PORT.config.json $CONFIG"
        fi
    fi

    echo "TAMAM: geri alma bitti. Eski veri kenarda: $kenar"
    echo "Servis BAŞLATILMADI. Elle: systemctl start $BIRIM   (oyun sunucusuna dokunulmadı)"
    echo "Geri almayı geri döndürmek için: servisi durdurun, $kenar içeriğini yerine taşıyın."
}

case "$ALT" in
    yedek) yedek ;;
    geri-al) geri_al ;;
esac
