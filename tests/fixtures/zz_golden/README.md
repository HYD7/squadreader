# Zero Zone golden verileri (Export API v1)

Bu dizindeki dosyalar, SquadReader Export API v1'in (`sqreader/export_api.py`)
sentetik fixture veri kümesi üzerinde verdiği yanıtlardır. Zero Zone testleri
bunları gerçek bir SquadReader olmadan okuyabilsin diye burada tutulur.

**Bunlar sentetiktir.** Oyuncu adları, EOS kimlikleri, konumlar, sayaçlar ve
skorlar `tests/zz_fixture.py` içinde yazılmıştır; hiçbiri gerçek bir Squad
sunucusunun belleğinden okunmadı. Dosyalar gerçek oyunun sayaç anlamlarını
(ör. bir teamkill'in kill sayılıp sayılmadığını) KANITLAMAZ. Yalnızca yanıt
şeklini ve iki sunucunun verisinin karışmadığını sınamak içindir. İçinde gerçek
bir token, sunucu dosya yolu ya da bellek adresi yoktur (kayıt karelerindeki
`_addr` alanları fixture'a bilerek konmuş, uydurma değerlerdir; Zero Zone tarafı
gizlilik allowlist'i ile bunları atmalıdır).

## Dizin düzeni

```
zz-7787/                      # Fallujah_RAAS_v1, maç A
  info.json                   # GET /export/v1/info
  matches.json                # GET /export/v1/matches?limit=100
  match-<matchId>.json        # GET /export/v1/matches/<matchId>
  recording-<recId>.ndjson.gz # GET /export/v1/recordings/<recId>  (gzip'lenmiş NDJSON)
  recording-<recId>.headers.json
zz-7788/                      # Narva_RAAS_v1, maç B (aynı saat penceresi, başka sunucu)
  (aynı beş dosya)
```

Yalnızca `finalized` maçlar vardır: zz-7787 için A (`5a7e0a00-7787-4a00-8000-00000000a001`),
zz-7788 için B (`5a7e0b00-7788-4a00-8000-00000000b001`). Doğrulanamamış (C) ve
sürmekte olan (D) maç bilerek yoktur; Export API bunları zaten listelemez.

`recording-<recId>.headers.json` yanıt başlıklarından şunları saklar:
`Content-Type`, `Cache-Control`, `X-Sqr-Match-Id`, `X-Sqr-Total-Frames` ve
saklanan `.ndjson.gz` dosyasının kodlaması olan `Content-Encoding: gzip`.
`X-Sqr-Total-Frames`, açılmış NDJSON'daki satır sayısına eşittir.

`info.json` içindeki `squadPort` (7787 / 7788) ve `containerId`
(`zz-container-<port>`) değerleri de uydurmadır; betik bunları yalnızca alanların
dolu hâlini göstermek için verir. `readerState` fixture'da canlı bir okuyucu
olmadığı için `"unknown"`dır.

## Nasıl üretildi

```
python scripts/dump_zz_golden.py            # bu dizini yeniden yazar
python scripts/dump_zz_golden.py --out <dizin>
```

Betik `tests/zz_fixture.py` ile fixture üretir, Export API'yi süreç içinde
gerçek bir HTTP sunucusu olarak (127.0.0.1, rastgele port, rastgele token) her
iki sunucu için başlatır ve yanıtları buraya yazar. Yalnızca `zz-7787/` ve
`zz-7788/` dizinleri yeniden yazılır; bu README'ye dokunulmaz.

Çıktı deterministiktir: betiği iki kez çalıştırmak bayt farkı üretmez.

- JSON dosyaları sabit düzenle (`indent=2`, UTF-8, `ensure_ascii=False`, sonda `\n`) yazılır.
- Kayıt gövdesi sunucudan gzip ile alınır, açılır ve `gzip.compress(..., compresslevel=9, mtime=0)`
  ile yeniden sıkıştırılır. Sunucunun kendi gzip çerçevesinin işletim sistemi baytı
  platforma göre değiştiği için onun ham baytları saklanmaz.
- `readerVersion` (`sqreader.__version__`) ve `statsSchemaVersion` olduğu gibi kalır;
  sürüm değişirse betik yeniden çalıştırılıp fark commit'lenmelidir.

`tests/test_export_api.py::test_golden_files_match_regeneration` betiği geçici bir
dizinde yeniden çalıştırır ve sonucu bu dosyalarla bayt bayt karşılaştırır.
