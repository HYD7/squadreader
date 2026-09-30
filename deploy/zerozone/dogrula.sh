#!/usr/bin/env bash
# dogrula.sh - Zero Zone SquadReader dağıtımı için SALT-OKUNUR teşhis.
#
# Kullanım:
#   sudo ./dogrula.sh <port>                 örnek: sudo ./dogrula.sh 7787
#   sudo ./dogrula.sh <port> --hizli         doctor/summary (bellek okuyan adımlar) atlanır
#   sudo ./dogrula.sh <port> --tam-yetki     bellek okuyan adımlar root olarak koşar
#                                            (YALNIZ geçici teşhis; bkz. KURULUM-TR.md bölüm 8)
#
# Ne yapmaz: hiçbir şeyi değiştirmez; hiçbir süreci durdurmaz/başlatmaz/sinyallemez;
# oyun sunucusuna ya da konteynerine dokunmaz; ağa dışarı çıkmaz (yalnız
# 127.0.0.1'deki reader'a bağlanır). Token'ı ekrana BASMAZ.
#
# Yetkiler: root olarak çalıştırılırsa bellek okuyan adımlar (doctor/summary) ve
# log erişimi, servisin sahip olacağı yetkilerle (kullanıcı sqreader + yalnız
# CAP_SYS_PTRACE ve CAP_DAC_READ_SEARCH) `setpriv` ile koşturulur; böylece "asgari
# yetkiyle çalışıyor mu" sınanmış olur. root değilsen adımlar kendi yetkinle koşar
# ve sonuç servisle aynı olmayabilir (belirtilir).
#
# Her adım TAMAM / HATA / ATLANDI basar. Biri başarısız olsa da devam eder.
# Çıkış kodu: hiç HATA yoksa 0, en az bir HATA varsa 1, kullanım hatası 2.
#
# DOĞRULANMADI: bu betik Windows geliştirme makinesinde yazıldı; yalnız `bash -n`
# sözdizimi denetimi yapıldı. Gerçek çalıştırma "çalıştırılmadı: Linux host gerekli".

set -u
set -o pipefail

KULLANICI="${SQREADER_KULLANICI:-sqreader}"
SR_HOME="${SQREADER_HOME:-/opt/sqreader}"
ETC_DIR="${SQREADER_ETC:-/etc/sqreader}"
VERI_KOK="${SQREADER_VERI_KOK:-/var/lib/sqreader}"
SR_BIN="${SQREADER_BIN:-$SR_HOME/venv/bin/sqreader}"
DISK_ESIK_YUZDE="${DISK_ESIK_YUZDE:-90}"
IKINCI_SUNUCU_ACIK="${IKINCI_SUNUCU_ACIK:-0}"
CLI_ZAMAN_ASIMI="${CLI_ZAMAN_ASIMI:-300}"

TAMAM_N=0
HATA_N=0
ATLANDI_N=0
HATA_LISTESI=()
ATLANDI_LISTESI=()

kullanim() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

PORT=""
HIZLI=0
TAM_YETKI=0
for arg in "$@"; do
    case "$arg" in
        --hizli) HIZLI=1 ;;
        --tam-yetki) TAM_YETKI=1 ;;
        -h|--yardim|--help) kullanim; exit 0 ;;
        -*) echo "bilinmeyen seçenek: $arg" >&2; exit 2 ;;
        *) if [ -z "$PORT" ]; then PORT="$arg"; else echo "fazla argüman: $arg" >&2; exit 2; fi ;;
    esac
done
case "$PORT" in
    ""|*[!0-9]*) echo "kullanım: $0 <oyun-portu> [--hizli] [--tam-yetki]" >&2; exit 2 ;;
esac

CONFIG="$ETC_DIR/$PORT.config.json"
TOKEN_DOSYASI="$ETC_DIR/$PORT.export-token"
ENV_DOSYASI="$ETC_DIR/$PORT.env"
VERI_DIZINI="$VERI_KOK/$PORT"
BIRIM="sqreader@$PORT.service"

# ---------------------------------------------------------------- yardımcılar

tamam()   { TAMAM_N=$((TAMAM_N + 1));     printf '  TAMAM    %s\n' "$*"; }
hata()    { HATA_N=$((HATA_N + 1));       HATA_LISTESI+=("$*");    printf '  HATA     %s\n' "$*"; }
atlandi() { ATLANDI_N=$((ATLANDI_N + 1)); ATLANDI_LISTESI+=("$*"); printf '  ATLANDI  %s\n' "$*"; }
bilgi()   { printf '           %s\n' "$*"; }
bolum()   { printf '\n== %s ==\n' "$*"; }
var_mi()  { command -v "$1" >/dev/null 2>&1; }

# JSON ayrıştırma için çalışan bir python: önce venv, sonra sistem python3/python
# (gerçekten çalıştığı denenir).
PY=""
for aday in "$SR_HOME/venv/bin/python" python3 python; do
    if var_mi "$aday" && "$aday" -c 'import json' >/dev/null 2>&1; then PY="$aday"; break; fi
done

# Servisin yetkilerine yakın koşum: root ise setpriv ile kullanıcı + asgari yetenek.
# --tam-yetki: root olarak (geçici teşhis). root değilse: kendi yetkin.
YETKI_KIPI="kendi-yetkin"
if [ "$TAM_YETKI" = 1 ]; then
    YETKI_KIPI="root-tam-yetki"
elif [ "$(id -u)" -eq 0 ] && var_mi setpriv && id "$KULLANICI" >/dev/null 2>&1; then
    YETKI_KIPI="setpriv"
fi

run_as() {
    case "$YETKI_KIPI" in
        setpriv)
            setpriv --reuid="$KULLANICI" --regid="$KULLANICI" --init-groups \
                --inh-caps=+sys_ptrace,+dac_read_search \
                --ambient-caps=+sys_ptrace,+dac_read_search \
                --bounding-set=-all,+sys_ptrace,+dac_read_search \
                --no-new-privs -- "$@" ;;
        *) "$@" ;;
    esac
}

GECICI_DIZIN=""
temizle() {
    if [ -n "$GECICI_DIZIN" ] && [ -d "$GECICI_DIZIN" ]; then
        rm -rf -- "$GECICI_DIZIN"
    fi
}
trap temizle EXIT

# CLI'yi servisin ortamıyla koştur. Adres önbelleği GEÇİCİ dizine yönlendirilir:
# gerçek veri dizinine hiçbir şey yazılmaz. Mevcut önbellek varsa (yalnız okunup)
# geçici dizine kopyalanır ki keşif sıcak başlasın.
sr_cli() {
    ( cd / && run_as env \
        SQREADER_CONFIG="$CONFIG" \
        SQREADER_NET_POLICY=local-only \
        SQR_ADDR_CACHE="$GECICI_DIZIN/addr-cache.json" \
        SQREADER_DATA_DIR="${SQREADER_DATA_DIR:-$SR_HOME/src/data/static}" \
        HOME="$GECICI_DIZIN" \
        PYTHONDONTWRITEBYTECODE=1 \
        timeout "$CLI_ZAMAN_ASIMI" "$SR_BIN" "$@" )
}

json_alan() {
    # json_alan <dosya> <anahtar>  -> JSON değeri, "__YOK__" ya da "__OKUNAMADI__"
    [ -n "$PY" ] || { echo "__OKUNAMADI__"; return; }
    "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null || echo "__OKUNAMADI__"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    if sys.argv[2] not in d:
        print("__YOK__")
    else:
        print(json.dumps(d[sys.argv[2]], ensure_ascii=False))
except Exception:
    print("__OKUNAMADI__")
PYEOF
}

json_stdin_alan() {
    # stdin'den JSON okur; ilk argüman anahtar.
    [ -n "$PY" ] || { echo "__OKUNAMADI__"; return; }
    "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    v = d.get(sys.argv[1], None)
    print("__NULL__" if v is None else (json.dumps(v) if not isinstance(v, str) else v))
except Exception:
    print("__OKUNAMADI__")
' "$1" 2>/dev/null || echo "__OKUNAMADI__"
}

ozel_adres() {
    # Loopback / RFC1918 / CGNAT / ULA ise 0 döner.
    case "$1" in
        127.*|10.*|192.168.*|::1|"[::1]"|fc*|fd*|"[fc"*|"[fd"*) return 0 ;;
        172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
        "[::ffff:127."*|"[::ffff:10."*|"[::ffff:192.168."*) return 0 ;;
    esac
    return 1
}

echo "Zero Zone SquadReader doğrulama - örnek portu: $PORT"
echo "Yetki kipi: $YETKI_KIPI"
GECICI_DIZIN="$(mktemp -d 2>/dev/null || true)"
if [ -z "$GECICI_DIZIN" ] || [ ! -d "$GECICI_DIZIN" ]; then
    echo "HATA: geçici dizin oluşturulamadı" >&2
    exit 2
fi
if [ "$YETKI_KIPI" = "setpriv" ]; then
    chown "$KULLANICI" "$GECICI_DIZIN" 2>/dev/null || true
fi
chmod 0700 "$GECICI_DIZIN" 2>/dev/null || true
if [ -r "$VERI_DIZINI/addr-cache.json" ]; then
    cp -- "$VERI_DIZINI/addr-cache.json" "$GECICI_DIZIN/addr-cache.json" 2>/dev/null || true
    [ "$YETKI_KIPI" = "setpriv" ] && chown "$KULLANICI" "$GECICI_DIZIN/addr-cache.json" 2>/dev/null
fi

# ---------------------------------------------------------- 1. işletim sistemi
bolum "1. İşletim sistemi / çekirdek"
if [ "$(uname -s)" = "Linux" ]; then
    tamam "çekirdek: $(uname -srm)"
else
    hata "Linux değil: $(uname -s). Reader /proc/<pid>/mem gerektirir; bu betik Linux host içindir."
fi
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    ( . /etc/os-release; bilgi "dağıtım: ${PRETTY_NAME:-bilinmiyor}" )
fi
if var_mi systemctl; then
    bilgi "systemd: $(systemctl --version 2>/dev/null | head -n1)"
else
    hata "systemctl bulunamadı (systemd gerekli)"
fi
if [ -x "$SR_HOME/venv/bin/python" ]; then
    tamam "python (venv): $("$SR_HOME/venv/bin/python" --version 2>&1)"
else
    hata "venv python bulunamadı: $SR_HOME/venv/bin/python (KURULUM-TR.md bölüm 3)"
fi
if [ -x "$SR_BIN" ]; then
    tamam "sqreader komutu var: $SR_BIN"
else
    hata "sqreader komutu yok: $SR_BIN"
fi
var_mi sqlite3 && bilgi "sqlite3: $(sqlite3 --version 2>/dev/null | cut -d' ' -f1)" \
    || bilgi "sqlite3 komut satırı aracı yok (yedek-geri-al.sh için gerekli)"

# ------------------------------------------------------------ 2. ptrace_scope
bolum "2. ptrace_scope ve /proc görünürlüğü"
PS_DOSYASI=/proc/sys/kernel/yama/ptrace_scope
if [ -r "$PS_DOSYASI" ]; then
    PS="$(cat "$PS_DOSYASI" 2>/dev/null)"
    case "$PS" in
        0) tamam "kernel.yama.ptrace_scope=0 (klasik; UID/yetenek denetimi yeterli)" ;;
        1) tamam "kernel.yama.ptrace_scope=1 (yalnız soy süreçler; CAP_SYS_PTRACE bu kısıtı aşar)" ;;
        2) tamam "kernel.yama.ptrace_scope=2 (yalnız CAP_SYS_PTRACE; servis bu yeteneğe sahip)" ;;
        3) hata "kernel.yama.ptrace_scope=3 (ptrace/attach tamamen kapalı, yetenekle de aşılmaz, yeniden başlatmaya kadar geri alınmaz). Reader çalışamaz." ;;
        *) hata "ptrace_scope beklenmeyen değer: $PS" ;;
    esac
else
    tamam "Yama etkin değil (ptrace_scope dosyası yok); ek kısıt yok"
fi
PROC_SECENEK="$(awk '$2=="/proc"{print $4}' /proc/mounts 2>/dev/null | head -n1)"
case "$PROC_SECENEK" in
    *hidepid=1*|*hidepid=2*|*hidepid=noaccess*|*hidepid=invisible*|*hidepid=ptraceable*)
        hata "/proc hidepid ile bağlı ($PROC_SECENEK): reader oyun sürecini göremeyebilir. hidepid'i gevşetmek ya da sqreader'ı proc grubuna eklemek gerekir." ;;
    "") atlandi "/proc bağlama seçenekleri okunamadı" ;;
    *) tamam "/proc bağlama seçenekleri: $PROC_SECENEK (hidepid yok)" ;;
esac
bilgi "yetkisiz kullanıcıda ptrace_scope>=1 iken yetenek gerekir; yetenekler unit'te AmbientCapabilities ile verilir."

# ------------------------------------------------------------- 3. oyun süreçleri
bolum "3. Oyun süreçleri"
ESLESEN_PID=()
ESLESEN_SAYISI=0
TUM_OYUN_SAYISI=0
for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ -r "$d/cmdline" ] || continue
    cmd="$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)"
    [ -n "$cmd" ] || continue
    # Ön süzgeç: yalnız komut satırında oyun adı geçenlere bakılır (her süreç için
    # setpriv çalıştırmamak için).
    case "$cmd" in *SquadGameServer*) ;; *) continue ;; esac
    argv0="${cmd%% *}"
    exe="$(run_as readlink "$d/exe" 2>/dev/null || true)"
    ad_argv0="${argv0##*/}"
    ad_exe="${exe##*/}"
    # Başlatıcı betikleri (SquadGameServer.sh) ve bu betiğin kendisi elenir:
    # yalnız yürütülebilir dosyası SquadGameServer olan süreç oyun sayılır.
    if [ "$ad_exe" != "SquadGameServer" ] && [ "$ad_argv0" != "SquadGameServer" ]; then
        continue
    fi
    TUM_OYUN_SAYISI=$((TUM_OYUN_SAYISI + 1))
    jetonlar="$(printf '%s' "$cmd" | tr ' ?' '\n\n')"
    game_port="$(printf '%s\n' "$jetonlar" | grep -iE '^-?Port=[0-9]+$' | sed 's/.*=//' | sort -u | paste -sd, -)"
    query_port="$(printf '%s\n' "$jetonlar" | grep -iE '^-?QueryPort=[0-9]+$' | sed 's/.*=//' | sort -u | paste -sd, -)"
    kimlik="$(grep -oE '[0-9a-f]{64}' "$d/cgroup" 2>/dev/null | head -n1)"
    baslangic="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//')"
    kullanici="$(ps -o user= -p "$pid" 2>/dev/null | sed 's/^ *//')"
    tur=""
    case ",${game_port}," in *",$PORT,"*) tur="Port" ;; esac
    case ",${query_port}," in *",$PORT,"*) tur="${tur:+$tur+}QueryPort" ;; esac
    printf '  süreç %s\n' "$pid"
    bilgi "exe        : ${exe:-okunamadı (yetki)}"
    bilgi "kullanıcı   : ${kullanici:-?}"
    bilgi "Port=       : ${game_port:-yok}"
    bilgi "QueryPort=  : ${query_port:-yok}"
    bilgi "konteyner   : ${kimlik:-bulunamadı (konteyner dışı olabilir)}"
    bilgi "başlangıç   : ${baslangic:-?}"
    if [ -n "$tur" ]; then
        bilgi "eşleşme     : $PORT = $tur"
        ESLESEN_PID+=("$pid")
        ESLESEN_SAYISI=$((ESLESEN_SAYISI + 1))
    fi
done
if [ "$TUM_OYUN_SAYISI" -eq 0 ]; then
    hata "oyun süreci (SquadGameServer) görünmüyor. Çalışmıyor olabilir ya da /proc görünürlüğü/yetki eksik (root ile deneyin)."
elif [ "$ESLESEN_SAYISI" -eq 1 ]; then
    tamam "$PORT için TAM BİR süreç eşleşti (pid ${ESLESEN_PID[0]})"
    bilgi "config anahtarı: Port ile eşleştiyse squad_port=$PORT, QueryPort ile eşleştiyse squad_query_port=$PORT"
elif [ "$ESLESEN_SAYISI" -eq 0 ]; then
    hata "$TUM_OYUN_SAYISI oyun süreci var ama hiçbirinde Port=$PORT ya da QueryPort=$PORT yok. config'teki port ölçütünü yukarıdaki değerlere göre düzeltin."
else
    hata "$PORT için $ESLESEN_SAYISI süreç eşleşti: belirsiz, reader BAŞLAMAZ (ilk eşleşme seçilmez). expected_container_id ile daraltın."
fi

# config ile port ölçütü tutarlı mı (sqreader check-instance ile AYNI kural: örnek adı =
# squad_port YA DA squad_query_port; ikisi de yoksa ya da hiçbiri eşleşmiyorsa hata)
if [ -r "$CONFIG" ]; then
    SP="$(json_alan "$CONFIG" squad_port)"
    SQP="$(json_alan "$CONFIG" squad_query_port)"
    if [ "$SP" = "$PORT" ] || [ "$SQP" = "$PORT" ]; then
        tamam "config port ölçütü: squad_port=$SP squad_query_port=$SQP"
    else
        hata "config'te ne squad_port ne squad_query_port = $PORT (squad_port=$SP squad_query_port=$SQP): $CONFIG"
    fi
    SID="$(json_alan "$CONFIG" server_id)"
    bilgi "config server_id=$SID"
    ECID="$(json_alan "$CONFIG" expected_container_id)"
    bilgi "config expected_container_id=$ECID"
else
    hata "config okunamıyor: $CONFIG"
    SID="__OKUNAMADI__"
fi

# ---------------------------------------------- 4. /proc/<pid>/root altında log yolu
bolum "4. /proc/<pid>/root altında log erişimi"
if [ "$ESLESEN_SAYISI" -eq 1 ]; then
    PID="${ESLESEN_PID[0]}"
    LOG_KONT="$(json_alan "$CONFIG" log_path_in_container)"
    LOG_ACIK="$(json_alan "$CONFIG" log_path)"
    if [ "$LOG_ACIK" != "null" ] && [ "$LOG_ACIK" != "__YOK__" ] && [ "$LOG_ACIK" != "__OKUNAMADI__" ]; then
        LOG_YOLU="$(printf '%s' "$LOG_ACIK" | tr -d '"')"
        bilgi "config log_path (açık yol, reader'ın kendi dosya sisteminde): $LOG_YOLU"
    else
        if [ "$LOG_KONT" != "null" ] && [ "$LOG_KONT" != "__YOK__" ] && [ "$LOG_KONT" != "__OKUNAMADI__" ]; then
            LOG_IC="$(printf '%s' "$LOG_KONT" | tr -d '"')"
        else
            LOG_IC="/home/container/SquadGame/Saved/Logs/SquadGame.log"
        fi
        LOG_YOLU="/proc/$PID/root$LOG_IC"
        bilgi "konteyner içi log yolu: $LOG_IC"
    fi
    if run_as test -r "$LOG_YOLU" 2>/dev/null; then
        BOY="$(run_as stat -c %s "$LOG_YOLU" 2>/dev/null)"
        MZ="$(run_as stat -c %Y "$LOG_YOLU" 2>/dev/null)"
        YAS=""
        [ -n "$MZ" ] && YAS=" son değişiklik $(( $(date +%s) - MZ )) sn önce"
        tamam "log okunabiliyor ($YETKI_KIPI): $LOG_YOLU boyut=${BOY:-?} bayt$YAS"
    else
        hata "log okunamıyor ($YETKI_KIPI): $LOG_YOLU. Yol yanlış olabilir (config log_path_in_container / log_path) ya da yetenek yetmiyor."
    fi
else
    atlandi "log erişimi: tek bir eşleşen oyun süreci yok (bölüm 3)"
fi

# --------------------------------------------- 5. sqreader komutları (bellek okuyan)
bolum "5. sqreader version / doctor / summary / selftest"
if [ "$YETKI_KIPI" = "setpriv" ]; then
    CAP="$(run_as grep -E '^CapEff' /proc/self/status 2>/dev/null | awk '{print $2}')"
    if [ "$CAP" = "0000000000080004" ]; then
        tamam "koşum yetenekleri CapEff=$CAP (yalnız CAP_SYS_PTRACE + CAP_DAC_READ_SEARCH)"
    else
        hata "koşum yetenekleri beklenen 0000000000080004 değil: '${CAP:-okunamadı}' (setpriv ile yetenek kümesi kurulamadı; sonuçlar servisi temsil etmeyebilir)"
    fi
elif [ "$YETKI_KIPI" = "root-tam-yetki" ]; then
    bilgi "UYARI: root olarak koşuyor (geçici teşhis). Sonuçlar asgari yetkili servisi temsil ETMEZ."
else
    bilgi "UYARI: root değilsin; kendi yetkinle koşuyor. Sonuçlar servisi temsil etmeyebilir. sudo ile çalıştırın."
fi

if [ -x "$SR_BIN" ]; then
    if SURUM="$(sr_cli version 2>&1)"; then
        tamam "sqreader version: $SURUM"
    else
        hata "sqreader version çalışmadı: $(printf '%s' "$SURUM" | head -n3 | tr '\n' ' ')"
    fi

    CIKTI="$(sr_cli selftest 2>&1)"; RC=$?
    while IFS= read -r satir; do
        case "$satir" in
            "  ok "*) tamam "selftest: ${satir#  ok }" ;;
            "  FAIL "*)
                case "$satir" in
                    *"sealed envelope"*cryptography*|*"release signatures"*cryptography*)
                        atlandi "selftest: ${satir#  FAIL } (cryptography kurulu değil; yerel modda merkezi gönderim/güncelleme kapalı olduğundan gerekmez)" ;;
                    *) hata "selftest: ${satir#  FAIL }" ;;
                esac ;;
        esac
    done <<< "$CIKTI"
    [ "$RC" -ne 0 ] && [ "$(printf '%s\n' "$CIKTI" | grep -c '^  FAIL')" -eq 0 ] \
        && hata "selftest sıfır olmayan çıkış kodu ($RC) verdi: $(printf '%s' "$CIKTI" | tail -n2 | tr '\n' ' ')"

    if [ "$HIZLI" = 1 ]; then
        atlandi "sqreader doctor: --hizli"
        atlandi "sqreader summary: --hizli"
        atlandi "oyun sürümü/bellek düzeni uyumu: --hizli (doctor gerekli)"
    elif [ "$ESLESEN_SAYISI" -ne 1 ]; then
        atlandi "sqreader doctor: tek bir eşleşen oyun süreci yok"
        atlandi "sqreader summary: tek bir eşleşen oyun süreci yok"
        atlandi "oyun sürümü/bellek düzeni uyumu: doctor koşmadı"
    else
        bilgi "doctor bellek okur (yalnız okuma) ve soğuk keşifte 20-30 sn sürebilir; zaman aşımı ${CLI_ZAMAN_ASIMI} sn"
        DOCTOR="$(sr_cli doctor 2>&1)"; DRC=$?
        if [ "$DRC" -eq 0 ] && printf '%s\n' "$DOCTOR" | grep -q '^PASS  doctor'; then
            tamam "sqreader doctor: PASS"
        elif [ "$DRC" -eq 124 ]; then
            hata "sqreader doctor zaman aşımı (${CLI_ZAMAN_ASIMI} sn)"
        else
            hata "sqreader doctor başarısız (çıkış $DRC)"
            printf '%s\n' "$DOCTOR" | grep -E 'FAIL|HATA|izin|Permission|EPERM' | head -n 8 | sed 's/^/           /'
        fi
        # 6'ya taşınan: oyun sürümü / bellek düzeni uyumu
        BOLUM6_DOCTOR="$DOCTOR"; BOLUM6_RC="$DRC"

        SUMMARY="$(sr_cli summary 2>&1)"; SRC=$?
        if [ "$SRC" -eq 0 ]; then
            tamam "sqreader summary çalıştı ($(printf '%s\n' "$SUMMARY" | wc -l) satır)"
            printf '%s\n' "$SUMMARY" | head -n 8 | sed 's/^/           /'
        else
            hata "sqreader summary başarısız (çıkış $SRC): $(printf '%s' "$SUMMARY" | tail -n2 | tr '\n' ' ')"
        fi
    fi
else
    atlandi "sqreader komutları: $SR_BIN yok"
fi

# ------------------------------------- 6. oyun sürümü / bellek düzeni uyumu
bolum "6. Oyun sürümü / bellek düzeni uyumu"
if [ "${BOLUM6_RC+x}" = "x" ]; then
    if [ "$BOLUM6_RC" -eq 0 ]; then
        tamam "sabit ofsetler canlı ikiliyle uyumlu (doctor PASS)"
    else
        hata "ofset/bellek düzeni uyumsuz ya da okunamadı. Oyun güncellenmiş olabilir: KURULUM-TR.md bölüm 11 (kayıt durur, görünür hata beklenir)."
        printf '%s\n' "$BOLUM6_DOCTOR" | grep -E '^  FAIL' | head -n 10 | sed 's/^/           /'
    fi
    printf '%s\n' "$BOLUM6_DOCTOR" | grep -E 'DRIFTED' | head -n 4 | sed 's/^/           /'
else
    atlandi "uyum denetimi: doctor koşmadı"
fi
if [ "$ESLESEN_SAYISI" -eq 1 ]; then
    EXE_KIMLIK="$(run_as stat -L -c '%s:%Y' "/proc/${ESLESEN_PID[0]}/exe" 2>/dev/null)"
    [ -n "$EXE_KIMLIK" ] && bilgi "oyun ikilisi kimliği (boyut:mtime, sqreader/addrcache.py ile aynı): $EXE_KIMLIK"
fi

# ------------------------------------------------------------------ 7. disk
bolum "7. Disk alanı"
if [ -d "$VERI_DIZINI" ]; then
    DF="$(df -P "$VERI_DIZINI" 2>/dev/null | awk 'NR==2 {print $5" "$4" "$6}')"
    KUL="${DF%% *}"; KUL="${KUL%\%}"
    if [ -n "$KUL" ] && [ "$KUL" -lt "$DISK_ESIK_YUZDE" ] 2>/dev/null; then
        tamam "disk kullanımı %$KUL (eşik %$DISK_ESIK_YUZDE; bağlama: ${DF##* })"
    else
        hata "disk kullanımı yüksek ya da okunamadı: '${DF:-?}' (eşik %$DISK_ESIK_YUZDE). Dolu disk oyun sunucusunu da etkiler."
    fi
    var_mi du && bilgi "veri dizini boyutu: $(du -sh "$VERI_DIZINI" 2>/dev/null | cut -f1)"
    if [ -d "$VERI_DIZINI/recordings" ]; then
        SAYI="$(find "$VERI_DIZINI/recordings" -maxdepth 1 -name '*.sqrx' 2>/dev/null | wc -l)"
        bilgi "kayıt sayısı (.sqrx): $SAYI"
    fi
else
    hata "veri dizini yok: $VERI_DIZINI"
fi

# --------------------------------------------------------------- 8. unit durumu
bolum "8. systemd unit durumu"
if var_mi systemctl; then
    DURUM="$(systemctl is-active "$BIRIM" 2>/dev/null)"
    ETKIN="$(systemctl is-enabled "$BIRIM" 2>/dev/null)"
    if [ "$DURUM" = "active" ]; then
        tamam "$BIRIM etkin çalışıyor (is-active=$DURUM, is-enabled=$ETKIN)"
    else
        hata "$BIRIM çalışmıyor (is-active=${DURUM:-?}, is-enabled=${ETKIN:-?}). journalctl -u $BIRIM -n 50"
    fi
    if [ "$PORT" = "7788" ] && [ "$ETKIN" = "enabled" ] && [ "$IKINCI_SUNUCU_ACIK" != "1" ]; then
        hata "sqreader@7788 ETKİNLEŞTİRİLMİŞ. İkinci sunucu yalnız bilinçli kararla açılır; öyleyse IKINCI_SUNUCU_ACIK=1 ile çalıştırın, değilse: systemctl disable --now sqreader@7788"
    fi
    if systemctl cat "$BIRIM" >/dev/null 2>&1; then
        if systemctl cat "$BIRIM" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -qE -e '--pid|pidof'; then
            hata "unit komutunda --pid/pidof var: PID sabitlenemez"
        else
            tamam "unit'te --pid / pidof yok"
        fi
        MPID="$(systemctl show -p MainPID --value "$BIRIM" 2>/dev/null)"
        NRS="$(systemctl show -p NRestarts --value "$BIRIM" 2>/dev/null)"
        bilgi "MainPID=${MPID:-?} NRestarts=${NRS:-?}"
        if [ -n "${MPID:-}" ] && [ "$MPID" != "0" ] && [ -r "/proc/$MPID/status" ]; then
            bilgi "servis yetenekleri: $(grep -E '^(CapEff|CapBnd|NoNewPrivs)' "/proc/$MPID/status" | tr '\n' ' ')"
            bilgi "(CapEff 0000000000080004 = yalnız CAP_SYS_PTRACE + CAP_DAC_READ_SEARCH)"
        fi
    else
        hata "unit dosyası yüklenmemiş: $BIRIM (KURULUM-TR.md bölüm 6)"
    fi
    if var_mi systemd-analyze; then
        VERIFY="$(systemd-analyze verify "$BIRIM" 2>&1)"
        if [ -z "$VERIFY" ]; then
            tamam "systemd-analyze verify $BIRIM: temiz"
        else
            hata "systemd-analyze verify çıktı verdi: $(printf '%s' "$VERIFY" | head -n3 | tr '\n' ' ')"
        fi
        SKOR="$(systemd-analyze security "$BIRIM" --no-pager 2>/dev/null | tail -n1)"
        [ -n "$SKOR" ] && bilgi "systemd-analyze security: $SKOR"
    else
        atlandi "systemd-analyze verify: systemd-analyze yok"
    fi
else
    atlandi "unit durumu: systemctl yok"
fi

# --------------------------------------------------- 9. Export API info / health
bolum "9. Export API (127.0.0.1)"
HTTP_PORT=""
if [ -r "$ENV_DOSYASI" ]; then
    HTTP_PORT="$(sed -n 's/^SQREADER_HTTP_PORT=\([0-9]\{1,5\}\)$/\1/p' "$ENV_DOSYASI" | head -n1)"
fi
if [ -z "$HTTP_PORT" ]; then
    hata "HTTP portu okunamadı: $ENV_DOSYASI içinde SQREADER_HTTP_PORT=<sayı> bekleniyor"
elif ! var_mi curl; then
    atlandi "Export API: curl yok"
elif [ ! -r "$TOKEN_DOSYASI" ]; then
    hata "token dosyası okunamıyor: $TOKEN_DOSYASI (yetki ya da dosya yok; root ile çalıştırın)"
else
    TDURUM="$(stat -c '%a %U' "$TOKEN_DOSYASI" 2>/dev/null)"
    if [ "$TDURUM" = "600 $KULLANICI" ]; then
        tamam "token dosyası izinleri: $TDURUM"
    else
        hata "token dosyası izni/sahibi beklenen '600 $KULLANICI' değil: '$TDURUM'"
    fi
    TOKEN="$(tr -d '\r\n' < "$TOKEN_DOSYASI" 2>/dev/null)"
    if [ "${#TOKEN}" -lt 32 ]; then
        hata "token 32 karakterden kısa ya da boş (Export API kapalı kalır); değer gösterilmez"
    else
        tamam "token uzunluğu yeterli (değer gösterilmez)"
        TABAN="http://127.0.0.1:$HTTP_PORT/export/v1"
        # Token komut satırında görünmesin: curl yapılandırması stdin'den (printf yerleşiktir).
        istek() {
            printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
                | curl -sS -K - --max-time 20 -w '\n%{http_code}' "$TABAN/$1" 2>&1
        }
        # Kimliksiz istek reddedilmeli.
        KOD="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$TABAN/info" 2>/dev/null)"
        case "$KOD" in
            401) tamam "kimliksiz istek reddedildi (401)" ;;
            404) hata "Export API kapalı görünüyor (kimliksiz istek 404): token dosyası boş/kısa ya da reader token'ı okuyamadı" ;;
            000|"") hata "reader'a bağlanılamadı: 127.0.0.1:$HTTP_PORT (servis çalışmıyor olabilir)" ;;
            *) hata "kimliksiz istek beklenmeyen kod verdi: $KOD (401 bekleniyordu)" ;;
        esac
        if [ "$KOD" = "401" ]; then
            YANIT="$(istek info)"; YK="${YANIT##*$'\n'}"; GOVDE="${YANIT%$'\n'*}"
            if [ "$YK" = "200" ]; then
                RS="$(printf '%s' "$GOVDE" | json_stdin_alan readerState)"
                SVID="$(printf '%s' "$GOVDE" | json_stdin_alan serverId)"
                SPORT="$(printf '%s' "$GOVDE" | json_stdin_alan squadPort)"
                CID="$(printf '%s' "$GOVDE" | json_stdin_alan containerId)"
                RV="$(printf '%s' "$GOVDE" | json_stdin_alan readerVersion)"
                tamam "GET /export/v1/info: serverId=$SVID squadPort=$SPORT readerState=$RS readerVersion=$RV"
                bilgi "containerId=${CID:0:12}$([ "${#CID}" -gt 12 ] && echo '...')"
                CFG_SID="$(printf '%s' "$SID" | tr -d '"')"
                if [ "$SVID" = "$CFG_SID" ]; then
                    tamam "serverId config ile aynı ($SVID)"
                else
                    hata "serverId ($SVID) config server_id ($CFG_SID) ile farklı"
                fi
                case "$RS" in
                    ok) tamam "readerState=ok" ;;
                    *) hata "readerState=$RS (ok bekleniyordu: waiting_for_game / container_mismatch / attach_failed / stale / unknown reader'ın oyunu okuyamadığını gösterir)" ;;
                esac
            else
                hata "GET /export/v1/info kod=$YK"
            fi
            YANIT="$(istek health)"; YK="${YANIT##*$'\n'}"; GOVDE="${YANIT%$'\n'*}"
            if [ "$YK" = "200" ]; then
                OKV="$(printf '%s' "$GOVDE" | json_stdin_alan ok)"
                SDB="$(printf '%s' "$GOVDE" | json_stdin_alan statsDbReadable)"
                RDR="$(printf '%s' "$GOVDE" | json_stdin_alan recordingsDirReadable)"
                LFA="$(printf '%s' "$GOVDE" | json_stdin_alan lastFinalizedAt)"
                if [ "$OKV" = "true" ] || [ "$OKV" = "True" ]; then
                    tamam "GET /export/v1/health: ok statsDbReadable=$SDB recordingsDirReadable=$RDR lastFinalizedAt=$LFA"
                else
                    hata "GET /export/v1/health ok=$OKV statsDbReadable=$SDB recordingsDirReadable=$RDR"
                fi
            else
                hata "GET /export/v1/health kod=$YK"
            fi
        else
            atlandi "info/health: kimlik denetimi geçmediği için istenmedi"
        fi
    fi
    unset TOKEN
fi

# ------------------------------------------------------- 10. dış ağ politikası
bolum "10. Dış ağ politikası"
if [ -r "$CONFIG" ]; then
    NP="$(json_alan "$CONFIG" net_policy)"
    case "$NP" in
        '"local-only"') tamam "config net_policy=local-only" ;;
        __YOK__) atlandi "config'te net_policy yok (bekleyen anahtar; unit yine SQREADER_NET_POLICY=local-only verir)" ;;
        __OKUNAMADI__) atlandi "config net_policy okunamadı (JSON ayrıştırılamadı ya da python yok)" ;;
        *) hata "config net_policy beklenen 'local-only' değil: $NP" ;;
    esac
    NAH="$(json_alan "$CONFIG" net_allow_hosts)"
    case "$NAH" in
        "[]") tamam "config net_allow_hosts boş" ;;
        __YOK__) atlandi "config'te net_allow_hosts yok (bekleyen anahtar)" ;;
        __OKUNAMADI__) atlandi "config net_allow_hosts okunamadı" ;;
        *) hata "net_allow_hosts boş değil: $NAH (çalışma zamanında dış ağ yok ilkesi)" ;;
    esac
    for anahtar in push_enabled update_enabled offset_autoheal; do
        DEG="$(json_alan "$CONFIG" "$anahtar")"
        case "$DEG" in
            false) tamam "config $anahtar=false" ;;
            __OKUNAMADI__) atlandi "config $anahtar okunamadı" ;;
            __YOK__) atlandi "config'te $anahtar yok (kodun varsayılanı geçerli: bkz. sqreader/config.py; açıkça false yazın)" ;;
            *) hata "config $anahtar=$DEG (false bekleniyordu)" ;;
        esac
    done
    for anahtar in central_url alert_webhook alert_replay_base; do
        DEG="$(json_alan "$CONFIG" "$anahtar")"
        case "$DEG" in
            null) tamam "config $anahtar=null" ;;
            __OKUNAMADI__) atlandi "config $anahtar okunamadı" ;;
            __YOK__) atlandi "config'te $anahtar yok (varsayılan kullanılır; açıkça null yazın)" ;;
            *) hata "config $anahtar boş değil (dış hedef): değer gösterilmez" ;;
        esac
    done
else
    atlandi "config okunamadı: $CONFIG"
fi
if ! var_mi systemctl; then
    atlandi "unit ortamı: systemctl yok"
elif systemctl show -p Environment --value "$BIRIM" 2>/dev/null | grep -q 'SQREADER_NET_POLICY=local-only'; then
    tamam "unit ortamı: SQREADER_NET_POLICY=local-only"
else
    hata "unit ortamında SQREADER_NET_POLICY=local-only görünmüyor"
fi
if [ -n "$HTTP_PORT" ] && var_mi ss; then
    DINLEME="$(ss -H -ltn "sport = :$HTTP_PORT" 2>/dev/null | awk '{print $4}')"
    if [ -z "$DINLEME" ]; then
        hata "127.0.0.1:$HTTP_PORT dinlenmiyor (reader çalışmıyor olabilir)"
    else
        KOTU=""
        while IFS= read -r adres; do
            [ -n "$adres" ] || continue
            case "$adres" in
                127.0.0.1:*|"[::1]:"*) : ;;
                *) KOTU="$KOTU $adres" ;;
            esac
        done <<< "$DINLEME"
        if [ -z "$KOTU" ]; then
            tamam "reader yalnız loopback'te dinliyor: $(printf '%s' "$DINLEME" | paste -sd' ' -)"
        else
            hata "reader loopback dışında dinliyor:$KOTU (yalnız 127.0.0.1 olmalı)"
        fi
    fi
elif [ -n "$HTTP_PORT" ]; then
    atlandi "dinleme adresi: ss yok"
fi
if var_mi ss && var_mi systemctl; then
    MPID2="$(systemctl show -p MainPID --value "$BIRIM" 2>/dev/null)"
    if [ -n "${MPID2:-}" ] && [ "$MPID2" != "0" ]; then
        if [ "$(id -u)" -ne 0 ]; then
            atlandi "dış bağlantı taraması: root gerekli (ss -p)"
        else
            DIS=""
            while IFS= read -r satir; do
                case "$satir" in *"pid=$MPID2,"*) ;; *) continue ;; esac
                uzak="$(printf '%s' "$satir" | awk '{print $4}')"
                ip="${uzak%:*}"
                ozel_adres "$ip" || DIS="$DIS $uzak"
            done < <(ss -H -tnp state established 2>/dev/null)
            if [ -z "$DIS" ]; then
                tamam "reader'ın (pid $MPID2) yerel-dışı kurulu bağlantısı yok (ANLIK görüntü; süreklilik kanıtı değil)"
            else
                hata "reader'ın özel ağ dışına kurulu bağlantısı var:$DIS"
            fi
        fi
    else
        atlandi "dış bağlantı taraması: servis çalışmıyor"
    fi
fi
if var_mi journalctl; then
    ENG="$(journalctl -u "$BIRIM" --since '-24h' --no-pager 2>/dev/null | grep -c 'dış ağ çağrısı engellendi' || true)"
    bilgi "son 24 saatte engellenen dış ağ çağrısı girişimi (log satırı): ${ENG:-0} (0 beklenir; >0 ise bir kod yolu dışarı çıkmayı denemiş)"
fi

# ------------------------------------------------------------------------ özet
echo
echo "== Özet =="
echo "  TAMAM: $TAMAM_N   HATA: $HATA_N   ATLANDI: $ATLANDI_N"
if [ "$HATA_N" -gt 0 ]; then
    echo "  Başarısız adımlar:"
    for m in "${HATA_LISTESI[@]}"; do echo "    - $m"; done
fi
if [ "$ATLANDI_N" -gt 0 ]; then
    echo "  Atlanan adımlar:"
    for m in "${ATLANDI_LISTESI[@]}"; do echo "    - $m"; done
fi
echo "  Bu betik salt-okunurdur; hiçbir şeyi değiştirmedi ve hiçbir süreci durdurmadı."
[ "$HATA_N" -eq 0 ]
exit $?
