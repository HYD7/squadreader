# Zero Zone dağıtım paketi (Linux host)

SquadReader'ı Zero Zone için oyun sunucusuyla aynı Linux makinede, asgari yetkiyle,
dış ağa çıkmadan çalıştırmak için dosyalar. Adım adım rehber: **[KURULUM-TR.md](KURULUM-TR.md)**.

> Bu dosyalar Windows geliştirme makinesinde yazıldı. systemd, nginx ve gerçek oyun
> sürecine karşı hiçbir şey çalıştırılmadı. Yerelde yalnız sözdizimi denetimi
> (`bash -n`, JSON ayrıştırma) ve sahte araçlarla sınırlı betik denemesi yapıldı.

| Dosya | Ne işe yarar |
|---|---|
| `sqreader@.service` | Örnek başına okuyucu unit'i (örnek adı = oyun portu). Yalnız `sqreader@7787` etkinleştirilir. |
| `sqreader-retention@.service`, `sqreader-retention@.timer` | Günlük kayıt saklama (eski kayıtları siler; dikkatli okuyun). |
| `sqreader.7787.config.json.example` | 7787 için config (`server_id: zz-7787`, yerel ağ politikası, push/güncelleme kapalı). |
| `sqreader.7788.config.json.example` | 7788 için config. 7788 **varsayılan olarak açılmaz**. |
| `nginx-export.conf.example` | Zero Zone web sunucusunun Export API'ye TLS + IP izin listesiyle ulaşması için örnek (yalnız `/export/v1/`). |
| `dogrula.sh` | Salt-okunur teşhis: `sudo ./dogrula.sh 7787`. Hiçbir şeyi değiştirmez. |
| `yedek-geri-al.sh` | `yedek <port> <dizin>` ve `geri-al <port> <yedek> [--dry-run]`. |
| `KURULUM-TR.md` | Kurulum, yetkiler, doğrulama, bakım, kaldırma. |

Değişmeyen kurallar: `--pid` kullanılmaz; HTTP yalnız `127.0.0.1`; token dosyada
(`/etc/sqreader/<port>.export-token`, 0600), config'te değil; oyun sunucusu/konteyneri
yeniden başlatılmaz; Docker soketi verilmez; hiçbir konteyner ayrıcalıklı yapılmaz.

Lisans: AGPL-3.0-or-later + Commons Clause. `LICENSE` ve `NOTICE` korunur (bkz.
KURULUM-TR.md, son bölüm).
