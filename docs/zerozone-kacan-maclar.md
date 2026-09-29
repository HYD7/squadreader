# Dışa aktarılmayan ("kaçan") maçlar ve bilinen ölçüm sınırları

Bu belge Zero Zone entegrasyonu için yazıldı. Hedef kurulumda SquadReader, Zero Zone'un TEK
istatistik ve EXP kaynağıdır. Export API yalnız `status='final'` VE `end_state='finalized'` maçları
verir (`sqreader/export_api.py`, `_matches` ve `_bundle`). SquadReader bir maçın SONUNU göremezse
o maç `unverified` kalır ve HİÇ dışa aktarılmaz: o maçta oyuncular istatistik de EXP de almaz.

Bu belge üç şeyi yanıtlar:

1. Bir maç hangi koşulda `finalized`, `unverified`, `legacy` olur ve kaçan maç nasıl görünür kılınır.
2. `sqreader/stats.py:246-248` notundaki "123 / 821" oranı neyi ölçtü, neyi ölçmedi.
3. Sayaçların ölçümünde bilinen sınırlar (en büyük değer kuralı, okunamayan sayacın 0 olması).

Kaynak referansları `dosya:satır` biçimindedir ve bu deponun bu sürümüne aittir. Kaynaktan
kanıtlanamayan her şey **"doğrulanmadı"** diye işaretlidir. Hiçbir oran uydurulmamıştır: güncel oran
BİLİNMİYOR; canlıda `health.unverified.ratioLast30d` ile ölçülür (bölüm 2).

Okuma motorunun davranışı bu görevde DEĞİŞTİRİLMEDİ. Eklenen tek şey görünürlüktür: iki salt-okunur
sorgu (bölüm 2) ve testler.

---

## 1. Bir maçın sonu: `finalized`, `unverified`, `legacy`, açık

`matches` tablosunda iki ayrı sütun vardır: `status` (`open` / `final`) ve `end_state`
(`finalized` / `unverified` / `legacy`, sütun `stats.py:242-263` göçü `_m006` ile geldi).

### 1.1 `finalized`: maçın bittiği GÖZLENDİ

Tek üretim yolu `StatsStore.record_tick` içindedir (`stats.py:627-722`):

- Tick'te geçerli bir `matchId` YOK ve `matchState` bilinen bir "oynanmıyor" durumudur
  (`INACTIVE_MATCH_STATES`: `WaitingToStart`, `Warmup`, `WaitingPostMatch`, `EndState`;
  `recording_lifecycle.py:17-22`) ve
- bu durum ARDIŞIK `MATCH_TRANSITION_CONFIRM_TICKS = 3` tick boyunca sürmüştür
  (`recording_lifecycle.py:13`, `stats.py:649-667`) ve
- o anda bu süreçte açık bir maç vardır (`open_id is not None`, `stats.py:687`).

O zaman `_finalize_match(..., end_state="finalized")` çağrılır (`stats.py:688-692`, `987-1039`).
Yalnız bu durumda kazanan hesaplanır: son bilet karşılaştırması, 0 = eşit (`stats.py:1011-1013`).
ELO da yalnız `unverified` olmayan maçı derecelendirir (`elo.py:312`).

Kaynaktan çıkan dikkat noktaları:

- "Oynanmıyor" listesinde `WaitingPostMatch` ve `EndState` yanında `WaitingToStart` ile `Warmup` da
  vardır. Yani `finalized` = "3 ardışık oynanmıyor tick'i görüldü"dür; maçın yalnız normal bitişini
  değil, maç ortasında oyunun bu durumlara düşmesini de kapsayabilir. Böyle bir olayın canlıda olup
  olmadığı **doğrulanmadı**.
- `finalize_open_match(confirmed=True)` üretimde HİÇBİR yerden çağrılmaz; yalnız testler çağırır
  (`grep`: `cli.py:1674` ve `cli.py:1776` varsayılanı, yani `unverified`'ı kullanır).

### 1.2 `unverified`: maç durdu ama SONU görülmedi

`status='final'`, `end_state='unverified'`, `winner_team=NULL`. Üretim yolları:

| `end_reason` (kayıtta görünen) | Kaynak | Ne zaman |
|---|---|---|
| `startup sweep: left open by a hard kill` | `stats.py:604-614` | `StatsStore` her açıldığında `status='open'` kalmış HER satır `final/unverified` yapılır (sunucu kimliğine bakılmaz). Sert kapanış, oyun sürecinin kaybı ve okuyucunun maç sürerken bilerek satırı açık bırakması (`cli.py:1670-1679`) sonrası bir sonraki açılışta olur. |
| `superseded by a newer match` | `stats.py:785-791` | Yeni bir `matchId` açılırken bu sunucuya ait başka açık satır kalmışsa. |
| `superseded: matchId changed` | `stats.py:696-703` | Aynı süreçte `matchId` değişti, ama arada 3 ardışık "oynanmıyor" tick'i GÖRÜLMEDİ (tick boşluğu, okunamayan `matchState`; belirsiz tick seriyi sıfırlar, `stats.py:660-664`). |
| `serve shutdown` | `stats.py:724-739`, `cli.py:1673-1674` | Okuyucu kapanırken `matchState` `InProgress` DEĞİL ama 3 tick'lik onay henüz dolmamış. |
| `backfill end` | `cli.py:1776` | `stats-backfill` bitişinde açık kalan maç. |

Ek olarak `record_tick` içindeki bir istisna cli'de yutulur (`cli.py:1454-1459`): o tick kaybolur.
Kayıp tick'ler bitiş onayını geciktirebilir ya da engelleyebilir (koddan çıkarım; sıklığı
**doğrulanmadı**).

`unverified` bir maçın `ended_at` değeri GERÇEK bitiş değil, okuyucunun o maçı en son gördüğü andır
(`stats.py:1015`, `_update_match_running` her tick `ended_at`'i günceller, `stats.py:815`). Bu
yüzden API'de `lastSeenAt` adıyla da verilir.

### 1.3 `legacy`: göçten ÖNCE yazılmış satırlar

`_m006` var olan `status='final' AND end_state IS NULL` satırları `legacy` damgalar
(`stats.py:262-263`): "çoğu sağlam, toptan `unverified` yapmak iyi geçmişi cezalandırır"
(`stats.py:251-255`). Güncel yazıcı `legacy` ÜRETMEZ. Okuma tarafı `COALESCE(end_state,'legacy')`
kullanır (`elo.py:300`, `stats.py:1426`). Export `legacy` satırları da DIŞA AKTARMAZ; kaçan maç
görünürlüğü (bölüm 2) de onları saymaz. Bu, eski arşivi olan bir kurulumda ayrı bir kayıptır ve bu
görevde ölçülmedi.

### 1.4 Açık maç ve "yeniden açılan" maç

`status='open'` maç ne `finalized` ne `unverified` sayılır; export'ta ve bu belgedeki sayaçlarda
yoktur.

**Okuyucu maç ortasında yeniden başlarsa aynı `matchId` ile maç yeniden açılıp sonradan
`finalized` olabilir mi? Evet, koşullu.** Kaynak akışı:

1. Kapanışta maç sürüyorsa satır bilerek `open` bırakılır (`cli.py:1656-1680`).
2. Bir sonraki `StatsStore` açılışında süpürme satırı `final/unverified` yapar (`stats.py:604-614`).
3. Aynı `matchId` ile ilk `InProgress` tick'inde `_open_match` `ON CONFLICT` ile satırı `status='open'`
   yapar; `started_at` DEĞİŞMEZ, `end_state` bayat `unverified` kalır ama `status='open'` olduğu için
   hiçbir sayaçta yoktur (`stats.py:792-807`).
4. Bitiş gözlenirse `_finalize_match` `end_state='finalized'` yazar; maç ONA kadar biriken sayaçlarla
   (tick sayacı restart boyunca sürer) tek kez dışa aktarılır.

Bu, `tests/test_export_unverified.py::test_restart_mid_match_reopens_and_can_finalize` ile sınanır.
Sonuçları:

- 2. ile 3. adım arası (yeniden başlatma ile ilk `InProgress` tick'i; oyun süreci bekleniyorsa
  dakikalar sürebilir) canlı bir maç GEÇİCİ olarak `unverified` listesinde ve sayacında görünür.
  Bu geçici görünüm doğru davranıştır: maç o anda gerçekten doğrulanmamış durumdadır.
- Yeniden açılma yalnız `matchId` AYNIYSA olur. Lisanslı sunucuda OWI MatchID yeniden başlatmadan
  etkilenmez (canlıda **doğrulanmadı**). Lisanssız sunucuda kimliği `SyntheticMatchIds` üretir ve
  durumunu en çok her 30 sn'de bir `synth_match.json` dosyasına yazar (`synth_match.py:268-283`,
  `_PERSIST_EVERY_SEC`). Durum dosyası kaybolur ya da eskirse (`serverStartTimestamp` ya da harita
  değişti, `elapsedSec` geri gitti, çıpa kayması `%10 x boşluk + 60 sn` zarfını aştı,
  `synth_match.py:187-203`) 3 tick sonra YENİ kimlik üretilir: eski satır `unverified` kalır, maçın
  kalanı mid-match başlayan YENİ satır olarak açılır ve bitişi gözlenirse `finalized` olup KISMİ
  sayaçlarla dışa aktarılır. Bu, koddan çıkarımdır; canlıda **doğrulanmadı**.
- Oyun sunucusu yeniden başlarsa yeni `matchId` gelir; eski satır sonsuza dek `unverified` kalır.
- Maç okuyucu KAPALIYKEN biterse (ya da yeniden başlatma sonrası ilk görülen durum "oynanmıyor" ise)
  yeni `StatsStore`'da `open_id` boştur ve hiçbir şey finalize etmez (`stats.py:687`); satır
  sonsuza dek `unverified` kalır.

### 1.5 Kayıt (`.sqrx`) tarafı ayrıdır

`.meta.json` içindeki `recordingState` (`active` / `finalized` / `unverified`) istatistik
`end_state`'inden bağımsızdır (`recorder.py:272-331`, `export_state.py:462-473`). Export bir maçı
yalnız istatistik `finalized` ise verir; ona bağlı kayıtları ayrıca yalnız sidecar `finalized` ise
ekler (`export_api.py`, `_SidecarIndex.finalized`). İki taraf aynı 3 tick eşiğini kullanır
(`stats.py:30-38`), ama ikisi ayrı durum tutar.

---

## 2. Kaçan maçların görünür kılınması (yeni)

İkisi de mevcut Export API ile AYNI güvenliği kullanır: `Authorization: Bearer <token>`, token yoksa
ya da API kapalıysa `404`, hatalar `{"schema":"sqr-export-1","error":"<kod>"}`. Yalnız `GET`/`HEAD`.
İkisi de yalnız bu sunucunun (`server_id`) maçlarını görür.

`matches`, `matches/<id>` ve `recordings/<id>` uçlarının "yalnız `finalized`" garantisi DEĞİŞMEDİ
(`test_matches_still_only_a_and_c_is_404`).

### 2.1 `GET /export/v1/health` içindeki yeni alan `unverified`

`null` yalnız istatistik veritabanı okunamıyorsa. Aksi halde:

```json
"unverified": {
  "total": 1,
  "last24h": 1,
  "last7d": 1,
  "last30d": 1,
  "lastAt": 1788289200,
  "finalizedLast7d": 1,
  "finalizedLast30d": 1,
  "ratioLast30d": 0.5,
  "training": { "total": 0, "last30d": 0, "lastAt": null, "finalizedLast30d": 0 },
  "asOf": 1788292800
}
```

- Sayılan: `status='final'` VE `end_state='unverified'` VE `server_id` bu sunucu. Açık (devam eden ya da
  yeniden açılmış) maç sayılmaz. `legacy` sayılmaz (bölüm 1.3).
- Zaman ölçüsü `ended_at` (gözlenen son an, epoch sn). Pencereler `asOf` anına göre `>=`:
  24 saat = 86400 sn, 7 gün, 30 gün. `asOf` = okuyucunun isteği yanıtladığı andaki saati.
  `total` tüm zamanlardır; `lastAt` = en yeni `unverified` maçın `ended_at`'i, yoksa `null`.
- `finalizedLast7d`/`finalizedLast30d`: aynı pencerede `finalized` maç sayısı (aynı sunucu).
- `ratioLast30d` = `last30d / (last30d + finalizedLast30d)`, 4 basamağa yuvarlanır; payda 0 ise `null`.
  Bu oran "kaçan / (kaçan + verilen)"dir. Yeni kurulumda payda küçükken dalgalanır.
- Eğitim katmanı: `training` altındaki sayılar ayrı tutulur, ÜST DÜZEY sayılara DAHİL DEĞİLDİR
  (Zero Zone eğitim katmanına ödül vermiyor). Ayrım `jensen` ya da `training` sözcüğüne bakar
  (`map_name`, `layer_name`, `game_mode` sütunlarına, büyük/küçük harf duyarsız): Zero Zone'un
  `isTrainingLayer` deseniyle AYNI, ama Zero Zone'un katman nesnesindeki fazladan alanlar SquadReader'da
  yoktur; adı bu sözcükleri içermeyen bir eğitim katmanı ayrılamaz (**doğrulanmadı**).

### 2.2 `GET /export/v1/unverified?after=<cursor>&limit=<n>`

Sayfalama, `limit` (varsayılan 50, en çok 100, 100'den büyük 100'e kısılır), `after` imleci
(`"<ended_at>:<matchId>"`), `nextCursor` ve `400 bad_query` / `bad_limit` / `bad_cursor` kuralları
`matches` ucuyla AYNI koddan geçer. Sıra `(ended_at ASC, match_id ASC)`. Bilinmeyen sorgu anahtarı
yok sayılır. `unverified/<id>` gibi alt yollar `404`. Veritabanı okunamazsa `503 stats_unavailable`.

```json
{
  "schema": "sqr-export-1",
  "serverId": "zz-7787",
  "unverifiedMatches": [
    {
      "matchId": "5a7e0c00-7787-4a00-8000-00000000c001",
      "serverId": "zz-7787",
      "startedAt": 1788289000,
      "lastSeenAt": 1788289200,
      "endedAt": 1788289200,
      "endState": "unverified",
      "endReason": "serve shutdown",
      "mapName": "Yehorivka",
      "layerName": "Yehorivka_AAS_v1",
      "gameMode": "AAS",
      "peakPlayers": 6,
      "tickCount": 20,
      "playerCount": 6,
      "isTraining": false
    }
  ],
  "nextCursor": null
}
```

(Örnek değerler biçimi göstermek içindir.)

- Dizi adı BİLEREK `matches` değil `unverifiedMatches`: içe aktarma koduna yanlışlıkla bağlanmasın.
- `lastSeenAt` ve `endedAt` AYNI değerdir (gözlenen son an; GERÇEK bitiş değildir). `endedAt`, `matches`
  ucuyla ve imleçle simetri içindir.
- `endReason`: bölüm 1.2 tablosundaki serbest metin (en çok 200 karakter); bir uyarı ya da neden
  göstermek için kullanılır, ayrıştırmaya dayanılmamalıdır.
- `playerCount` = o maç için yazılmış `player_matches` satır sayısı (SAYI; oyuncu kimliği ya da
  istatistik DEĞİL). `peakPlayers` = gözlenen en yüksek oyuncu sayısı.
- `isTraining`: bölüm 2.1'deki eğitim ayrımı.
- Yanıtta OYUNCU, EOS, sayaç, bilet, kazanan, paket özeti (`bundleSha256`) ya da kayıt YOKTUR
  (`test_unverified_has_no_player_or_stat_data`). Bu maçların istatistiği doğrulanmamıştır; Zero Zone'un
  bunları ödüle çevirmesi istenmez. Uç yalnız "hangi maçlar kaçtı" sorusunu yanıtlar.

### 2.3 Sınırlar

- Sayım geçici görünümü içerir (bölüm 1.4: yeniden başlatma ile ilk tick arası).
- Bir `unverified` maç sonradan `finalized` olabilir (bölüm 1.4): sayaç düşer ve maç `matches` içinde
  yeni bir `endedAt` ile belirir.
- Bu uç kaçan maçı KURTARMAZ; yalnız görünür kılar. Kurtarma ancak yeniden oynatılabilir bir kayıt
  ya da yönetici kararıyla olur; ikisi de bu görevin dışındadır.

---

## 3. `stats.py:246-248` notundaki "123 / 821" oranı

Not (`stats.py:242-263`, `_m006` docstring'i): "Measured on the live archive before this landed: 123 of
821 matches carried a fabricated result, and one was verified against its own replay — 8023 frames,
every one InProgress, no terminal state at all, stored as '273-203, team 1 won'."

Kaynağın söylediği ve söylemediği:

- Ölçüm `_m006` göçünden ÖNCEKİ arşivdedir ve "sahte sonuç taşıyan maç" sayısıdır: yani ESKİ mantığın
  (bir tek okunamayan/oynanmıyor tick'in maçı bitirmesi, ortadaki bir anlık görüntüden kazanan
  çıkarılması) ürettiği yanlış sonuçlar. ~%15 = 123/821 yalnız bu aritmetiktir; SquadReader'ın kendi
  hesabı.
- Bugünkü `unverified` oranı değildir. Kaynak, eski mantığın tek bir bozuk okumanın maçı bitirip o anki
  skoru yayımlamasına izin verdiğini söyler (`stats.py:638-642`); 123 sayısı bu tür yanlış bitirmeleri de
  içerir. Yeni mantıkta belirsiz tick maçı AÇIK tutar (`stats.py:660-664`) ve bitiş 3 ardışık tick ister
  (bölüm 1.1). Yani rakam güncel oran için ne üst ne alt sınırdır; güncel oran BİLİNMİYOR.
- Arşivin hangi sunuculara/sürüme ait olduğu notta YAZMIYOR; Zero Zone sunucuları olduğu
  **doğrulanmadı**. Not, `1.4.0` ilk işleme (`0167579`) ile depoya girdi; başka bağlam yok.
- Güncel oranı bu belge veremez. Ölçmek için: canlı kurulumda `GET /export/v1/health` içindeki
  `unverified.ratioLast30d` (ve `training` ayrımı) izlenir. Tahmin: `n` maç çok küçükse oran anlamsızdır.
- m006 sonrası davranış: `unverified` maç dışa aktarılmaz, kazanan `NULL`, ELO yok (bölüm 1.1-1.2).
  Eski `final` satırlar `legacy` olarak dışarıda kalır (bölüm 1.3).

Yönetici için: oran yüksekse en sık `endReason` değerine bakın (bölüm 1.2): `startup sweep` ağırlıklıysa
okuyucu yeniden başlıyor ya da oyun süreci kayboluyordur; `superseded` ağırlıklıysa tick boşlukları
ya da hızlı harita değişimi vardır. Bu eşleme koddan çıkarımdır, canlıda **doğrulanmadı**.

---

## 4. Bilinen ölçüm sınırları

Bu bölüm mevcut davranışı belgeler; hiçbiri bu görevde DEĞİŞTİRİLMEDİ.

### 4.1 Sayaçlar "en büyük değer" ile tutulur (yeniden bağlanma toplanmaz)

`player_matches` sayaçları upsert'te `MAX(mevcut, yeni)` ile yazılır (`stats.py:293-304` liste,
`stats.py:916-918` ve `904-927` SQL). Kodun kendi yorumları gerekçeyi şöyle verir: yeniden
bağlanmada oyun içi sayaç sıfırlanır ve en büyük değer toplamı asla düşürmesin
(`stats.py:204-205`, `293-295`, `306-307`).

Kaynaktan doğrulananlar:

- Oyuncu başına oyun içi sayaç maç içinde sıfırlanırsa (yeniden bağlanma), sonraki değerler önceki en
  büyük değeri AŞANA KADAR sayılmaz; TOPLANMAZ. Örnek: 5 öldürme, sıfırlanma, 3 öldürme daha, oyun 3
  gösterir, SquadReader 5 tutar; hepsi toplansaydı 8 olurdu. Bu, gerçek `StatsStore` ile sınanır
  (`test_counters_are_max_upserts_a_reset_is_not_added_up`).
- Kodda buna karşı bir ele alış YOKTUR: oturum başına taban çizgisi, "sayaç geriledi" algılaması ya da
  toplama yoktur. Yeniden bağlanmada tek özel durum, aynı EOS için eski ve yeni `SQPlayerState`'in
  aynı anda bulunduğu kısa sürede birini seçmektir (`snapshot.py:3934-3966`, sıfır olmayan skor
  tercih edilir); bu, sayacı toplamaz.
- Etkilenen alanlar: `_PM_COUNTERS_INT` (kills, vehicle_kills, deaths, woundeds, wounds, team_kills,
  is_commander, is_admin, supplies_delivered, fobs_built, fobs_destroyed, captures, defenses) ve
  `_PM_COUNTERS_REAL` (heal_points, revived_points, team_work_score, objective_score, combat_score,
  score, vehicle_damage).
- `kill_events` (log kaynaklı) bundan ETKİLENMEZ: olay olarak yazılır. Bu yüzden `kills` sayacı ile
  `kill_events` sayısı yeniden bağlanan oyuncuda ayrışabilir.
- Oyunun yeniden bağlanmada sayaçları GERÇEKTEN sıfırlayıp sıfırlamadığı bu depodan
  **doğrulanamadı**: yalnız kod yorumları öyle söylüyor, canlıda ölçülmedi.
- Sonuç: yeniden bağlanan oyuncu için `kills`/`deaths`/`objective_score` vb. EKSİK sayılmış olabilir
  (asla fazla). Zero Zone'da bu, o oyuncunun EXP'sini az verir. Kimin yeniden bağlandığı SquadReader
  tarafında izlenmez.
- Tek bir bozuk okuma da en büyük değeri kilitleyebilir; bunu engellemek için sınırlar vardır
  (`stats.py:341-343`: sayaç 5000, `379` araç öldürme 150, `405` revive 500 ve tamsayı, skor
  ±100 000; sınır dışı okuma "okunamadı" sayılır, bölüm 4.2).
- `score` ve `lives`, `_PM_DESC` ya da MAX kuralına farklı girer: ilk tick'te `score` okunamazsa satır
  `NULL` ile açılır ve SQLite'ta `MAX(NULL, x)` `NULL` verdiğinden `score` o maç boyunca `NULL` KALIR
  (deneyle görüldü). `score` sayaç değil görüntü verisi sayılmalıdır.
- `playtime_sec` = son görülme - ilk görülme (`stats.py:925`): yeniden bağlanma boşluğu ve maç
  ortasında girip çıkma dahil.

### 4.2 Okunamayan sayaç `0`'a çevrilir

`_upsert_players` her sayaçta `_count(...) or 0`, `_vk(...) or 0`, `_score(...) or 0.0`,
`_revive(...) or 0.0` vb. kullanır (`stats.py:863-892`). Sınır dışı ya da okunamayan değer `None` olur
(`stats.py:364-369`, `382-387`, `390-395`, `408-416`, `432-443`) ve `or 0` ile `0`'a döner. Sonuçlar:

- Veritabanında sayaç sütunları bu yolla NULL olmaz: "hiç okunamadı" ile "gerçekten 0" AYIRT EDİLEMEZ.
  Bir oyuncunun sayacı maç boyunca hiç okunamazsa `0` görünür (`test_unreadable_counter_becomes_zero_not_null`).
- Bir sayaç önce okunup sonra okunamazsa MAX son geçerli değeri korur, zarar yoktur.
- İstisnalar: `score` (`_score` `None` döner, kolonu doğrudan yazılır; bölüm 4.1) ve `lives` (`NULL` olabilir).
- Zero Zone tarafında bu, "0 ile eksik ayrılamaz" uyarısıdır. Bu görevde `unverified` ucu da
  bu ayrımı çözmez.

### 4.3 Diğer (kısa)

- Takım (`team_id`) son gözlenen değerdir; takım değişimi stats DB'de izlenmez (`stats.py:308-309`).
- Kazanan yalnız `finalized` maçta ve son bilet karşılaştırmasıyla belirlenir (`stats.py:1011-1013`).
  Terminal karede biletler okunamazsa son aktif kare kullanılır (`stats.py:1002-1010`).

---

## 5. Test ve doğrulama

- `tests/test_export_unverified.py`: `health.unverified` (C sayılır, D sayılmaz, A `finalizedLast*`,
  oran, pencereler, iki sunucu bir veritabanında karışmaz, eğitim ayrımı), `/export/v1/unverified`
  (yalnız C, oyuncu/istatistik yok, sayfalama, geçersiz imleç/limit, token, HEAD/405, export kapalıyken 404,
  503), `matches` hâlâ yalnız A ve C için `404`, yeniden başlatmada yeniden açılma, sayaç davranışı.
- Fixture: `tests/zz_fixture.py` (A `finalized`, C `unverified`, D aktif).
- Canlıda doğrulanmadı: gerçek `unverified` oranı, yeniden bağlanmada oyun sayaçlarının sıfırlanması,
  lisanslı sunucuda MatchID'nin yeniden başlatmada sabit kalması, lisanssız sunucuda durum dosyası
  kaybı senaryosu.
