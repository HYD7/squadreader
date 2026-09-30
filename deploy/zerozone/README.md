# Zero Zone dağıtım paketi (Linux host)

SquadReader'ı Zero Zone için oyun sunucusuyla aynı Linux makinede, asgari yetkiyle,
dış ağa çıkmadan çalıştırmak için dosyalar. Adım adım rehber: **[KURULUM-TR.md](KURULUM-TR.md)**.

> Bu dosyalar Windows geliştirme makinesinde yazıldı. systemd, nginx ve gerçek oyun
> sürecine karşı hiçbir şey çalıştırılmadı. Yerelde yalnız sözdizimi denetimi
> (`bash -n`, JSON ayrıştırma) ve sahte araçlarla sınırlı betik denemesi yapıldı.

| Dosya | Ne işe yarar |
|---|---|
| `sqreader@.service` | Örnek başına okuyucu unit'i (örnek adı = oyun portu). Yalnız `sqreader@7787` etkinleştirilir. `ExecStartPre=sqreader check-instance %i` örnek adının config'teki `squad_port` YA DA `squad_query_port` ile eşitliğini denetler (ikisi de yoksa ya da hiçbiri eşleşmiyorsa servis başlamaz). Export API açıkken `squad_port`/`squad_query_port`/`expected_container_id`'den biri zorunludur (`require_target`); bozuk config varsayılana dönmez (çıkış 2). |
| `sqreader-retention@.service`, `sqreader-retention@.timer` | Günlük kayıt saklama. **Kayıtları SİLER**: Export API açıkken yalnız Zero Zone'un onayladığı (ack) bitmiş kayıtları yaş/boyut kuralıyla siler; parametresiz `sqreader retention` artık `retention_*` config varsayılanlarıyla siler (KURULUM-TR.md bölüm 13). Önce `--dry-run`. |
| `sqreader.7787.config.json.example` | 7787 için config (`server_id: zz-7787`, `net_policy: local-only`, push/güncelleme kapalı, `export_token_file`, `export_state_dir`, `retention_*`). |
| `sqreader.7788.config.json.example` | 7788 için config. 7788 **varsayılan olarak açılmaz**. |
| `nginx-export.conf.example` | Zero Zone web sunucusunun Export API'ye TLS + IP izin listesiyle ulaşması için örnek (yalnız `/export/v1/`). `client_max_body_size 32k` (ack gövdesi en çok 24576 bayt, control 4096 bayt). |
| `dogrula.sh` | Salt-okunur teşhis: `sudo ./dogrula.sh 7787`. Hiçbir şeyi değiştirmez. Bölümlerinin Zero Zone `docs/squadreader/CANLI-DOGRULAMA.md` maddeleriyle eşlemesi: KURULUM-TR.md bölüm 18.1. |
| `yedek-geri-al.sh` | `yedek <port> <dizin>` ve `geri-al <port> <yedek> [--dry-run]`. |
| `KURULUM-TR.md` | Kurulum, yetkiler, doğrulama, bakım, kaldırma. |

Bu okuyucu Zero Zone'un **tek istatistik ve EXP kaynağıdır** (kullanıcı kararı 2026-09-29; Zero Zone `docs/squadreader/KARARLAR.md`). Okuyucu durursa (ör. oyun güncellemesi sonrası) Zero Zone o sunucu için istatistik ve EXP üretmez; yedek motor yoktur. Canlıda yapılacak doğrulamaların tamamı Zero Zone deposunda `docs/squadreader/CANLI-DOGRULAMA.md` içindedir.

Değişmeyen kurallar: `--pid` kullanılmaz; HTTP yalnız `127.0.0.1`; token dosyada
(`/etc/sqreader/<port>.export-token`, 0600), config'te değil; oyun sunucusu/konteyneri
yeniden başlatılmaz; Docker soketi verilmez; hiçbir konteyner ayrıcalıklı yapılmaz.

## Upstream'den sapmalar (fork değişiklik notu)

- Upstream'den davranış sapması: config dosyası VAR ama bozuksa ya da boşsa (geçersiz JSON, boş dosya, JSON nesnesi olmayan içerik, okunamayan dosya) `sqreader/config.py` sessizce varsayılana dönmez; `ConfigError` fırlatır ve her komut çıkış kodu 2 ile durur (upstream varsayılanlarla devam ederdi ve oyun portu ölçütünü kaybedip bulduğu ilk sunucuya bağlanabilirdi). Config dosyası hiç yoksa upstream'in sıfır-config davranışı aynen sürer.
- Fork özelliği, upstream'de YOK, varsayılan KAPALI: Export API üzerinde **canlı yayın** (`GET /export/v1/live/stream` SSE + `GET /export/v1/live/snapshot`; `sqreader/livehub.py`, `sqreader/live_fake.py`). Config anahtarları `export_live_enabled` (false), `live_max_clients` (2), `live_ring_seconds` (30), `live_ring_max_mb` (32). Kapalıyken iki yol da 404 döner; hiçbir zaman herkese açık `/stream` ya da `/latest` yoktur, yalnız token korumalı Export yolu vardır. Akış ham EOS/ad/adres taşır; temizleme yalnız Zero Zone sunucu tarafında yapılır (`PRIVACY.md`). Canlı yayın kapalıyken tick döngüsüne eklenen maliyet bir `None` denetimidir. Açma, sınırlar, nginx ve CPU/RAM ölçümü: KURULUM-TR.md bölüm 10.5. 7788 için açılmaz.

Lisans: AGPL-3.0-or-later + Commons Clause. `LICENSE` ve `NOTICE` korunur (bkz.
KURULUM-TR.md, son bölüm).
