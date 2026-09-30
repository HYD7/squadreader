#!/usr/bin/env bash
# sqreader-kur.sh - SquadReader deposundan başlayarak Ubuntu kurulumu (oyun sunucusu makinesi için).
#
# Ana orkestrasyon Zero Zone deposundadır (deploy/ubuntu/kur.sh); bu betik yalnız onu bulur ve
# `--yalniz squadreader --sr-kaynak <bu depo>` ile çağırır. Yani SR'ye özgü mantık BURADA tekrarlanmaz.
#
# Kullanım (root ile):
#   sudo bash deploy/ubuntu/sqreader-kur.sh --kuru-kosu                       # önce: adımları gör
#   sudo bash deploy/ubuntu/sqreader-kur.sh --evet                            # ZZ deposu ../zerozone-squadjs'te ise
#   sudo bash deploy/ubuntu/sqreader-kur.sh --zz-deposu /yol/zerozone-squadjs --evet
#   sudo bash deploy/ubuntu/sqreader-kur.sh --zz-url <ZZ_GIT_URL> --zz-referans <SHA> --evet
#
# Diğer tüm seçenekler kur.sh'ye aynen geçer (--sr-port, --sr-sha, --retention-ac, --yalniz squadreader,nginx
# --export-alan-adi ... --zz-ip ... vb.). Tam liste: kur.sh --yardim.
#
# Ayrı makine düzeni: SR bu makinede, Zero Zone başka makinede ise export API'yi TLS + IP izin listesiyle açmak için
#   ... --yalniz squadreader,nginx --export-alan-adi <ALAN> --export-sertifika <DIZIN> --zz-ip <ZZ_WEB_SUNUCUSU_IP>
# (nginx-export.conf.example'dan türetilir; ayrıntı: Zero Zone deposunda deploy/ubuntu/KURULUM-UBUNTU-TR.md).
#
# DOĞRULANMADI: Windows'ta yazıldı; Ubuntu'da ÇALIŞTIRILMADI.

set -euo pipefail

SR_KOK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ZZ_DEPO=""
ZZ_URL=""
ZZ_REF=""
GECERLI=()
GECICI=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --zz-deposu) [ "$#" -ge 2 ] || { echo "HATA: --zz-deposu için değer yok" >&2; exit 2; }; ZZ_DEPO="$2"; shift 2 ;;
        --zz-url) [ "$#" -ge 2 ] || { echo "HATA: --zz-url için değer yok" >&2; exit 2; }; ZZ_URL="$2"; shift 2 ;;
        --zz-referans) [ "$#" -ge 2 ] || { echo "HATA: --zz-referans için değer yok" >&2; exit 2; }; ZZ_REF="$2"; shift 2 ;;
        -h|--yardim|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) GECERLI+=("$1"); shift ;;
    esac
done

temizle() { if [ -n "$GECICI" ]; then rm -rf -- "${GECICI:?}"; fi; }
trap temizle EXIT

if [ -z "$ZZ_DEPO" ] && [ -n "$ZZ_URL" ]; then
    [ -n "$ZZ_REF" ] || { echo "HATA: --zz-url ile --zz-referans (SHA/etiket) da verilmeli" >&2; exit 2; }
    GECICI="$(mktemp -d)"
    git clone --no-checkout "$ZZ_URL" "$GECICI/zz"
    git -C "$GECICI/zz" switch --detach "$ZZ_REF"
    ZZ_DEPO="$GECICI/zz"
fi
if [ -z "$ZZ_DEPO" ] && [ -d "$SR_KOK/../zerozone-squadjs/deploy/ubuntu" ]; then
    ZZ_DEPO="$(cd "$SR_KOK/../zerozone-squadjs" && pwd)"
fi
if [ -z "$ZZ_DEPO" ] || [ ! -r "$ZZ_DEPO/deploy/ubuntu/kur.sh" ]; then
    echo "HATA: Zero Zone deposu (deploy/ubuntu/kur.sh) bulunamadı. --zz-deposu DIZIN ya da --zz-url URL --zz-referans SHA verin." >&2
    exit 2
fi

echo "kur.sh: $ZZ_DEPO/deploy/ubuntu/kur.sh  (SR kaynağı: $SR_KOK)"
# exec KULLANILMAZ: geçici klon çıkışta silinsin (trap).
if [ "${#GECERLI[@]}" -gt 0 ]; then
    bash "$ZZ_DEPO/deploy/ubuntu/kur.sh" --yalniz squadreader --sr-kaynak "$SR_KOK" "${GECERLI[@]}"
else
    bash "$ZZ_DEPO/deploy/ubuntu/kur.sh" --yalniz squadreader --sr-kaynak "$SR_KOK"
fi
