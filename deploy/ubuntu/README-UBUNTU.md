# SquadReader: Ubuntu tek komut kurulumu (SR'ye özgü parça)

SquadReader'ı Ubuntu 22.04/24.04'te oyun sunucusuyla aynı makinede, ayrı `sqreader` kullanıcısıyla ve asgari yetkiyle kurmak için **ana orkestrasyon Zero Zone deposundadır**: `zerozone-squadjs/deploy/ubuntu/kur.sh`. Adım adım anlatım, güncelleme, geri alma ve sık hatalar: **`zerozone-squadjs/deploy/ubuntu/KURULUM-UBUNTU-TR.md`**. Bu klasör yalnız SR deposundan başlamak isteyenler için ince bir sarmalayıcı içerir.

> Bu dosyalar Windows'ta yazıldı; Ubuntu'da **çalıştırılmadı**. Önce her zaman `--kuru-kosu`.

| Dosya | Ne işe yarar |
|---|---|
| `sqreader-kur.sh` | Zero Zone deposunu bulur (`../zerozone-squadjs`, `--zz-deposu DIZIN` ya da `--zz-url URL --zz-referans SHA`) ve `kur.sh --yalniz squadreader --sr-kaynak <bu depo>` çağırır. Diğer tüm seçenekler `kur.sh`'ye aynen geçer. |

```bash
sudo bash deploy/ubuntu/sqreader-kur.sh --kuru-kosu      # hiçbir şeyi değiştirmez, adımları yazar
sudo bash deploy/ubuntu/sqreader-kur.sh --evet           # etkileşimsiz kurulum
```

Kurulum mevcut dosyaları **yeniden kullanır** (kopyalamaz): `deploy/zerozone/sqreader@.service`, `sqreader-retention@.service/.timer`, `sqreader.<port>.config.json.example`, `nginx-export.conf.example` (ayrı makine düzeni için türetilir), `yedek-geri-al.sh` ve doğrulama için `dogrula.sh`. SR portu (`--sr-port`) varsayılan **7787**; 7788 yalnız `--ikinci-sunucu-onayi` ile. Token `umask 077; openssl rand -hex 32` ile üretilir (`sqreader:sqreader 0600`), yeniden üretilmez.

Güncelleme ve doğrulama Zero Zone deposundaki `guncelle.sh` / `dogrula.sh` ile yapılır. `guncelle.sh` SR'yi **önce durdurmaz**; kayıt sürerken (`GET /export/v1/health` → `activeRecording`) SR yeniden başlatılmaz.
