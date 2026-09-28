# Zero Zone: dış URL / host envanteri ve ağ politikası

Bu fork, squadreader.com'a ya da başka bir dış servise ÇALIŞMA ZAMANINDA bağımlı
olmamalıdır. Bu belge (1) ağ politikasını, (2) depodaki tüm dış URL/host'ların
envanterini ve (3) envanteri yeniden üretme komutlarını içerir. Kurulum zamanı
paket indirmeleri (pip/npm) kapsam dışıdır.

## 1. Ağ politikası (`sqreader/netpolicy.py`)

| Düzey | Anlamı |
|---|---|
| `local-only` (VARSAYILAN) | Yalnız loopback (127.0.0.0/8, `::1`, `localhost`), RFC1918, CGNAT (100.64.0.0/10) ve fc00::/7 hedeflerine izin verilir. Alan adları yalnız `allow_hosts` listesinde birebir varsa geçer (alt alan adı kendiliğinden geçmez). Denetim soket açılmadan ve DNS çözümlemesi yapılmadan önce, yalnızca metin üzerinden yapılır. URL'de kullanıcı adı/parola varsa reddedilir. Yönlendirmeler (redirect) de aynı denetimden geçer; ortam proxy'leri yok sayılır. |
| `allow-central` | Upstream davranışı. Hiçbir şey engellenmez. Yalnız açıkça seçilirse. |

Kaynak sırası (güçlüden zayıfa): fonksiyon parametresi, ortam değişkeni
`SQREADER_NET_POLICY` (`local-only` / `allow-central`), `netpolicy.configure(...)`
(config dosyası), yerleşik varsayılan. `allow_hosts`: `SQREADER_NET_ALLOW_HOSTS`
(virgüllü) > `configure(allow_hosts=...)`. Geçersiz ortam değeri `local-only`'ye
düşer (güvenli taraf).

Engellenen her girişim tek satır loglanır (amaç + host; yol, sorgu dizesi, token,
parola yok) ve süreç içi sayaçta tutulur: `netpolicy.blocked_attempts()`,
`netpolicy.status()`.

`local-only`'de davranış özeti:

| Yetenek | Davranış |
|---|---|
| Kayıt (`enroll`) | Ağa çıkmaz; `PushBlocked` ("merkezi gönderim kapalı (yerel mod): enroll"), çıkış kodu 1. |
| Maç/replay gönderimi (push) | Gönderilmez; okuyucu çökmez, kayıt ve istatistik etkilenmez. `flush_backlog` `{"blocked": 1, ...}` döner, kuyruk işaretleri dokunulmadan kalır, yeniden deneme/bekleme yok. Durum: "merkezi gönderim kapalı (yerel mod)". |
| Check-in / telemetri | Gönderilmez (`{}` döner). |
| Uzaktan offset onarımı | Aranmaz; uyumsuz sürümde "kayıt durur, görünür hata" davranışı aynen korunur. |
| Güncelleme denetimi / indirme | Yapılmaz. |
| Bildirim webhook'u (Discord vb.) | Engellenir (alan adı izin listesinde değilse); yeniden deneme yok. Uyarı veritabanına yine yazılır. |
| Uyarıdaki "replayi izle" bağlantısı | Taban adres izinli değilse üretilmez (squadreader.com üretilmez). |

## 2. Envanter

Tablo `git ls-files` üzerinden taranmıştır (bkz. Bölüm 3). `local-only` durumu:
**engellenir** = kod yolu vardır, `check_outbound` ile kesilir; **hiç çağrılmaz** =
çalışma zamanında kod yolu yoktur; **kurulum/derleme, kapsam dışı**.

### 2.1 Python çalışma zamanı (dış ağa çıkabilen kod)

| dosya:satır | URL/host | ne zaman kullanılır | `local-only`'de durum | not |
|---|---|---|---|---|
| `sqreader/ingest_client.py:171` | `<central>/api/enroll` | çalışma zamanı (`sqreader enroll`) | engellenir (`PushBlocked`) | kayıt jetonu değişimi |
| `sqreader/ingest_client.py:189` | `<push_url>/ingest/match` | çalışma zamanı (push) | engellenir | `flush_backlog` başta keser |
| `sqreader/ingest_client.py:293` | `<push_url>/ingest/replay` | çalışma zamanı (push, `push_replay`) | engellenir | |
| `sqreader/ingest_client.py:210` | `<push_url>/agent/checkin` | çalışma zamanı (telemetri) | engellenir, sessiz `{}` | |
| `sqreader/ingest_client.py:227` | `<push_url>/agent/offsets` | çalışma zamanı (offset onarımı) | engellenir, sessiz `None` | `offset_client.fetch_and_accept` da başta keser |
| `sqreader/ingest_client.py:244` | `<push_url>/agent/manifest` | çalışma zamanı (güncelleme denetimi) | engellenir, sessiz `None` | `updater.fetch_and_accept` da başta keser |
| `sqreader/ingest_client.py:273` | `<push_url>/agent/download?file=` | çalışma zamanı (güncelleme indirme) | engellenir, sessiz `None` | `updater.download_verified` da başta keser |
| `sqreader/ingest_client.py:473-490` | (tüm yukarıdakilerin ortak çıkış noktası `_send`) | çalışma zamanı | `netpolicy.guarded_urlopen`: istek ve yönlendirmeler denetlenir | `allow-central`'de eski `urllib.request.urlopen` |
| `sqreader/plugins/notify.py:334,351` | `alert_webhook` (Discord webhook) | çalışma zamanı (uyarı bildirimi) | engellenir (izin listesindeki host'a gider) | URL bir kimlik bilgisidir; loglanmaz |
| `sqreader/plugins/notify.py:79-95` | `alert_replay_base` (örnek: `https://squadreader.com`) + `/replay/?mode=replay&id=` | çalışma zamanı (bağlantı metni üretimi, ağ çağrısı yok) | taban izinli değilse bağlantı üretilmez | |
| `sqreader/plugins/notify.py:109` | `squadreader.com` (yalnız alt bilgi METNİ) | çalışma zamanı (yalnız metin, bağlantı değil) | webhook engellendiği için hiç gönderilmez | webhook izinliyse metin gider; istenirse ayrıca sadeleştirilebilir |
| `sqreader/squad/metadata.py:4,11-14` | `cota.squadreplay.com/*` | yalnız belge (docstring) | hiç çağrılmaz | doğrulandı: yerel. Modül yalnız `data/static/*.json` okur; `urllib`/`socket` içermez |
| `sqreader/build_config.py:20` | `https://squadreader.com` (örnek, `BAKED_CENTRAL_URL` boş) | yalnız belge / paketleme zamanı | hiç çağrılmaz | kaynak ağaçta `LOCKED=False` |
| `sqreader/config.py` (`alert_replay_base` açıklaması) | `https://squadreader.com` | yalnız belge (yorum) | hiç çağrılmaz | |
| `sqreader.config.example.json:35` | `"alert_replay_base": "https://squadreader.com"` | yalnız örnek yapılandırma | `local-only`'de bağlantı üretilmez | ŞEF: `null` yapılmalı (Bölüm 4) |
| `sqreader.config.example.json:52` | `https://your-central` | yalnız belge | hiç çağrılmaz | |

Sunucu tarafı dinleyiciler (`sqreader/httpsrv.py`, `sqreader/export_api.py`) dışarı
bağlantı açmaz; yalnız dinler. `sqreader/fleet.py` yalnız veri derler (gönderim
`ingest_client.checkin` ile).

### 2.2 Elle çalıştırılan betikler (`scripts/`, çalışma zamanı değil)

| dosya:satır | URL/host | ne zaman kullanılır | `local-only`'de durum | not |
|---|---|---|---|---|
| `scripts/fetch_capzones.py:43,59-65` | `https://squadcalc.app/api/get/layer` | geliştirme (haritalar değişince elle) | engellenir (`capzone-fetch`); `SQREADER_NET_POLICY=allow-central` ile çalışır | önbellekteki katmanlar ağsız işlenir; `data/static/capzones.json` depoda hazırdır |
| `scripts/dump_zz_golden.py:60` | `http://127.0.0.1:<port>` | geliştirme (golden üretimi) | loopback, izinli | |
| `scripts/rcon_diff.py:48` | `socket.create_connection((host, port))` (parametre) | geliştirme (RCON karşılaştırma aracı) | politikaya bağlı DEĞİL; elle, oyun sunucusuna | çalışma zamanına dahil değil |

### 2.3 Kurulum / paketleme / CI (kapsam dışı)

| dosya:satır | URL/host | ne zaman kullanılır | `local-only`'de durum | not |
|---|---|---|---|---|
| `packaging/install.sh:28` | `CENTRAL=${SQREADER_CENTRAL:-https://squadreader.com}` | kurulum zamanı | kurulum zamanı, kapsam dışı | Zero Zone kurulumunda `SQREADER_CENTRAL` verilmemeli/betik kullanılmamalı |
| `packaging/install.sh:168,175,199` | `$CENTRAL/agent/download?file=` (curl) | kurulum zamanı (ikili indirme) | kurulum zamanı, kapsam dışı | |
| `packaging/entitlements.env.example:13` | `CENTRAL_URL=https://squadreader.com` | derleme zamanı | kapsam dışı | |
| `.github/workflows/build-agent.yml:39` | `vars.CENTRAL_URL \|\| 'https://squadreader.com'` | CI derleme zamanı | kapsam dışı | |
| `README.md:29,127` | `squadreader.com/replays`, `curl ... squadreader.com/install.sh` | yalnız belge | hiç çağrılmaz | upstream tanıtım metni |
| `NOTICE:30` | `squadreplay.com`, `squadcalc.app` | yalnız belge | hiç çağrılmaz | `data/static/` türev veri, lisans notu |
| `pyproject.toml:40-43`, `CONTRIBUTING.md:8`, `README.md:57`, `SECURITY.md:6`, `CHANGELOG.md:4-5`, `CODE_OF_CONDUCT.md:34`, `.github/PULL_REQUEST_TEMPLATE.md:13` | github.com/cagrianilokumus/squadreader, keepachangelog.com, semver.org, developercertificate.org, contributor-covenant.org | yalnız belge / üst veri | hiç çağrılmaz | |
| `deploy/nginx_reverse-proxy.example.conf` | yalnız `127.0.0.1` `proxy_pass` ve kendi `$host` yönlendirmesi | yalnız örnek yapılandırma | hiç dış host yok | |

### 2.4 Frontend (`frontend/`)

| dosya:satır | URL/host | ne zaman kullanılır | `local-only`'de durum | not |
|---|---|---|---|---|
| `frontend/index.html:5` (ve derlenmiş `frontend/dist/index.html:5`) | `https://www.googletagmanager.com/gtag/js?id=G-D0XZRE5LLP` (Google Analytics) | yalnız upstream viewer (tarayıcı, sayfa açılışında) | Zero Zone viewer build'inde YOK; upstream build tarayıcıdan yükler (sunucu politikası kapsamaz) | upstream build'i Zero Zone'da SERVİS ETMEYİN |
| `frontend/src/ui/PlayerStats.tsx:300` (derlenmiş `frontend/dist/assets/index-B7Owi6nE.js:61`) | `https://discord.gg/3mQytyAwJd` | yalnız upstream viewer (bağlantı; tıklanınca) | Zero Zone build'inde YOK | |
| `frontend/src/ui/PlayerStats.tsx:269,388` | `/logo.svg`, `squadreader.com` (metin) | yalnız upstream viewer (site modu) | aynı kökene göreli / yalnız metin | |
| `frontend/src/**` (yorumlar: `App.tsx:158`, `main.tsx:8,14`, `theme-*.css`) | `squadreader.com` | yalnız belge (yorum) | hiç çağrılmaz | |
| `frontend/vite.config.ts:8` | `http://127.0.0.1:8080` (geliştirme proxy) | geliştirme | loopback | |
| `frontend/dist-zerozone/` (Zero Zone build) | dış host YOK (yalnız React/zustand hata metinlerindeki belge bağlantıları: `reactjs.org/docs/error-decoder.html`, `github.com/pmndrs/zustand/...`; çağrılmaz) | Zero Zone viewer | temiz | tarama Bölüm 3.3 |

## 3. Yeniden üretme komutları

Depo kökünde (`squadreader/`), bash:

### 3.1 Metin taraması (tüm izlenen dosyalar)

```bash
git ls-files -z | grep -zv -E '^(frontend/package-lock\.json|sqmaps/|icons/|tests/fixtures/)' \
  | xargs -0 grep -nIE '(https?|wss?|ftp)://[^"'"'"' )>`\\<]+|(squadreader\.com|squadreplay\.com|squadcalc\.app|discord\.gg|googletagmanager|google-analytics)' \
  | grep -vE '127\.0\.0\.1|localhost|w3\.org|0\.0\.0\.0'
```

### 3.2 Ağa çıkan kod yolları

```bash
grep -rnE "urllib\.request|http\.client|import requests|socket\.create_connection|websocket|aiohttp|httpx|ftplib|smtplib|urlopen" \
  --include=*.py sqreader scripts
grep -rnE "curl|wget|git (clone|fetch|pull)" packaging deploy .github
```

Beklenen: `sqreader/` altında ağa çıkan tek modüller `ingest_client.py` (`_send` ->
`netpolicy.guarded_urlopen`), `plugins/notify.py` ve `netpolicy.py`
(`guarded_urlopen`); diğer `urllib` kullanımları yalnız `urllib.parse` (ayrıştırma).
Yeni bir modül çıkarsa `netpolicy.check_outbound(...)` eklenmeli ve bu tablo
güncellenmeli.

### 3.3 Frontend derlenmiş çıktıları

```bash
grep -rnoIE "(https?:)?//[a-zA-Z0-9.-]+\.[a-z]{2,}[^\"' )>\`\\\\]{0,60}" frontend/dist-zerozone \
  | grep -vE "w3\.org|localhost"          # yalnız React/zustand hata metni bağlantıları beklenir
grep -rnoIE "googletagmanager|discord\.gg" frontend/dist-zerozone   # boş olmalı
```

### 3.4 Testler

```bash
python -m pytest tests/test_netpolicy.py -q
```

Mevcut push/güncelleme/offset/bildirim testleri upstream davranışını sahte merkez
adresleriyle (`c.test`, `central.example`) sınadığı için `tests/conftest.py` bunları
`SQREADER_NET_POLICY=allow-central` altında çalıştırır; `test_netpolicy.py` her testte
varsayılana döner.

## 4. Yapılandırma (config/CLI) notları

Bu görevde `config.py`/`cli.py`/örnek config'e dokunulmadı; gerekli değişiklikler
şef tarafından ayrı görevle yapılacaktır (görev raporunda "şefin yaptıracağı
değişiklikler" olarak listelenmiştir): `net_policy` ve `net_allow_hosts` anahtarları,
`netpolicy.configure(...)` çağrısı, `update_enabled` / `offset_autoheal` varsayılanlarının
`false` olması, örnek config'te `alert_replay_base: null`.
