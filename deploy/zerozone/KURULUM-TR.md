# SquadReader - Zero Zone kurulum ve işletme rehberi (Linux host)

> **Ubuntu tek komut kurulumu için bkz. `deploy/ubuntu/sqreader-kur.sh` ve Zero Zone deposundaki `deploy/ubuntu/KURULUM-UBUNTU-TR.md`.** Bu rehber adımları elle anlatır; tek komut betiği aynı unit/config dosyalarını kullanır.

Bu rehber, SquadReader okuyucusunu (Zero Zone çatalı) oyun sunucusuyla aynı Linux
makinede, ayrı bir sistem kullanıcısıyla ve asgari yetkiyle çalıştırmak içindir.
Okuyucu yalnız **okur**: oyun sürecinin belleğini ve logunu izler, biten maçları kaydeder,
kendi SQLite veritabanına istatistik yazar ve Zero Zone'un çektiği salt-okunur bir
**Export API v1** sunar.

> **Nasıl okunmalı.** Bu rehber ve yanındaki dosyalar bir Windows geliştirme makinesinde
> yazıldı. systemd, nginx, gerçek oyun süreci ve Linux yetenek (capability) davranışı
> **burada çalıştırılmadı**. Doğrulanmayan her nokta "DOĞRULANMADI" ya da
> "çalıştırılmadı: Linux host gerekli" diye işaretlidir; tam liste bölüm 18'de.
> Komutlardaki `<...>` yer tutucudur: gerçek değerleri siz yazarsınız.
>
> **Zero Zone ile birlikte doğrulama.** Canlıda yapılacak doğrulamaların TAMAMI (Linux host, ağ,
> veritabanı, veri anlamı, site, geçiş provası) Zero Zone deposunda
> `docs/squadreader/CANLI-DOGRULAMA.md` içinde toplanmıştır; bu rehberdeki `dogrula.sh`
> bölümlerinin o listeyle eşlemesi bölüm 18.1'dedir. Hedef kurulumda SquadReader Zero Zone'un
> TEK istatistik ve EXP kaynağıdır ve **yedek motor yoktur** (bölüm 9): okuyucu durursa istatistik
> ve EXP üretilmez.

## İçindekiler

1. Kapsam ve değişmez ilkeler
2. Ön koşullar
3. Kaynaktan kurulum
4. Kullanıcı, dizinler, izinler
5. Token üretimi ve config
6. Unit kurulumu ve yalnız 7787'nin etkinleştirilmesi
7. Asgari yetkiler, sertleştirme ve NELERİN yapılmadığı
8. Doğrulama (`dogrula.sh`) ve geçici teşhis
9. Uyumsuz oyun sürümünde beklenen davranış
10. Zero Zone ile bağlantı (ağ yolu, token aktarımı)
11. Konteyner yeniden oluşturma ve oyun güncellemesi
12. İkinci sunucu (7788)
13. Kayıt saklama (retention)
14. CPU / RAM / disk etkisini ölçme
15. Yedekleme ve geri alma
16. Kaldırma
17. Yapılmayanlar (özet)
18. "Çalıştırılmadı" listesi
19. Lisans notu

---

## 1. Kapsam ve değişmez ilkeler

- Bir okuyucu örneği = bir oyun sunucusu = bir config = bir veri dizini = bir token.
  Örnek adı **oyun portudur**: `sqreader@7787`.
- **PID sabitlenmez.** Unit'te `--pid`/`pidof` yoktur. Okuyucu oyun sürecini her
  başlangıçta config'teki `squad_port`/`squad_query_port` (ve isteğe bağlı
  `expected_container_id`) ölçütleriyle yeniden bulur (`sqreader/discovery.py`).
  Birden fazla süreç eşleşirse **hiçbiri seçilmez**, okuyucu başlamayı reddeder.
- Çalışma zamanında **dış ağ yok**: merkezi gönderim, kayıt (enroll), telemetri,
  otomatik güncelleme, ofset kendini onarma, Discord bildirimi, dış "replay" bağlantısı
  kapalıdır (config + `SQREADER_NET_POLICY=local-only`).
- Okuyucunun HTTP sunucusu yalnız `127.0.0.1`e bağlanır. Dışarıya yalnız
  `/export/v1/` yolu, token'la (ve tercihen özel ağ / IP izin listesiyle) açılır.
- Okuyucunun yeniden başlaması oyun sunucusunu **etkilemez**. Unit oyunu/konteyneri
  yeniden başlatmaz, sinyallemez, durdurmaz; onunla bağımlılık ilişkisi kurmaz.

## 2. Ön koşullar

- Linux host, systemd, oyun sunucusu aynı makinede (konteynerde ya da değil).
- Python >= 3.10 (`pyproject.toml`: `requires-python`), `python3-venv`, `git`,
  `sqlite3` komut satırı aracı (yedek için), `curl`, `util-linux` (`setpriv`, `runuser`),
  `iproute2` (`ss`). Debian/Ubuntu örneği:

  ```bash
  sudo apt-get update
  sudo apt-get install -y git python3 python3-venv python3-pip sqlite3 curl util-linux iproute2
  ```

- Node.js **gerekmez**: replay görüntüleyici Zero Zone'da barındırılır; okuyucu yalnız veri
  sunar (frontend build'i bu makinede yapılmaz).
- Kurulum sırasında ağ gerekir (git clone + PyPI). Çalışma zamanında gerekmez (bölüm 3.4).
- Zero Zone çatalının adresi (`FORK_URL` = `https://github.com/HYD7/squadreader`; dal `orkestra/canli-izleme`,
  upstream `https://github.com/cagrianilokumus/squadreader`) ve kurulacak sabit commit
  (`<COMMIT_SHA>`, 40 karakter) elinizde olmalı.

## 3. Kaynaktan kurulum

### 3.1 Kaynağı sabit commit'le al

```bash
sudo install -d -m 0755 /opt/sqreader
sudo git clone https://github.com/HYD7/squadreader /opt/sqreader/src
sudo git -C /opt/sqreader/src checkout --detach <COMMIT_SHA>
sudo git -C /opt/sqreader/src rev-parse HEAD          # <COMMIT_SHA> ile AYNI olmalı
sudo git -C /opt/sqreader/src status --porcelain      # boş olmalı (yerel değişiklik yok)
```

Dal adına değil **commit SHA'ya** sabitlenir: aynı SHA her makinede aynı kaynağı verir.
Kaynak `root` sahibi kalır; `sqreader` kullanıcısı yalnız okur.

### 3.2 Sanal ortam ve kurulum

```bash
sudo python3 -m venv /opt/sqreader/venv
sudo /opt/sqreader/venv/bin/pip install --upgrade pip
sudo /opt/sqreader/venv/bin/pip install -e /opt/sqreader/src
```

**Neden `-e` (düzenlenebilir kurulum)?** Oyun meta verisi (`data/static/*.json`) pip
paketine dahil değildir (`pyproject.toml`: paketler yalnız `sqreader*`); `-e` kurulum kaynak
ağaçtan çalışır. Unit ayrıca `SQREADER_DATA_DIR=/opt/sqreader/src/data/static` verir
(`sqreader/squad/metadata.py`), yani veri dizini iki yoldan da bulunur.

### 3.3 Sürüm sabitleme

Depoda Python için kilit dosyası (lock) **yoktur**; `pyproject.toml` yalnız
`zstandard>=0.22,<1.0` aralığı ister. Tekrarlanabilirlik için kurulu sürümleri kaydedin:

```bash
/opt/sqreader/venv/bin/pip freeze --exclude-editable | sudo tee /opt/sqreader/requirements.kurulu.txt
```

Aynı ortamı yeniden kurmak için: `pip install -r /opt/sqreader/requirements.kurulu.txt`
ardından `pip install -e /opt/sqreader/src --no-deps`. (Karma/hash doğrulaması istenirse
`pip download` ile tekerlekleri (wheel) saklayıp `--require-hashes` kullanılabilir; bu
rehberde denenmedi.) İsteğe bağlı ek paketler (`[push]` = `cryptography`, `[fast]` =
`numpy`) **kurulmaz**: push kapalıdır, numpy yalnız hızlandırıcıdır.

### 3.4 Kurulum zamanı ile çalışma zamanı bağımlılıkları

| | Ne zaman | Ne | Gerekli mi |
|---|---|---|---|
| Kurulum zamanı | tek sefer | `apt` paketleri, `git clone https://github.com/HYD7/squadreader`, `pip install` (PyPI: `zstandard`) | evet, ağ gerekir |
| Çalışma zamanı | sürekli | **hiçbiri** | dış ağ **yok** |

Çalışma zamanı güvencesi üç katmandır: (1) config `net_policy: "local-only"` ve boş
`net_allow_hosts` (varsayılanları `sqreader/config.py` DEFAULTS'ta, `net_policy: "local-only"`,
`net_allow_hosts: []`; her komuttan önce `sqreader/cli.py` içinde `netpolicy.configure(...)`
ile uygulanır ve geçersiz değerde Türkçe hata verip çıkar; `SQREADER_NET_POLICY` /
`SQREADER_NET_ALLOW_HOSTS` ortam değişkenleri bu anahtarlara ÜSTÜN gelir), (2) unit'teki `SQREADER_NET_POLICY=local-only` ortam değişkeni
(`sqreader/netpolicy.py`), (3) isteğe bağlı güvenlik duvarı:

```bash
# ÖRNEK, SINANMADI (çalıştırılmadı: Linux host gerekli): sqreader kullanıcısının yalnız
# loopback ve özel ağlara çıkmasına izin verir.
sudo nft -f - <<'EOF'
table inet sqreader_egress {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid "sqreader" oifname "lo" accept
    meta skuid "sqreader" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 } accept
    meta skuid "sqreader" counter drop
  }
}
EOF
```

### 3.5 Kurulum denemesi

```bash
/opt/sqreader/venv/bin/sqreader version
/opt/sqreader/venv/bin/sqreader selftest
```

`selftest` çıktısında `sealed envelope` ve `release signatures` satırları `cryptography`
kurulu olmadığı için FAIL görünür; bu yerel modda **beklenir ve gerekmez** (merkezi
gönderim/güncelleme kapalı). `compression` ve `bundled metadata` `ok` olmalıdır.

## 4. Kullanıcı, dizinler, izinler

```bash
sudo useradd --system --home-dir /var/lib/sqreader --shell /usr/sbin/nologin sqreader
sudo install -d -m 0755 -o root -g root /etc/sqreader
sudo install -d -m 0750 -o sqreader -g sqreader /var/lib/sqreader
sudo install -d -m 0750 -o sqreader -g sqreader /var/lib/sqreader/7787
sudo install -d -m 0750 -o sqreader -g sqreader /var/lib/sqreader/7787/stats
sudo install -d -m 0750 -o sqreader -g sqreader /var/lib/sqreader/7787/recordings
```

Kural: sqreader kullanıcısı **docker grubunda değildir**, oyunun kullanıcısıyla aynı
kullanıcı da değildir. Veri dizini yalnız `sqreader`a aittir; oyun sunucusunun dosyalarına
yazma izni yoktur.

## 5. Token üretimi ve config

### 5.1 Token

Gerçek token hiçbir dosyada/depoda yoktur; makinede üretilir:

```bash
# /etc/sqreader root'a ait 0755 dizindir: sqreader kullanıcısı orada dosya OLUŞTURAMAZ
# (eski `sudo -u sqreader tee` komutu "Permission denied" verirdi). Dosyayı root
# oluşturur; umask 077 sayesinde daha ilk anda 0600'dür, sonra sahibi devredilir.
# Token komut satırında görünmez (openssl kabuğun içinde çalışır, çıktı dosyaya
# yönlenir) ve kabuk geçmişine sır girmez (komutun içinde token yoktur).
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/sqreader/7787.export-token'
sudo chown sqreader:sqreader /etc/sqreader/7787.export-token
sudo chmod 0600 /etc/sqreader/7787.export-token
sudo stat -c '%a %U:%G %n' /etc/sqreader/7787.export-token   # 600 sqreader:sqreader ...
sudo -u sqreader test -r /etc/sqreader/7787.export-token && echo "sqreader okuyabiliyor"
```

Aynı komut token'ı **döndürmek** için de kullanılır (dosyanın içeriğini yeniden yazar; bölüm 10.3).

`openssl rand -hex 32` 64 karakter üretir (en az 32 gerekir; kısa/boş dosya = API kapalı,
`/export/v1/*` 404). Token'ı ekrana yazdırmayın, kabuk geçmişine koymayın, sohbete/e-postaya
yapıştırmayın. Zero Zone'a aktarım bölüm 10.3'te.

### 5.2 Config

```bash
sudo install -m 0640 -o root -g sqreader \
  /opt/sqreader/src/deploy/zerozone/sqreader.7787.config.json.example \
  /etc/sqreader/7787.config.json
sudoedit /etc/sqreader/7787.config.json
# Dosya root:sqreader 0640: sıradan kullanıcı okuyamaz, bu yüzden sqreader olarak denetleyin.
sudo -u sqreader python3 -c "import json; json.load(open('/etc/sqreader/7787.config.json'))" && echo "JSON geçerli"
```

Örnekteki anahtarlar ve anlamları (hepsi `sqreader/config.py` DEFAULTS'ta ya da
`sqreader.config.example.json`'da vardır; "bekleyen" olanlar işaretlidir):

| Anahtar | Değer | Not |
|---|---|---|
| `server_id` | `zz-7787` | Veri bölümleme anahtarı. Başka `server_id`'nin verisi olan dizinde okuyucu **başlamaz** (`discovery.check_stats_db_server_id`). |
| `squad_port` / `squad_query_port` | `7787` / `null` | **Zero Zone kipinde (Export API açık) `squad_port`, `squad_query_port` ya da `expected_container_id`'den EN AZ BİRİ ZORUNLUDUR:** hiçbiri yoksa `serve` başlamayı reddeder (çıkış kodu 2, "hedef ölçütü yok"); reader 7788'e (henüz devredilmemiş sunucuya) "bulduğum tek süreç" diye bağlanmaz. `require_target` (`null` = Export API açıksa zorunlu; `true`/`false` zorlar) bunu değiştirir. Ayrıca unit'in `ExecStartPre=sqreader check-instance %i` adımı örnek adının (`sqreader@7787`) `squad_port` YA DA `squad_query_port` değerlerinden birine EŞİT olduğunu denetler (7787 `QueryPort` ise `squad_port: null` + `squad_query_port: 7787` geçerlidir); ikisi de yoksa ya da hiçbiri örnek adıyla eşleşmiyorsa servis başlamaz. Config dosyası VAR ama bozuksa (geçersiz JSON, okunamıyor) reader varsayılana dönmez, HATA verip çıkar (kod 2). 7787'nin oyunun `-Port=` mu `-QueryPort=` mu olduğu **DOĞRULANMADI**. `sudo ./dogrula.sh 7787` bölüm 3 bunu gösterir; `QueryPort` ise değerleri değiştirin. İkisi de doluysa ikisi de eşleşmeli. |
| `expected_container_id` | `null` | İsteğe bağlı ek güvence; bölüm 11. |
| `log_path_in_container` / `log_path` | `null` | Varsayılan konteyner içi log: `/home/container/SquadGame/Saved/Logs/SquadGame.log` (`sqreader/discovery.py`, `DEFAULT_LOG_PATH_IN_CONTAINER`). Farklıysa yazın. |
| `net_policy` / `net_allow_hosts` | `"local-only"` / `[]` | Dış ağ politikası (`sqreader/netpolicy.py`). `local-only`: yalnız loopback, özel ağ ve `net_allow_hosts`'ta birebir yazılan adresler; `allow-central` upstream davranışını (enroll, push, güncelleme) açar ve açıkça seçilmelidir. Geçersiz değerde okuyucu HATA verip çıkar (çıkış kodu 2). `SQREADER_NET_POLICY` / `SQREADER_NET_ALLOW_HOSTS` ortam değişkenleri config'e üstündür (3.4). |
| `update_enabled` / `offset_autoheal` | `false` / `false` | Bu çatalda kod varsayılanı da `false` (`sqreader/config.py`); upstream'de `true` idi. Örnek config açıkça `false` verir. |
| `export_state_dir` | `null` | `export_state.json`, `export_audit.log`, `retention_state.json` yeri (`sqreader/export_state.py`: `STATE_FILE`, `AUDIT_FILE`, `RETENTION_STATE_FILE`); `null` = kayıt dizininin üst dizini (`/var/lib/sqreader/7787`). `retention --state-dir` bunu ezer. |
| `export_live_enabled` | `false` | **Canlı yayın** (SSE) anahtarı; varsayılan KAPALI. JSON boolean olmalı (`"true"` metni reddedilir, okuyucu çıkış 2 ile durur). Export API açık değilse (kullanılabilir token dosyası yoksa) etkisizdir. Bölüm 10.5. |
| `live_max_clients` | `2` | Canlı yayında aynı anda en çok abone (1-8); sonraki istek 503 `live_busy`. |
| `live_ring_seconds` / `live_ring_max_mb` | `30` / `32` | Son karelerin bellek halkası (5-120 sn / 1-256 MB; hangisi önce dolarsa). Diske yazılmaz. |
| `retention_max_age_days` | `90` | Onaylı (ack) ve bitmiş kayıtlar bu yaştan sonra silinir (0 = kapalı). |
| `retention_max_total_gb` | `150` | Kayıt dizini bu boyuta sığana dek en eskiler silinir (0 = kapalı). |
| `retention_min_free_gb` | `5` | Boş alan tabanı: altında DİSK ACİL DURUMU (en eskiler silinir, onaysızlar en sonda); hiçbir şey silinemiyorsa YENİ KAYIT AÇILMAZ (`recordingEnabledReason=disk_low`). |
| `retention_in_progress_min` | `60` | Son N dakikada değişen dosyaya (etkin kayıt) dokunulmaz. |
| `retention_min_keep` | `3` | Boş alan kuralında en yeni N bitmiş kayıt korunur. |
| `retention_require_ack` | `null` | `null` = Export API açıksa ack ZORUNLU; `true`/`false` zorlar. |
| `retention_include_unverified` / `retention_unverified_max_age_days` | `false` / `180` | Doğrulanmamış/legacy kayıtlar yalnız `true` ve bu yaştan eskiyse silinir. |
| `push_enabled`, `central_url`, `push_replay`, `alert_replay_base`, `alert_webhook`, `plugins_config` | `false`/`null` | Dış hedef yok. |
| `export_token_file` | `/etc/sqreader/7787.export-token` | Token'ın kendisi değil, yolu. |

Veri dizini, stats DB ve kayıt dizini config'te değil, unit'in komut satırındadır
(`--stats-db`, `--recordings-dir`).

Ayrıca HTTP portu için bir ortam dosyası (sır içermez):

```bash
printf 'SQREADER_HTTP_PORT=8787\n' | sudo tee /etc/sqreader/7787.env >/dev/null
sudo chmod 0644 /etc/sqreader/7787.env
```

7787 -> 8787 (7788 -> 8788). `.env.agent` **oluşturulmaz** (`sqreader enroll` çalıştırılmaz).

## 6. Unit kurulumu ve yalnız 7787'nin etkinleştirilmesi

```bash
sudo install -m 0644 /opt/sqreader/src/deploy/zerozone/sqreader@.service            /etc/systemd/system/
sudo install -m 0644 /opt/sqreader/src/deploy/zerozone/sqreader-retention@.service  /etc/systemd/system/
sudo install -m 0644 /opt/sqreader/src/deploy/zerozone/sqreader-retention@.timer    /etc/systemd/system/
sudo systemctl daemon-reload

# Sözdizimi denetimi (çalıştırılmadı: Linux host gerekli):
systemd-analyze verify sqreader@7787.service
systemd-analyze security sqreader@7787.service --no-pager | tail -n 3

# Oyun sunucusu çalışıyorken, YALNIZ 7787:
sudo systemctl enable --now sqreader@7787.service
systemctl status sqreader@7787.service --no-pager
journalctl -u sqreader@7787 -f
```

**`sqreader@7788` için `enable` YAPILMAZ.** Şablon aynıdır ama ikinci sunucu ancak bilinçli
kararla, kendi config'i/veri dizini/token'ı ile açılır (bölüm 12).

**Örnek adı ve config eşitliği.** Unit `ExecStartPre=sqreader check-instance %i` adımıyla örnek adının (`sqreader@7787`) config'teki `squad_port` YA DA `squad_query_port` değerlerinden birine EŞİT olduğunu denetler (ikisi de doluysa biri yeter). İkisi de boşsa, hiçbiri eşleşmiyorsa (7788 config'i ile 7787 örneği ya da tersi) ya da config dosyası bozuksa servis BAŞLAMAZ (çıkış kodu 2, Türkçe neden iki alanı da adıyla söyler). Elle deneme (ÇALIŞTIRILMADI: Linux host gerekli):

```bash
sudo -u sqreader env SQREADER_CONFIG=/etc/sqreader/7787.config.json /opt/sqreader/venv/bin/sqreader check-instance 7787; echo "çıkış=$?"   # 0
sudo -u sqreader env SQREADER_CONFIG=/etc/sqreader/7787.config.json /opt/sqreader/venv/bin/sqreader check-instance 7788; echo "çıkış=$?"   # 2 (uyuşmuyor)
```

`systemctl stop sqreader@7787` okuyucuyu durdurur; oyun sunucusuna hiçbir etkisi yoktur.
Unit `Restart=on-failure`, `RestartSec=20`, 15 dakikada en çok 5 başlatma (`StartLimit*`)
kullanır: yeniden başlatma fırtınası olmaz; sınıra ulaşırsa unit `failed` kalır (görünür).
Sıfırlamak: `sudo systemctl reset-failed sqreader@7787`.

## 7. Asgari yetkiler, sertleştirme ve NELERİN yapılmadığı

### 7.1 Yetenekler (capabilities)

Unit `User=sqreader` ile çalışır; **yalnız iki yetenek** verilir (`AmbientCapabilities=` ve
`CapabilityBoundingSet=` aynı iki değerdir):

| Yetenek | Neden gerekli | Dayanak |
|---|---|---|
| `CAP_SYS_PTRACE` | Başka kullanıcının/konteynerin sürecinin `/proc/<pid>/mem` dosyasını `pread` ile ve `process_vm_readv` ile okumak (erişim denetimi `PTRACE_MODE_ATTACH`); `/proc/<pid>/exe`, `/proc/<pid>/root` gibi bağlantıları izlemek; `kernel.yama.ptrace_scope` 1 ya da 2 iken izin | Kaynak: `sqreader/mem.py` (dosya başı açıklaması, `_load_process_vm_readv`, `read()`), `README.md:36`, `docs/findings.md:42-44`. Belge: `proc(5)`, `ptrace(2)`, `process_vm_readv(2)`, `capabilities(7)`, çekirdek Yama belgesi. |
| `CAP_DAC_READ_SEARCH` | `/proc/<pid>/mem` dosyası hedef kullanıcıya aittir ve 0600'dır; konteyner dosya sistemindeki (`/proc/<pid>/root/...`) logu başka UID okuyabilir. Dosya okuma/dizin arama izin denetimini atlar | `README.md:36` (upstream aynı ikiliyi ister); `capabilities(7)` |

**DOĞRULANMADI:** bu iki yeteneğin gerçek host'ta (Docker/Pterodactyl, sizin çekirdek/LSM ayarlarınız)
yeterli olduğu. `docs/findings.md` yalnız root ile yapılan ölçümü anlatır ("root okur"); yetenekli
root-olmayan kullanıcı canlıda denenmedi. `dogrula.sh` bölüm 5 bu denemeyi yapar
(`setpriv` ile aynı yetenek kümesini kurar) ve `CapEff` değerini doğrular
(`0000000000080004` beklenir).

### 7.2 `kernel.yama.ptrace_scope` etkisi

`ptrace_scope` değeri `/proc/sys/kernel/yama/ptrace_scope` dosyasında görülür. Yama, `PTRACE_ATTACH`
benzeri erişimleri (belleği okumak dahil) kısıtlar:

| Değer | Anlamı | Reader için |
|---|---|---|
| 0 | klasik: UID/yetenek denetimi | çalışır |
| 1 | yalnız soy (descendant) süreçler; `CAP_SYS_PTRACE` bu kısıtı aşar | yetenekle çalışır |
| 2 | yalnız `CAP_SYS_PTRACE`'e sahip süreçler | yetenekle çalışır |
| 3 | hiçbir süreç attach edemez; yetenekle aşılmaz; yeniden başlatmaya kadar geri alınmaz | **çalışmaz** |

`ptrace_scope` **değiştirilmez** (`packaging/install.sh` de aynı kararı belgeler: makinedeki
tüm süreçleri zayıflatmak istemeyiz). Değer 3 ise yetenek yetmez; çözüm sunucu politikasının
gözden geçirilmesidir, reader'da bir ayar değildir. Bu davranışın kesin tablosu belgeden alındı;
**bu makinede doğrulanmadı**. `dogrula.sh` bölüm 2 değeri okur, 3 ise HATA basar.

### 7.3 Kullanılan sertleştirme

| Seçenek | Gerekçe |
|---|---|
| `NoNewPrivileges=yes` | süreç ve çocukları ek yetki kazanamaz (ortam yetenekleri korunur) |
| `ProtectSystem=strict` | tüm dosya sistemi salt-okunur, yalnız `ReadWritePaths` yazılabilir |
| `ReadWritePaths=/var/lib/sqreader/%i` | yalnız **bu örneğin** veri dizini |
| `ProtectHome=read-only` | `yes` değil: `/home` altındaki konteynersiz kurulumun logu (`squad_log_glob` geri dönüşü) okunabilsin, yazılamasın. Konteyner logu `/proc/<pid>/root` üzerinden okunur; bu sihirli bağlantı hedef sürecin bağlama alanını gösterir, reader'ın kendi `ProtectHome` ayarından etkilenmemesi beklenir. **DOĞRULANMADI** (`dogrula.sh` bölüm 4 sınar). |
| `PrivateTmp=yes`, `PrivateDevices=yes` | özel `/tmp`, asgari `/dev` (`/proc` etkilenmez) |
| `ProtectKernelTunables/Modules/Logs`, `ProtectControlGroups`, `ProtectClock`, `ProtectHostname` | çekirdek/cgroup yüzeyini kapatır (reader `/proc/<pid>/*` okur, bunlara yazmaz) |
| `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` | başka soket aileleri (netlink, packet) yok |
| `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`, `SystemCallArchitectures=native` | genel daraltma |
| `UMask=0027` | üretilen dosyalar grup-okur, başkasına kapalı |

### 7.4 Bilinçli OLARAK kullanılmayanlar ve nedenleri

- **`ProtectProc=`, `ProcSubset=`, `PrivateUsers=`, `PrivatePIDs=`**: `/proc` görünürlüğünü ya da
  kullanıcı ad alanını kısıtlar. `ProtectProc=invisible` ve `ProcSubset=pid` başka kullanıcıların
  süreçlerini gizler/kısar; `PrivateUsers=yes` kullanıcı ad alanı oluşturur ve `CAP_SYS_PTRACE`
  ana ad alanındaki oyun sürecine karşı geçerli olmaz. Hepsi oyun sürecini bulmayı/okumayı
  bozar; kullanılmaz.
- **`MemoryDenyWriteExecute=`**: CPython + `ctypes`/libffi ile uyumu bu makinede
  **DOĞRULANMADI**; yanlışsa okuyucu başlamaz. Konuldu**ysa** canlıda önce
  `systemctl edit sqreader@7787` ile geçici drop-in olarak denenmeli
  (`[Service]` altına `MemoryDenyWriteExecute=yes`); başlamazsa kaldırılır.
- **`SystemCallFilter=`**: izinli çağrı listesi ölçülmeden yazılırsa okuyucu sessizce ölür.
  Canlıda `strace -c -f -p <MainPID>` ile çıkarılıp eklenebilir; şimdi yok.
- Docker soketi (`docker.sock`) unit'e verilmez, `docker` grubu yok. Hiçbir konteyner
  ayrıcalıklı (privileged) yapılmaz, konteynerin ayarı değiştirilmez.

### 7.5 Kaynak sınırları (ÖLÇÜLMEDİ)

`Nice=10`, `CPUQuota=50%`, `MemoryMax=1G` (`MemoryHigh=768M`), `TasksMax=64`,
`IOSchedulingClass=best-effort`/`IOSchedulingPriority=7`. Bunlar oyun sunucusuna öncelik
veren **makul başlangıç tahminleridir; ölçüm değildir**. Canlıda bölüm 14'e göre ayarlanır.
Not: `MemoryMax` aşılırsa süreç öldürülür (okuyucu, oyun değil).

Tarama hızı: unit `--hz 2` kullanır (`packaging/install.sh` ile aynı tek katmanlı kayıt).
`--record-hz 4` iki katmanlı kayıt daha akıcı replay verir ama yardımcı bir alt süreç
(`build-worker`) başlatır ve kaynaktan çalıştırmada kanıtlanmıştır (`install.sh:287-293`);
ölçmeden açmayın.

### 7.6 Geçici teşhis: yetenekler yetmezse

Yeteneklerle `doctor`/`serve` çalışmıyorsa (log'da `PermissionError`/`EPERM`/`EACCES`), önce
nedeni ayırın; **unit'i kalıcı olarak root'a çevirmeyin**:

```bash
# 1) Servisin yetenekleriyle elle deneme (root olarak setpriv kurar; dogrula.sh bunu yapar):
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787
# 2) Aynı denemeyi TAM root ile: yalnız teşhis içindir (--tam-yetki)
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787 --tam-yetki
# 3) Root ile çalışıp yetenekle çalışmıyorsa eksik yeteneği/LSM engelini arayın:
sudo dmesg | grep -i -E 'apparmor|denied' | tail
sudo ausearch -m avc -ts recent 2>/dev/null | tail        # SELinux varsa
sudo strace -f -e trace=openat,process_vm_readv -o /tmp/sr.strace \
  setpriv --reuid=sqreader --regid=sqreader --init-groups \
  --inh-caps=+sys_ptrace,+dac_read_search --ambient-caps=+sys_ptrace,+dac_read_search \
  --bounding-set=-all,+sys_ptrace,+dac_read_search --no-new-privs -- \
  env SQREADER_CONFIG=/etc/sqreader/7787.config.json /opt/sqreader/venv/bin/sqreader doctor
```

Sonuçlar: root'la çalışıp yetenekle çalışmıyorsa, `strace` çıktısındaki ilk `EACCES/EPERM` hangi
yolu/çağrıyı engellediğini gösterir (ek yetenek ya da AppArmor/SELinux kuralı gerekebilir).
Ek yetenek gerekiyorsa gerekçesiyle belgeleyip yalnız o yeteneği ekleyin. Root çalıştırmak
**varsayılan değildir**; gerekirse ayrı, yazılı bir karardır.

## 8. Doğrulama (`dogrula.sh`) ve geçici teşhis

```bash
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787            # tam
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787 --hizli    # bellek okuyan adımlar hariç
```

Betik **salt-okunurdur**: hiçbir şeyi değiştirmez, hiçbir süreci durdurmaz, oyuna dokunmaz,
token'ı ekrana basmaz. Her adım `TAMAM` / `HATA` / `ATLANDI` basar; biri başarısız olsa da
devam eder; sonda özet ve, `HATA` varsa, sıfır olmayan çıkış kodu verir. Bölümler:

1. İşletim sistemi/çekirdek, systemd, venv, `sqreader` komutu.
2. `ptrace_scope` ve `/proc` bağlama seçenekleri (`hidepid`).
3. Oyun süreçleri: PID, `Port=`/`QueryPort=`, exe, konteyner kimliği, başlangıç zamanı; port
   ölçütüyle **tam bir** süreç eşleşmesi; config ile tutarlılık.
4. `/proc/<pid>/root` altında log dosyasına erişim.
5. `sqreader version`, `selftest`, `doctor`, `summary` (servisin yetenekleriyle).
6. Oyun sürümü / bellek düzeni uyumu (`doctor` sonucundan).
7. Disk alanı (eşik `DISK_ESIK_YUZDE`, varsayılan %90).
8. Unit durumu, `--pid` yokluğu, `systemd-analyze verify`, servis yetenekleri.
9. Export API: kimliksiz istek reddi (401), `info`, `health` (token dosyadan okunur, gösterilmez).
10. Dış ağ politikası: config, unit ortamı, dinleme adresi, dış bağlantı taraması, engellenen giriş sayısı.

**Beklenen sağlıklı çıktı (özet):** bölüm 3'te "TAM BİR süreç eşleşti", bölüm 4'te "log okunabiliyor",
bölüm 5'te `CapEff=0000000000080004` ve `doctor: PASS`, bölüm 8'de `is-active=active`,
bölüm 9'da `readerState=ok` ve `401` (kimliksiz), bölüm 10'da "yalnız loopback'te dinliyor".
`selftest`'in `sealed envelope`/`release signatures` satırları `ATLANDI` görünür (bölüm 3.5).

Betik yalnız Linux'ta anlamlıdır. Windows'ta çalıştırılıp yalnız yönetimsel akış
sınandı (tüm Linux'a özgü adımlar `HATA`/`ATLANDI` verir); gerçek host çıktısı bilinmez.
7788 unit'i etkinse ve bilinçli değilse `dogrula.sh 7788` HATA verir (`IKINCI_SUNUCU_ACIK=1` ile
onaylanır).

## 9. Uyumsuz oyun sürümünde beklenen davranış

Squad güncellenirse reverse-edilmiş ofsetler kayabilir (`docs/offsets.md`, `docs/findings.md`).
Bu dağıtımda **otomatik onarım kapalıdır** (`offset_autoheal: false`, `update_enabled: false`,
push yok), bu yüzden beklenen davranış **görünür durma**dır, sessiz yanlış veri değil:

1. Oyun yeniden başlar ve ikili değişir. Okuyucu bağlanamaz ya da `doctor` FAIL verir.
2. `journalctl -u sqreader@7787` içinde bağlanma hatası/bekleme satırları görülür
   (`[degraded] durum ucu açık ...`, `oyun süreci bekleniyor`). Reader **yaşamaya devam eder**
   ve yeniden dener (15 sn'den 300 sn'ye kadar); `sqreader/cli.py` `_open_pipeline_or_wait`.
3. `readerState` değişir: `attach_failed` / `waiting_for_game` / `container_mismatch` / `stale`
   (`GET /export/v1/info` ve `/export/v1/health`'te). `GET /health` (`127.0.0.1:8787`) 503 ve
   `STALE` döner (`sqreader/httpsrv.py`, `_handle_health`).
4. Yeni maç **kaydedilmez** (okuyucu okuyamıyor). Mevcut, bitmiş kayıtlar ve istatistikler
   Export API'den servis edilmeye devam eder.
   **Zero Zone `squadreader` modundaysa (hedef kurulum) bu süre boyunca o sunucu için istatistik ve EXP
   ÜRETİLMEZ ve kaçan maçlar sonradan gelmez: yedek motor yoktur** (Zero Zone `docs/squadreader/KARARLAR.md`
   bölüm 1.4). SR'nin `finalized` yapamadığı maç Export API'de yer almaz.
5. Zero Zone tazelik göstergesi "bayat/çevrimdışı" olur (`freshness.js`, varsayılan 10 dk).

Nasıl gözlenir:

```bash
curl -s http://127.0.0.1:8787/health                     # OK / STALE + lastTickAgeSec
curl -s http://127.0.0.1:8787/health/deep | python3 -m json.tool   # yalnız bu makineden; X-Forwarded/X-Real-IP başlığı varsa 404
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787   # bölüm 5-6: doctor FAIL/DRIFTED satırları
journalctl -u sqreader@7787 --since "-30min" --no-pager
```

**Bu davranış canlıda gözlenmedi**; yukarıdaki durumlar kaynak koddan ve belgeden çıkarıldı.
Canlı sunucuda "denemek için" sürüm bozmayın; oyunun gerçek bir güncellemesinden hemen sonra
`dogrula.sh`'i koşup sonucu kaydedin. Uyumsuzsa bölüm 11'e göre ELLE yükseltme yapılır.

## 10. Zero Zone ile bağlantı

### 10.1 Ağ yolu (öncelik sırası)

1. **WireGuard / özel ağ** (önerilen). Zero Zone web sunucusu reader'a özel ağ adresinden
   ulaşır; TLS zorunlu değildir (tünel şifreli). Reader `127.0.0.1`e bağlı kalır; özel ağ
   arayüzünde dinleyen **nginx** yalnız `/export/v1/` yolunu ilerletir (`nginx-export.conf.example`
   dosyasında `listen <WG_ARAYUZ_IP>:8080;` yazıp TLS ve `ssl_*` satırlarını çıkarın; `allow` satırı
   Zero Zone'un **tünel** adresi olur). Reader'ın kendi HTTP portu **doğrudan özel arayüze açılmaz**:
   o portta `/export/v1/` dışında kimlik doğrulamasız yollar (`/api/*`, `/health`...) vardır.
   - Yine de doğrudan bağlama istenirse (nginx'siz) tek satırlık drop-in ve `nftables` ile yalnız
     Zero Zone eşine izin verilmesi gerekir; **önerilmez**:
     `sudo systemctl edit sqreader@7787` ile `[Service]` altında boş `ExecStart=` ve ardından tüm komutu
     `--host <WG_ARAYUZ_IP>` ile yeniden yazın (bağlama adresi tam arayüz adresi olmalı, "her arayüz"
     adresi olmamalı).
2. **TLS sonlandıran nginx + IP izin listesi** (`nginx-export.conf.example`). Zero Zone farklı
   makinedeyse (mevcut site bir Windows VDS'te) ve WireGuard yoksa. Ayrı `server` bloğu ve ayrı alt alan
   adı kullanır; mevcut sitenin nginx/Caddy ayarına dokunmaz. Yalnız `/export/v1/` proxylenir
   (`/sr/7787/export/v1/...` -> `127.0.0.1:8787/export/v1/...`), `allow <ZZ_WEB_SUNUCUSU_IP>; deny all;`,
   `proxy_buffering off` (yanıt akışı), 600 sn zaman aşımı, küçük `client_max_body_size` (32k; reader ack gövdesi en çok 24576 bayt, control 4096 bayt), `limit_req`, yöntemler GET/HEAD/POST (POST yalnız `/export/v1/ack` ve `/export/v1/control/recording`; GET yolları `info`, `health`, `matches`, `unverified`, `recordings`; diğer yollar 404),
   `Authorization` aynen iletilir, erişim logunda `Authorization` yazılmaz.

   ```bash
   sudo install -m 0644 /opt/sqreader/src/deploy/zerozone/nginx-export.conf.example /etc/nginx/conf.d/zz-export.conf
   sudoedit /etc/nginx/conf.d/zz-export.conf      # <...> yer tutucularını doldurun
   sudo nginx -t                                   # ÇALIŞTIRILMADI: Linux host gerekli
   sudo systemctl reload nginx
   ```

### 10.2 Zero Zone tarafı ayarı

`SquadReaderSync` eklentisi (`squad-server/plugins/squadreader-sync.js`) kaynakları
`sources: [{ zzServerId, sourceServerId, baseUrl, gamePort, tokenFile, enabled, cutoverAt, allowHosts }]`
olarak alır. Örnek (yer tutucular):

```json
{
  "zzServerId": "<ZZ_SUNUCU_KIMLIGI>",
  "sourceServerId": "zz-7787",
  "baseUrl": "https://<EXPORT_ALT_ALAN_ADI>/sr/7787",
  "gamePort": 7787,
  "tokenFile": "<ZZ_TARAFINDA_TOKEN_DOSYASI_YOLU>",
  "enabled": true,
  "cutoverAt": null,
  "allowHosts": ["<EXPORT_ALT_ALAN_ADI>"]
}
```

- `sourceServerId` reader'ın `server_id`'siyle **birebir** aynı olmalı (`zz-7787`); uyuşmazsa import reddedilir.
- Zero Zone istemcisi (`squad-server/lib/squadreader/net-guard.js`) yalnız özel ağ adreslerine
  izin verir; genel bir alt alan adı (yol 2) `allowHosts` içinde **birebir** yazılmalıdır. Yönlendirme
  (redirect) hata sayılır: nginx yönlendirme üretmemeli.
- Zero Zone tarafındaki SIRA (bağlayıcı; ayrıntı Zero Zone deposunda `deploy/squadreader/KURULUM-TR.md` bölüm 3): (0) üretim
  veritabanının KOPYASINDA MariaDB migration kuru koşusu (027-036; MariaDB >= 10.2.3; bu migration'lar MariaDB'de hiç
  çalıştırılmadı), (1) taze yedek (bu rehberin bölüm 15.1 sırası: önce reader, sonra ZZ veritabanı), (2) kod, (3) `legacy` +
  migration, (4) `shadow` (içe aktarır, gizli, ödül yok; ÖNERİLEN doğrulama dönemi), (5) `squadreader` (hedef: SquadReader tek
  istatistik ve EXP kaynağı; kesinti tarihi `cutoverAt`, tek anahtar yapılandırması ve iki maç arasında yeniden başlatma). Geri alma
  adımları aynı dizinde `GERI-ALMA-TR.md`'dedir (önce tüketiciyi config'ten çıkar, bekleyenleri işle, sonra `legacy`; migration
  `down` çalıştırılmaz). **Zero Zone'da üretime kurulum ve üretim verisini değiştiren her adım kullanıcı onayı ister.**
- **Her SquadJS süreci için benzersiz `server.id`; her süreç `sources` listesine yalnız KENDİ kaynağını yazar.** Bu okuyucu (7787)
  yalnız 7787'yi yöneten SquadJS sürecinin `sources` girdisidir; 7788 okuyucusu açılırsa Zero Zone'da onun AYRI bir SquadJS süreci
  (ayrı `config.json`, ayrı `server.id`, `localServerId: zz-7788`) olur. Aynı sunucu için iki SquadJS süreci çalıştırılmaz
  (Zero Zone süreç kirası: `leaseTtlSec` 90, `leaseHeartbeatSec` 15).
- Zero Zone eklenti seçenekleri (kod varsayılanlarıyla; hepsi Zero Zone deposundaki `squad-server/plugins/squadreader-sync.js`
  `optionsSpecification` içindedir): `leaseTtlSec` 90, `leaseHeartbeatSec` 15, `deepReconcileEveryHours` 24 (0 kapatır),
  `deepReconcileWindowDays` 14, `reconcileEveryRuns` 10, `reconcileWindowSec` 86400; `DiscordRoundEnded`:
  `squadReaderStatsWaitSec` 1800, `squadReaderMatchWindowSec` 900, `legacyTimestampFix` false (kullanıcı kararı).
- Zero Zone replay'i `ready` yaptıktan sonra kaydı bu okuyucuya `POST /export/v1/ack` ile ONAYLAR; retention yalnız
  onaylı kayıtları yaşa göre siler (bölüm 13). **Zero Zone tarafında replay anahtarı (`replaySecretFile`) yoksa
  hiçbir kayıt onaylanmaz ve bu makinenin diski dolabilir.**
- Zero Zone `/tekrar` hız sınırı `request.ip` ile çalışır; ters proxy arkasında `trustProxy` kapalıysa tüm
  ziyaretçiler tek IP görünür. Zero Zone'un web sunucusunu yapılandırırken buna dikkat edin.

### 10.3 Token'ın Zero Zone'a güvenli aktarımı

- Token **reader makinesinde** üretilir (bölüm 5.1); aktarım şifreli kanaldan yapılır, düz e-posta/sohbet
  ile **yapılmaz**. Örnek (yer tutucular, token ekrana/komut satırına yazılmaz):

  ```bash
  sudo cat /etc/sqreader/7787.export-token | ssh <ZZ_YONETICI>@<ZZ_WEB_SUNUCUSU> 'umask 077; cat > <ZZ_TARAFINDA_TOKEN_DOSYASI_YOLU>'
  ```

- Zero Zone tarafındaki dosya yalnız Zero Zone sürecinin kullanıcısına okunabilir olmalı. Windows'ta örnek:
  `icacls "<TOKEN_DOSYASI>" /inheritance:r /grant:r "<ZZ_SERVIS_KULLANICISI>:R"`. Zero Zone token'ı her
  çağrıda dosyadan okur (yeniden başlatma gerekmez).
- **Döndürme (rotation):** yeni token üret, reader'daki dosyaya yaz, Zero Zone'daki dosyayı güncelle; iki adım arası
  kısa bir 401 penceresi olur (Zero Zone `unauthorized` hatası verip yeniden dener). Şüphede hemen döndürün.
- Token yedeğe alınmaz (bölüm 15).

### 10.4 Kaçan (dışa aktarılmayan) maçları izleme

Okuyucu bir maçın SONUNU göremezse (kapalıydı, oyun sunucusu yeniden başladı, sentetik kimlik durum
dosyası kayboldu) o maç `unverified` kalır ve Export API ile HİÇ verilmez: o maçta oyuncular ne
istatistik ne EXP alır. İki salt-okunur görünürlük vardır (aynı token, aynı güvenlik; oyuncu verisi ve
istatistik DÖNMEZ):

```bash
# (token komut satırına yazılmaz: başlık dosyadan okunur; `curl -H @dosya` curl 7.55+ ister, çalıştırılmadı: Linux host gerekli)
# sayılar: unverified.{total,last24h,last7d,last30d,lastAt,finalizedLast7d,finalizedLast30d,ratioLast30d,training,asOf}
curl -s -H @<(printf 'Authorization: Bearer %s' "$(sudo cat /etc/sqreader/7787.export-token)")   http://127.0.0.1:8787/export/v1/health | python3 -m json.tool
# hangi maçlar kaçtı (özet; sayfalı: ?after=<imleç>&limit=<n>)
curl -s -H @<(printf 'Authorization: Bearer %s' "$(sudo cat /etc/sqreader/7787.export-token)")   "http://127.0.0.1:8787/export/v1/unverified?limit=20" | python3 -m json.tool
```

- `ratioLast30d` = kaçan / (kaçan + verilen), son 30 gün; payda 0 ise `null`. Eğitim katmanı maçları
  `training` altında ayrıdır ve üst düzey sayılara dahil değildir.
- Güncel oran BİLİNMİYOR ve bu depodan tahmin edilemez; canlıda bu alanla ölçülür. `stats.py`'deki
  "821 maçın 123'ü" notu göç öncesi arşiv ölçümüdür, güncel oran değildir.
- Yeniden başlatma ile ilk `InProgress` tick'i arasında canlı bir maç geçici olarak listede görünür;
  aynı `matchId` ile devam ederse yeniden açılır ve bitişi görülürse `finalized` olarak verilir.
- Ayrıntı, kaynak referansları ve bilinen ölçüm sınırları (sayaçlar en büyük değerle tutulur; okunamayan
  sayaç `0` olur): `docs/zerozone-kacan-maclar.md`.

### 10.5 Canlı yayın (opsiyonel; varsayılan KAPALI)

Zero Zone'un sitede "canlı maç izleme" özelliği için okuyucu, oynanmakta olan maçı Export API üzerinden Zero Zone'a
**akıtabilir**. Bu, upstream'de olmayan bir fork özelliğidir (`sqreader/livehub.py`) ve **varsayılan olarak kapalıdır**.
Açmadan önce `PRIVACY.md` "Live stream over the Export API" bölümünü okuyun.

#### 10.5.1 Ne olduğu, ne olmadığı

- İki yol: `GET /export/v1/live/stream` (SSE) ve `GET /export/v1/live/snapshot` (son tam kare, JSON). İkisi de Export API'nin
  AYNI Bearer token'ıyla korunur. `export_live_enabled` `false` iken (varsayılan) ikisi de düz **404** döner.
  Herkese açık `/stream` ya da `/latest` yoktur ve yapılmaz: canlı veri yalnız token korumalı Export yolundadır.
- Akış **HAM veridir**: okuyucunun kendi tick satırı, bayt bayt. EOS kimlikleri, oyuncu adları, bellek adresleri
  (`_addr`, `leaderStateAddr`), `isAdmin` ve her oyuncunun konumu içerir. Okuyucu hiçbirini süzmez. Temizleme (takma ad,
  adres/`isAdmin` atma, gizli profiller, yayın gecikmesi) **yalnız Zero Zone sunucu tarafında** yapılır; Zero Zone dışında
  herhangi bir tüketici bağlarsanız o adım sizin sorumluluğunuzdadır. Token sızıntısı bu yüzden ciddidir (bölüm 10.3).
- Canlı yayın tick döngüsüne yalnız O(1) bir `publish` ekler (hazır satırı halkaya ve abone kuyruklarına koyar);
  kodlama ve soket yazımı abone iş parçacıklarındadır. Kapalıyken eklenen tek şey bir `None` denetimidir.
- Diske hiçbir şey yazılmaz; yalnız bellekte kısa bir kare halkası tutulur (yeniden başlatmada gider).

#### 10.5.2 Açma

Ön koşullar (hepsi sağlanmadan açmayın):

1. Export API açık ve token dosyası en az **32 karakter** (bölüm 5.1). Token yoksa ya da kısaysa canlı yayın da kurulmaz
   (log: "export_live_enabled is set but the Export API is off").
2. Zero Zone tarafı hazır: `SquadReaderSync` `live` seçenekleri ve **herkese açık yayın için** Zero Zone'da replay anahtarı
   (`replaySecretFile`; yoksa herkese açık canlı kapalı kalır) ve yayınlanmış çatal adresi (`https://github.com/HYD7/squadreader`; AGPL, bölüm 19).
   Zero Zone'da yayın gecikmesi varsayılan 600 sn, en az 180 sn'dir (`live.delaySec`).
3. **Yalnız 7787.** 7788 için açılmaz (bölüm 12).
4. Ölçüm planı (bölüm 10.5.6): önce yayın kapalıyken taban değeri kaydedin.

Config (`/etc/sqreader/7787.config.json`; anahtarlar örnek dosyada `false`/varsayılanlarla zaten vardır):

```json
"export_live_enabled": true,
"live_max_clients": 2,
"live_ring_seconds": 30,
"live_ring_max_mb": 32
```

- `live_max_clients` 1-8 (varsayılan 2): Zero Zone tek bağlantı kullanır; ikinci abone bir yönetici/teşhis içindir. Sınırın
  ötesi 503 `live_busy` (`Retry-After: 5`).
- `live_ring_seconds` 5-120, `live_ring_max_mb` 1-256: hangisi önce dolarsa en eski kare atılır. Aralık dışı değer ya da
  boolean olmayan `export_live_enabled` okuyucuyu HATA (çıkış 2) ile durdurur; sessizce varsayılana dönmez.
- Değişiklik için okuyucu yeniden başlatılır. **İki maç arasında yeniden başlatın:** maç ortasında yeniden başlama maçı iki
  kayda bölebilir ve kapanmamış parça `unverified` kalır (bölüm 10.4).

Açtıktan sonra doğrulama (ÇALIŞTIRILMADI: Linux host gerekli):

```bash
# `info` yanıtında `live` alanı yalnız canlı yayın etkinken bulunur:
curl -s -H @<(printf 'Authorization: Bearer %s' "$(sudo cat /etc/sqreader/7787.export-token)")   http://127.0.0.1:8787/export/v1/info | python3 -m json.tool
# akış: ilk olay `hello`, sonra `full`/`pos`/`match`; boştayken 5 sn'de bir `hb`. -N tamponlamayı kapatır.
curl -sN --max-time 15 -H @<(printf 'Authorization: Bearer %s' "$(sudo cat /etc/sqreader/7787.export-token)")   http://127.0.0.1:8787/export/v1/live/stream | cut -c1-160
# token olmadan 401 beklenir (Export API açıkken); doğru token ile ama canlı yayın KAPALIyken 404:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/export/v1/live/stream
```

Bu çıktı ham veri içerir (ad, EOS): ekran görüntüsü/log olarak paylaşmayın.

#### 10.5.3 Zero Zone tarafı (özet)

Ayrıntı Zero Zone deposunda `docs/squadreader/CANLI.md` ve `KARARLAR.md`'dedir. Özet: `SquadReaderSync` `live` seçeneği
`{ delaySec, maxViewers, maxViewersPerIp, bufferMaxMb, adminIdleCloseSec, servers: { "<ZZ_SUNUCU_KIMLIGI>": { enabled, publicEnabled } } }`;
yalnız `sources[].enabled` VE `live.servers` içinde olan kaynak bağlanır (7788 eklenmedikçe sıfır bağlantı). Herkese açık
yayın (`publicEnabled`) varsayılan KAPALIdır; yönetici görünümü ayrı izinle (`squadreader.live`) açılır. Zero Zone yayın
gecikmesini kendi saatiyle uygular ve kareleri yayın anında temizler; okuyucu tarafında gecikme yoktur.

#### 10.5.4 nginx ve ters proxy

Canlı yol `nginx-export.conf.example` içinde AYNI `location ^~ /sr/7787/export/v1/` bloğundadır; ayrı blok gerekmez.
Şart olanlar (örnekte hazırdır, silmeyin): `proxy_http_version 1.1` + `Connection ""`, `proxy_buffering off` (yoksa SSE
donar), `gzip off` (`text/event-stream` `gzip_types`'a eklenmez), yeterli `proxy_read_timeout` (okuyucu 5 sn'de bir `hb`
yazar; 600 sn yeter), `chunked_transfer_encoding`'i kapatmayın. `limit_req` sınırı aşılınca 503 döner ve yeniden bağlanma
fırtınası yaratır; akış için **`limit_conn`** (eşzamanlı bağlantı) kullanılır. Caddy kullanılıyorsa `reverse_proxy` altında
`flush_interval -1`. Bu ayarlar `nginx -t` ve gerçek akışla SINANMADI.

#### 10.5.5 Hata kodları ve sınırlar

| Durum | Anlamı |
|---|---|
| 404 | `export_live_enabled` kapalı, Export API kapalı ya da yanlış yol (hepsi aynı 404) |
| 401 `unauthorized` | token yok/yanlış |
| 400 `bad_query` | `live/stream` sorgu dizesi kabul etmez (`Last-Event-ID` başlığı kullanılır) |
| 503 `live_busy` | `live_max_clients` dolu; `Retry-After: 5` |
| 503 `live_no_frame` | `live/snapshot`: henüz hiç tam kare yok; `Retry-After: 1` |

Olaylar: `hello` (ilk), `full`, `pos` (iki katmanlı kipte 4 Hz konum), `match` (`started`/`ended`, 3 tick onaylı), `hb` (5 sn),
`gap` (kayıp; sonraki `full` yeni baz). Yazma zaman aşımı 10 sn: tıkanan abone bırakılır, tick döngüsü etkilenmez.
Canlı istatistikler (`subscribers`, `publishAvgNs`, `publishMaxNs`, `gaps`, `rejected`, `bytesSent`, `ringBytes`...)
`GET http://127.0.0.1:8787/health/deep` yanıtındaki `live` alanındadır (yalnız loopback; nginx bu yolu dışarı açmaz).

#### 10.5.6 CPU / RAM ölçümü

**Gerçek sunucuda ölçülmedi.** Canlıda, oyun yoğunken, üç durumu aynı komutlarla karşılaştırın: (a) canlı yayın kapalı,
(b) açık ve abonesiz, (c) Zero Zone bağlı. Komutlar bölüm 14'tekiyle aynıdır (`pidstat -u -r -p "$MPID" 5 24`,
`systemctl show sqreader@7787 -p CPUUsageNSec -p MemoryCurrent`), ek olarak `/health/deep` içindeki `live.publishAvgNs` ve
`live.ringBytes`. Oyunun tick süresini/CPU'sunu da aynı anda izleyin.

**Yerel ölçüm (Windows, sentetik golden; gerçek sunucuda ÇALIŞTIRILMADI).** `scripts/live_bench.py`, gerçek `LiveHub`'ı gerçek
`httpsrv` + Export API arkasında, sentetik golden kaydın (`tests/fixtures/zz_golden/zz-7787/recording-*.ndjson.gz`; 8 sahte
oyunculu küçük kareler) satırlarıyla besler ve 0/1/2 gerçek SSE aboneyle (ayrı süreçler, 127.0.0.1) `publish` süresini
ölçer. Süreç yalnız hub + sunucu + üreticiyi içerir (bellek okuyucusu YOK): CPU ve RSS bir SquadReader'ın değil, canlı
yayının **artımlı** maliyetidir.

**A. Canlıya yakın hız** (`5.0` satır/sn, senaryo başına `60.0` sn, `--pad-kb 0`, win32 Python 3.12.10, 16 mantıksal CPU):

| Abone | Yayın sayısı | `publish` p50 (ns) | p99 (ns) | en çok (ns) | CPU % | CPU µs/satır | RSS (MB) | RSS artışı (MB) | gap | gönderilen (MB) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 301 | 19700 | 45900 | 150700 | 0.03 | 51.9 | 32.77 | 1.79 | 0 | 0.00 |
| 1 | 301 | 25200 | 66100 | 83300 | 0.0 | 0.0 | 33.75 | 2.68 | 0 | 1.32 |
| 2 | 301 | 23200 | 77600 | 150400 | 0.0 | 0.0 | 33.87 | 2.89 | 0 | 2.64 |

Tam kare boyutu: 12734-14422 bayt (JSON satırı).

**B. 20 kat hızlı (daha çok örnek; p99 daha güvenilir)** (`100.0` satır/sn, senaryo başına `20.0` sn, `--pad-kb 0`, win32 Python 3.12.10, 16 mantıksal CPU):

| Abone | Yayın sayısı | `publish` p50 (ns) | p99 (ns) | en çok (ns) | CPU % | CPU µs/satır | RSS (MB) | RSS artışı (MB) | gap | gönderilen (MB) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 2000 | 7900 | 24400 | 52500 | 0.08 | 7.8 | 31.89 | 0.91 | 0 | 0.00 |
| 1 | 2001 | 10100 | 32100 | 121700 | 0.0 | 0.0 | 41.02 | 9.83 | 0 | 8.35 |
| 2 | 2001 | 16000 | 81300 | 1239900 | 0.47 | 46.9 | 41.26 | 10.08 | 0 | 16.69 |

Tam kare boyutu: 12734-14422 bayt (JSON satırı).

**C. Şişirilmiş tam kareler (yalnız ölçeklenme göstergesi; gerçek kare boyutu ÖLÇÜLMEDİ)** (`5.0` satır/sn, senaryo başına `60.0` sn, `--pad-kb 256`, win32 Python 3.12.10, 16 mantıksal CPU):

| Abone | Yayın sayısı | `publish` p50 (ns) | p99 (ns) | en çok (ns) | CPU % | CPU µs/satır | RSS (MB) | RSS artışı (MB) | gap | gönderilen (MB) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 301 | 24600 | 66900 | 242600 | 0.0 | 0.0 | 64.8 | 1.23 | 0 | 0.00 |
| 1 | 301 | 27900 | 85800 | 91500 | 0.0 | 0.0 | 76.37 | 11.0 | 0 | 22.07 |
| 2 | 301 | 30400 | 101400 | 167300 | 0.05 | 103.8 | 77.57 | 12.84 | 0 | 44.14 |

Tam kare boyutu: 274895-276583 bayt (JSON satırı).

Bu sayıları şöyle okuyun: (1) golden kareler küçüktür; gerçek tam karenin boyutu ÖLÇÜLMEDİ ve çok daha büyüktür, `--pad-kb`
satırı yalnız ölçeklenmeyi göstermek içindir; (2) Windows'ta süreç CPU saatinin çözünürlüğü ~15,6 ms'dir: `0.0`/`0.03` gibi CPU% değerleri "ölçüm çözünürlüğünün altında"
demektir, "sıfır" değil; CPU µs/satır sütunu bu yüzden anlamsızdır (yalnız B tablosu, 20 sn'de 2000 satır, kabaca yorumlanabilir);
(3) RSS artışı halkanın dolmasını (`live_ring_seconds`) ve abone iş parçacıklarını içerir; senaryolar ayrı süreçtir ve her biri ~30 MB taban RSS ile başlar; (4) Windows saati ve Python sürümü Linux'tan farklıdır: bu `publish` süreleri Linux'ta
ölçülecek değerleri TEMSİL ETMEZ; (5) oyun sunucusu, gerçek ağ ya da Zero Zone bağlı değil. Yeniden üretmek:

```bash
python scripts/live_bench.py --rate 5 --seconds 60            # tablo
python scripts/live_bench.py --rate 100 --seconds 20 --json   # 20x hızlı, daha çok örnek
python scripts/live_bench.py --rate 5 --seconds 60 --pad-kb 256
```

#### 10.5.7 Kapatma

Config'te `export_live_enabled` `false` yapıp okuyucuyu (iki maç arasında) yeniden başlatın; iki yol yine 404 olur. Zero Zone
tarafında ayrıca yönetici panelinden "acil durdur" (bellek içi; yeniden başlatmada kalkar) ve `sources[].enabled` ile `live.servers`
kapatılabilir. Sızıntı şüphesinde token'ı döndürün (bölüm 10.3).

## 11. Konteyner yeniden oluşturma ve oyun güncellemesi

### 11.1 Konteyner yeniden oluşturuldu (`expected_container_id` değişir)

`expected_container_id` boş (`null`) ise yalnız port ölçütü kullanılır ve **yapılacak bir şey yok**.
Doluysa: konteyner yeniden oluşturulunca kimlik değişir; güncelleninceye kadar okuyucu yeni sürece
**bağlanmaz** (log'da ve `readerState=container_mismatch` olarak görünür).

```bash
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787 --hizli    # bölüm 3: yeni konteyner kimliği
sudoedit /etc/sqreader/7787.config.json                            # expected_container_id: yeni 12+ karakter
sudo -u sqreader python3 -c "import json; json.load(open('/etc/sqreader/7787.config.json'))"
sudo systemctl restart sqreader@7787                               # yalnız reader; oyun etkilenmez
```

### 11.2 Oyun güncellendi (ofset uyumsuzluğu) - kontrollü, ELLE yükseltme

Otomatik güncelleme ve ofset onarımı **kapalıdır**. Akış:

1. `dogrula.sh 7787` bölüm 5-6: `doctor` PASS mı? PASS ise yapılacak bir şey yok.
2. FAIL ise okuyucu zaten durmuştur (bölüm 9). Yeni oyun sürümüyle uyumlu bir çatal commit'i hazırlanmalı
   (ofsetler `docs/offsets.md`, `scripts/dump_struct_layout.py`; **bu bir geliştirici işidir**, bu rehberin
   kapsamı dışında). Testlerden geçmiş yeni `<YENI_COMMIT_SHA>` hazır olduktan sonra:
3. Yedek al (bölüm 15), sonra:

   ```bash
   sudo git -C /opt/sqreader/src fetch origin
   sudo git -C /opt/sqreader/src checkout --detach <YENI_COMMIT_SHA>
   sudo git -C /opt/sqreader/src rev-parse HEAD
   sudo /opt/sqreader/venv/bin/pip install -e /opt/sqreader/src        # bağımlılık değiştiyse
   sudo systemctl restart sqreader@7787                                 # yalnız reader
   sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787
   ```

4. Geri dönüş: eski `<COMMIT_SHA>`'ya checkout + `systemctl restart sqreader@7787`. Oyun sunucusuna dokunulmaz.

Güncelleme boyunca oyun sunucusu **yeniden başlatılmaz**, tickrate değiştirilmez.

## 12. İkinci sunucu (7788)

**Varsayılan: kapalı.** Yalnız 7787 etkinleştirilir. `sqreader@7788` için şablon aynıdır ama
`enable` **edilmez**. İkinci sunucu ancak bilinçli bir kararla, aşağıdakilerin **hepsi** hazırlanarak açılır:

```bash
sudo install -d -m 0750 -o sqreader -g sqreader /var/lib/sqreader/7788 /var/lib/sqreader/7788/stats /var/lib/sqreader/7788/recordings
sudo install -m 0640 -o root -g sqreader /opt/sqreader/src/deploy/zerozone/sqreader.7788.config.json.example /etc/sqreader/7788.config.json
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/sqreader/7788.export-token'
sudo chown sqreader:sqreader /etc/sqreader/7788.export-token && sudo chmod 0600 /etc/sqreader/7788.export-token
printf 'SQREADER_HTTP_PORT=8788\n' | sudo tee /etc/sqreader/7788.env >/dev/null
# 7788'in Port= mu QueryPort= mu olduğunu dogrula.sh 7788 ile görüp config'i düzeltin, sonra:
sudo systemctl enable --now sqreader@7788.service
```

Kendi `server_id`si (`zz-7788`), veri dizini, stats DB'si, token'ı ve HTTP portu (8788) vardır. 7787'nin
dizinini ya da token'ını **paylaşmayın**: okuyucu başka `server_id`'nin verisi olan dizinde başlamayı reddeder.
nginx'te 7788 bloğu yorumdadır (bölüm 10) ve Zero Zone'da 7788 için AYRI bir SquadJS süreci gerekir (kendi benzersiz `server.id`, kendi `config.json`, yalnız kendi kaynağını içeren `sources`; bölüm 10.2).

**Canlı yayın 7788 için AÇILMAZ.** `sqreader.7788.config.json.example` içinde `export_live_enabled` `false` kalır, Zero Zone'da `live.servers`'a 7788 eklenmez; ayrıca ölçüm ve bilinçli karar olmadan açılmaz (bölüm 10.5).

## 13. Kayıt saklama (retention)

Okuyucu bitmiş kayıtları (`.sqrx` + `.meta.json`) kendiliğinden silmez; disk dolmasın diye günlük bir iş vardır
(`sqreader-retention@.timer`). **Bu iş kayıt dosyalarını SİLER.**

> **UYARI: parametresiz `sqreader retention --recordings-dir <DIZIN>` artık config varsayılanlarıyla SİLER.**
> Upstream sürümde `--max-age-days`, `--max-total-gb` ve `--min-free-gb` varsayılanı 0 (kapalı) idi, yani
> parametresiz çağrı hiçbir şey silmezdi. Bu çatalda bayrak verilmeyen her sınır `retention_*` config
> anahtarından gelir (varsayılanlar: 90 gün, 150 GB, 5 GB boş alan). Bir bayrak verilirse yalnız o sınır ezilir.
> Neyi sileceğini görmek için HER ZAMAN önce `--dry-run` kullanın.

Kurallar (`sqreader/export_state.py` başlık açıklaması, `sqreader/cli.py` `cmd_retention`, `sqreader/config.py`):

- Yalnız **bitmiş (finalized)** kayıtlar aday olur; etkin kayıt, doğrulanmamış/legacy kayıtlar (`--include-unverified` ve
  kendi daha uzun yaş sınırı olmadıkça) ve sidecar'ı okunamayanlar asla silinmez. Son `--in-progress-min` dakikada
  değişen dosyaya dokunulmaz; bir `.sqrx` daima `.meta.json`'ıyla birlikte ve çökmeye dayanıklı silinir; yalnız
  `--recordings-dir` içindeki düz dosyalar silinir.
- Export API **açıkken** (bu dağıtımda açık) yaş ve boyut kuralları yalnız Zero Zone'un **onayladığı**
  (`POST /export/v1/ack`) kayıtları siler. Onaylanmamış kayıt yalnız disk **acil durumunda** (boş alan
  `retention_min_free_gb` altına inince) en eskiden silinir; her böyle silme günlüğe ve denetim kaydına
  (`export_audit.log`) yazılır, `health` içinde `degradedReasons` `unacked_deleted` görünür.
- **Zero Zone artık ack gönderir** (replay `ready` olup commit edildikten sonra; Zero Zone deposunda
  `squad-server/lib/squadreader/client.js` `ackRecordings`). Ancak Zero Zone'da replay anahtarı yoksa replay işleme
  kapalıdır ve HİÇBİR kayıt onaylanmaz: bu durumda yaş/boyut kuralı **hiçbir şey silmez** (güvenli yön: Zero Zone
  çekmeden kayıt silinmez) ama **disk dolabilir**. `dogrula.sh` bölüm 7'yi ve `/export/v1/health`'i (`disk`,
  `pendingAck`, `degradedReasons`) izleyin; `retention_min_free_gb` altında ve hiçbir şey silinemiyorsa **yeni kayıt
  açılmaz** (istatistik ve oyun etkilenmez).
- Boş alan kuralında en yeni `retention_min_keep` (3) bitmiş kayıt korunur.

**`retention` bayrakları** (`sqreader/cli.py` `build_parser`; hepsi isteğe bağlıdır, verilmezse ilgili `retention_*` config anahtarı):

| Bayrak | Config anahtarı | Anlam |
|---|---|---|
| `--recordings-dir` (ZORUNLU) | yok | Kayıt dizini |
| `--max-age-days` | `retention_max_age_days` | Bu yaştan eski onaylı bitmiş kayıtları sil (0 = kapalı) |
| `--max-total-gb` | `retention_max_total_gb` | Dizin bu boyuta sığana dek en eskileri sil (0 = kapalı) |
| `--min-free-gb` | `retention_min_free_gb` | Boş alan tabanı; altı DİSK ACİL DURUMU (0 = kapalı) |
| `--in-progress-min` | `retention_in_progress_min` | Son N dakikada değişene dokunma |
| `--min-keep` | `retention_min_keep` | En yeni N kaydı koru (boş alan kuralında) |
| `--require-ack` / `--no-require-ack` | `retention_require_ack` | Yalnız onaylı kayıtları sil / onayı bekleme (verilmezse: Export API açıksa açık) |
| `--include-unverified` | `retention_include_unverified` | Doğrulanmamış/legacy kayıtları da (kendi yaş sınırıyla) sil |
| `--unverified-max-age-days` | `retention_unverified_max_age_days` | Doğrulanmamış kayıtlar için yaş sınırı |
| `--state-dir` | `export_state_dir` | `export_state.json` / `export_audit.log` / `retention_state.json` yeri (yoksa `--recordings-dir`'in üst dizini) |
| `--dry-run` | yok | Hiçbir şey silmez; ne silineceğini ve nedenini yazar |

`retention` hiçbir şey eşleşmese de 0 çıkış kodu verir (systemd zamanlayıcısı yeşil kalsın diye). Sayılar (`90` gün,
`150` GB, `5` GB, `3` kayıt, `60` dk, `180` gün) kodun kendi varsayılanlarıdır ve **ölçülmedi**: disk boyutunuza göre
ayarlayın. Unit (`sqreader-retention@.service`) `--max-age-days 90 --min-free-gb 5 --min-keep 3` bayraklarını açıkça verir
(kod varsayılanlarıyla aynı değerler); diğer sınırlar config'ten gelir.

```bash
# Önce silmeden bakın (unit ile aynı bayraklar):
sudo -u sqreader env SQREADER_CONFIG=/etc/sqreader/7787.config.json   /opt/sqreader/venv/bin/sqreader retention   --recordings-dir /var/lib/sqreader/7787/recordings   --max-age-days 90 --min-free-gb 5 --min-keep 3 --dry-run

# Uygun bulursanız zamanlayıcıyı YALNIZ 7787 için açın:
sudo systemctl enable --now sqreader-retention@7787.timer
systemctl list-timers 'sqreader-retention@*'
```

Stats veritabanı (`player_stats.db`) bu işle **küçülmez**; kayıtlar silinse de maç satırları kalır.

## 14. CPU / RAM / disk etkisini ölçme

**Bu belgede hiçbir ölçüm yapılmadı.** Aşağıdakiler canlıda, oyun yoğunken çalıştırılacak komutlardır.
Belleği okumak `PTRACE_ATTACH` yapmaz ve oyunu durdurmaz (`docs/findings.md:44`: attach'sız okuma),
ama yine de CPU ve bellek bant genişliği tüketir; oyun tarafındaki etkiyi kendi izleme yönteminizle ölçün
(reader açıkken/kapalıyken oyunun tick süresi/CPU'su karşılaştırması).

```bash
# systemd cgroup bazında CPU/RAM (canlı):
systemd-cgtop -m -c --depth=3 -n 5 -d 5
systemctl show sqreader@7787 -p CPUUsageNSec -p MemoryCurrent -p TasksCurrent -p NRestarts
# süreç bazında CPU, bellek, disk G/Ç (sysstat paketi):
MPID=$(systemctl show -p MainPID --value sqreader@7787)
pidstat -u -r -d -p "$MPID" 5 24
# aynı anda oyun sürecinin CPU'su (karşılaştırma için); oyun PID'sini dogrula.sh bölüm 3'ten alın:
pidstat -u -p <OYUN_PID> 5 24
# disk:
sudo du -sh /var/lib/sqreader/7787 /var/lib/sqreader/7787/recordings /var/lib/sqreader/7787/stats
df -h /var/lib/sqreader
```

Sonuçlara göre unit'teki `CPUQuota`, `MemoryMax`, `Nice` ayarlanır (`sudo systemctl edit sqreader@7787`).
`CPUQuota` çok düşükse tick gecikir ve replay seyrekleşir; çok yüksekse oyunla yarışır.

Canlı yayın açılırsa (bölüm 10.5) ölçümü ayrıca yapın: yayın kapalı / açık-abonesiz / bir abone (Zero Zone bağlı) durumlarını
aynı komutlarla karşılaştırın (bölüm 10.5.6). Bu makinede canlı yayın için hiçbir ölçüm yapılmadı.

**Kayıt boyutu için kaba tahmin yöntemi (rakam uydurmadan).** İlk birkaç maçtan sonra ölçün:

```bash
ls /var/lib/sqreader/7787/recordings/*.sqrx | wc -l
du -sb /var/lib/sqreader/7787/recordings | cut -f1
# ortalama bayt/maç = toplam bayt / kayıt sayısı;  günlük tahmin = ortalama x günlük maç sayısı
```

Elde tek gerçek veri **sentetiktir** ve gerçek sunucuyu temsil etmez: test fixture'ındaki bir maç (8 oyuncu, 231
kare) `.sqrx` olarak 207404 bayttır (`tests/fixtures/zz_golden/zz-7787/matches.json`). Gerçek maçlarda oyuncu
sayısı ve kare sayısı çok daha büyüktür, bu sayı **ölçekle**nemez. (Upstream, `deploy/cleanup_recordings.sh`
başında "günde ~250 MB" der; bu upstream'in notudur, bu dağıtımda doğrulanmadı.)

## 15. Yedekleme ve geri alma

Betik: `yedek-geri-al.sh` (sqreader kullanıcısı olarak çalışır; root ile çağrılırsa kendini çevirir).

```bash
sudo install -d -m 0750 -o sqreader -g sqreader /var/backups/sqreader
sudo /opt/sqreader/src/deploy/zerozone/yedek-geri-al.sh yedek 7787 /var/backups/sqreader
```

Yedek içeriği: `stats/player_stats.db` (`sqlite3 .backup` ile **tutarlı kopya**, düz `cp` değil; sonra
`PRAGMA integrity_check`), yalnız **finalize** kayıtlar (`.sqrx` + `.meta.json`; aktif kayıt kopyalanmaz),
`export_state.json` / `export_audit.log` / `retention_state.json` (varsa; adlar `sqreader/export_state.py` `STATE_FILE`, `AUDIT_FILE`,
`RETENTION_STATE_FILE`), config
(`/etc/sqreader/7787.config.json`, `.env`), `MANIFEST.txt` ve `SHA256SUMS`. **Token yedeğe alınmaz**
(kaybolursa bölüm 5.1 ile yenisi üretilir ve Zero Zone'da güncellenir). `--token-yedekle` verilirse token ayrı `SIR/`
dizinine ve uyarıyla kopyalanır; o yedeği şifreli/kısıtlı yerde tutun.

Geri alma (önce **servisi elle durdurun**; betik durdurmaz/başlatmaz, çalışıyorsa reddeder):

```bash
sudo systemctl stop sqreader@7787
sudo /opt/sqreader/src/deploy/zerozone/yedek-geri-al.sh geri-al 7787 /var/backups/sqreader/sqreader-7787-<ZAMAN> --dry-run
sudo /opt/sqreader/src/deploy/zerozone/yedek-geri-al.sh geri-al 7787 /var/backups/sqreader/sqreader-7787-<ZAMAN>
sudo systemctl start sqreader@7787
sudo /opt/sqreader/src/deploy/zerozone/dogrula.sh 7787
```

Geri alma: `SHA256SUMS` + `integrity_check` + `server_id` denetimi geçmeden hiçbir şeye dokunmaz; mevcut veriyi silmez,
`/var/lib/sqreader/7787/.geri-al-oncesi-<zaman>/` altına **taşır**; config'i geri yüklemez (farkı gösterir).
`export_audit.log` eklemeli bir denetim izi olduğundan **yerinde bırakılır** (yedektekiyle ezilmez). `export_state.json`
geri yüklenince Zero Zone'un yedekten sonraki onayları (ack) kaybolur; bu güvenli yöndür (onaysız kayıt retention'da korunur),
Zero Zone yeniden onaylayabilir.

### 15.1 Yedek SIRASI: önce reader, sonra Zero Zone veritabanı

1. **Önce reader yedeği** (`yedek-geri-al.sh yedek`), **sonra Zero Zone veritabanı yedeği.**

Gerekçe: Zero Zone veritabanı reader'dan **türetilmiş** veri (içe aktarılmış maçlar, senkron imleci
`WebPanel_SrSyncState.cursor`, ödül defteri) tutar. Bu sırayla ZZ yedeği reader yedeğinden **aynı zamanda ya da daha
yenidir**; ikisi birlikte geri yüklenince ZZ, reader'ın ilerisinde ya da eşit olur. ZZ'nin ilerde olması güvenlidir:
imleç reader'ın son maçını geçmez, yeniden içe aktarma (ve onunla DB dışındaki yan etkilerin: Discord, VIP, RCON
tekrarı) olmaz. Bedeli: ZZ'de olup geri yüklenen reader'da kaynağı bulunmayan maçlar olabilir; bunlar ZZ'de kalır
(aynalanmış replay ile) ve yeniden içe aktarılmaz.

Ters sıra (önce ZZ, sonra reader) reader'ın ZZ'yi kapsamasını sağlar ama geri yüklemede ZZ eski imleçten maçları
**yeniden** çeker; ödül defteri aynı DB'de olduğu için DB içi ödüller tutarlı yeniden uygulanır, **DB dışı yan etkiler
yinelenebilir**. Bu ödünleşim seçimi bu rehberin sırasını belirler. Zero Zone'un bu iki senaryodaki gerçek davranışı
**bu belgede test edilmedi**; canlı kullanımdan önce Zero Zone tarafında denenmeli.

2. Geri yüklerken: önce reader'ı geri yükleyin ve `dogrula.sh` ile doğrulayın; **sonra** ZZ veritabanını; Zero Zone
   senkronunu (`sources[].enabled`) reader hazır olana kadar kapalı tutun.
3. İki yedeğin zaman damgalarını (reader `MANIFEST.txt` `olusturma_utc`) ZZ yedeğiyle birlikte saklayın.

## 16. Kaldırma

```bash
sudo systemctl disable --now sqreader@7787.service sqreader-retention@7787.timer
# (7788 hiç etkinleştirilmediyse ona gerek yok; edildiyse aynısı 7788 için)
sudo rm /etc/systemd/system/sqreader@.service /etc/systemd/system/sqreader-retention@.service /etc/systemd/system/sqreader-retention@.timer
sudo systemctl daemon-reload
# VERİ (kayıtlar + istatistik) İSTEDİĞİNİZ ZAMAN, YEDEK ALDIKTAN SONRA:
#   sudo rm -rf /var/lib/sqreader/7787
sudo rm -f /etc/sqreader/7787.config.json /etc/sqreader/7787.env /etc/sqreader/7787.export-token
sudo rm -rf /opt/sqreader
sudo nft delete table inet sqreader_egress 2>/dev/null   # bölüm 3.4 kuralını eklediyseniz
sudo userdel sqreader
```

Kaldırma oyun sunucusunu etkilemez. Zero Zone'da ilgili `sources` girdisini kapatın ve Zero Zone'daki token dosyasını silin.

## 17. Yapılmayanlar (özet)

- Konteyner **privileged yapılmaz**; konteynerin ayarı, ağı, mount'ları değiştirilmez.
- **Docker soketi** unit'e verilmez; sqreader `docker` grubunda değildir.
- Oyun sunucusu/konteyneri **yeniden başlatılmaz**, durdurulmaz, sinyallenmez. Tickrate/oyun ayarı değişmez.
- `--pid` kullanılmaz; PID sabitlenmez. `kernel.yama.ptrace_scope` değiştirilmez.
- Okuyucu root olarak çalıştırılmaz (yalnız geçici teşhis, bölüm 7.6).
- Dış ağ, merkezi gönderim, otomatik güncelleme, Discord bildirimi yoktur.
- Canlı yayın (`export_live_enabled`) varsayılan KAPALIdır, yalnız token korumalı Export yolunda vardır ve 7788 için açılmaz (bölüm 10.5).
- HTTP tüm arayüzlere açılmaz; token config'e/depoya yazılmaz.
- Otomatik `git pull`/güncelleme yoktur; yükseltme ELLE ve sabit commit ile yapılır.

## 18. "Çalıştırılmadı" listesi

Aşağıdakilerin hiçbiri bu belgenin yazıldığı Windows makinesinde çalıştırılmadı; Linux host'ta ilk
kurulumda sırayla yapılmalıdır:

| # | Kontrol | Komut / nerede |
|---|---|---|
| 1 | Unit sözdizimi | `systemd-analyze verify sqreader@7787.service` (ve retention unit'leri) |
| 2 | nginx yapılandırması | `sudo nginx -t` |
| 3 | Yeteneklerin yeterliliği (`CAP_SYS_PTRACE` + `CAP_DAC_READ_SEARCH`) | `dogrula.sh` bölüm 5 (`CapEff`, `doctor`) |
| 4 | `ProtectHome=read-only` ile `/proc/<pid>/root` log erişimi | `dogrula.sh` bölüm 4 |
| 5 | `ptrace_scope` ve `hidepid` ile uyum | `dogrula.sh` bölüm 2 |
| 6 | 7787/7788'in `Port=` mu `QueryPort=` mu olduğu | `dogrula.sh` bölüm 3 |
| 7 | Süreç keşfi gerçek oyunla (tek eşleşme) | `dogrula.sh` bölüm 3; `sqreader doctor` |
| 8 | `doctor`/`summary` gerçek Squad sürümüyle | `dogrula.sh` bölüm 5-6 |
| 9 | Export API `info`/`health`/401 | `dogrula.sh` bölüm 9 |
| 10 | Loopback bağlama ve dış bağlantı yokluğu | `dogrula.sh` bölüm 10; nftables örneği (bölüm 3.4) SINANMADI |
| 11 | `MemoryDenyWriteExecute`, `SystemCallFilter` uyumu | bölüm 7.4 (kullanılmıyor) |
| 12 | Kaynak sınırları (`CPUQuota`, `MemoryMax`, `Nice`) | bölüm 14 (ölçülmedi) |
| 13 | Retention'ın gerçek kayıtlarda neyi sileceği; Zero Zone'un gerçek ack davranışı (ayar adları kaynakta doğrulandı, bölüm 13) | `--dry-run` (bölüm 13); `GET /export/v1/health` `pendingAck` |
| 14 | `yedek-geri-al.sh` gerçek `sqlite3`/`systemctl`/`runuser` ile | bölüm 15 (yalnız sahte araçlarla denendi) |
| 15 | `setpriv` ile yetenek kümesi kurulumu (`dogrula.sh`) | `dogrula.sh` bölüm 5 (`CapEff` denetimi) |
| 16 | `net_policy`/`net_allow_hosts` ve `retention_*` anahtarlarının gerçek host'ta beklenen etkisi (anahtar adları ve varsayılanlar kaynakta doğrulandı) | `dogrula.sh` bölüm 10 |
| 17 | Zero Zone'un yedek sırası senaryolarında davranışı | Zero Zone tarafında (bölüm 15.1) |
| 18 | Uyumsuz oyun sürümünde "kayıt durur, görünür hata" | gerçek bir oyun güncellemesinden sonra (bölüm 9) |
| 19 | `ExecStartPre=sqreader check-instance %i`: yanlış örnek adı/config ile servis başlamıyor (çıkış 2); bozuk config JSON'u varsayılana dönmüyor | bölüm 6; gerçek systemd gerekir (mantık `tests/test_zz_target_guard.py` ile sınandı) |
| 20 | Ölçütsüz `serve` (Export API açık, `squad_port`/`squad_query_port`/`expected_container_id` yok) çıkış 2 ile reddediliyor; `require_target` davranışı | bölüm 5.2 (anahtar tablosu); gerçek host |
| 21 | Token üretim komutu (`sudo sh -c 'umask 077; openssl rand -hex 32 > ...'`), izinler ve `sqreader` kullanıcısının okuyabilmesi | bölüm 5.1 (Linux host, sudo/chown) |
| 22 | nginx `client_max_body_size 32k` ile 100 kimlikli ack paketinin (ZZ tarafında en çok 3800 bayt; reader sınırı 24576) 200 dönmesi ve control isteğinin (<= 4096 bayt) sınırda çalışması | `sudo nginx -t`; ZZ ack kuyruğu (ZZ `CANLI-DOGRULAMA.md` G2) |
| 23 | Canlı yayın (`export_live_enabled: true`): SSE'nin nginx arkasında donmadan akması (`proxy_buffering off`, `gzip off`, `limit_conn`), `hb` olaylarının 5 sn'de bir gelmesi, 3. abonenin 503 `live_busy` alması | bölüm 10.5.4-10.5.5; gerçek nginx + Linux host gerekir |
| 24 | Canlı yayının gerçek oyun sunucusunda CPU/RAM etkisi (0/1/2 abone); tick süresine etkisi | bölüm 10.5.6 (yalnız Windows'ta sentetik golden ile yerel ölçüm var; gerçek sunucuda ÇALIŞTIRILMADI) |
| 25 | Gerçek kare boyutu (golden'daki 8 oyunculu kare gerçeği temsil etmez) ve `live_ring_max_mb` yeterliliği | canlıda `GET /health/deep` `live.ringBytes` (yalnız loopback) |

### 18.1 `dogrula.sh` ve Zero Zone canlı doğrulama listesi

Zero Zone deposundaki `docs/squadreader/CANLI-DOGRULAMA.md` canlıda yapılacak doğrulamaların TEK listesidir; bu tablo
`dogrula.sh` çıktı bölümlerinin o listedeki maddelerle eşlemesidir. `dogrula.sh` yalnız bu makineyi (oyun host'u) kapsar;
ağ, veritabanı, veri anlamı, site ve geçiş provası maddeleri Zero Zone tarafındadır.

| `dogrula.sh` bölümü | CANLI-DOGRULAMA.md maddesi |
|---|---|
| 1. İşletim sistemi / çekirdek | A8 (systemd unit, sözdizimi) |
| 2. `ptrace_scope` ve `/proc` görünürlüğü | A4 |
| 3. Oyun süreçleri (PID, `Port=`/`QueryPort=`, konteyner) | A1, A2, A7 |
| 4. `/proc/<pid>/root` altında log erişimi | A8 |
| 5. `version` / `doctor` / `summary` / `selftest` (yetenekler) | A3, A5 |
| 6. Oyun sürümü / bellek düzeni uyumu | A6 |
| 7. Disk alanı | A12, B7 (ack ve SR diski) |
| 8. systemd unit durumu | A8 |
| 9. Export API (`info`, `health`, kimliksiz 401) | B3, B4 |
| 10. Dış ağ politikası | B6 |

`dogrula.sh`'in kapsamadığı ve Zero Zone listesinde olan maddeler: A9 (kaynak ölçümü, bölüm 14), A10 (ilk gerçek maç
`finalized`), A11 (reader yeniden başlaması ve `unverified` maç), B1/B2/B5 (ağ yolu, TLS, yönlendirme), C (veritabanı),
D (veri anlamı: EOS, kills/deaths, revive yönü, kazanan, oynama süresi, komutan), E (site), F (geçiş provası).

## 19. Lisans notu

Bu yazılım **AGPL-3.0-or-later + Commons Clause** ile lisanslıdır (`LICENSE`; lisans sahibi: Cagri Anil Okumus).
Sonuçları:

- **Değiştirilmiş kaynak erişilebilir tutulmalıdır.** AGPL'nin ağ üzerinden kullanıcıya hizmet veren değiştirilmiş
  sürümler için kaynağı sunma yükümlülüğü vardır; bu çatalın (`https://github.com/HYD7/squadreader`) ve dağıtılan **tam commit'in**
  herkese erişilebilir kalması, replay görüntüleyicideki "Kaynak kodu" bağlantısının (build zamanında
  `VITE_ZZ_SOURCE_URL`) buna işaret etmesi gerekir. Hukuki değerlendirme için lisansı ve bir uzmanı esas alın; bu rehber
  hukuki görüş değildir.
- **`LICENSE` ve `NOTICE` dosyaları korunur** (silinmez, değiştirilmez). `NOTICE`: `icons/` ve `sqmaps/` Offworld
  Industries'e aittir; `data/static/` üçüncü taraf türevidir ve bu lisansın kapsamı dışındadır.
- Commons Clause **satışı** kısıtlar; sunucu işletmeyi (örn. bağış/ücretli whitelist) satış saymaması bir
  NOT ile belirtilmiştir (`LICENSE`), yine de kendi durumunuz için lisansı okuyun.
- Zero Zone tarafındaki gizlilik kuralları (takma ad, `isAdmin` çıkarılması) bu makinenin dışındadır; okuyucunun
  Export API'si ham alanları (`is_admin`, bellek adresleri dahil) süzmeden verir ve token'la korunur. Bu yüzden token
  sızıntısı ciddidir (bölüm 10.3).
