# Stav ověření (Mappio DIY 2.0)

Legenda: **Impl.** = implementováno · **Test** = ověřeno automatickým testem · **Demo** = ověřeno na skutečném CS2 demu ·
**Živé API** = ověřeno proti živému FACEIT API · **Blokováno** = nelze ověřit bez externího přístupu

Automatické testy: **107 prošlo, 0 selhalo** (`python -m pytest tests`). Testy se skutečným demem potřebují soubor
`tests/fixtures/mirage_sample.dem` (CS2 demo de_mirage, 9 kol). V ZIPu kvůli velikosti (58 MB) není, bez něj se přeskočí.

| Oblast | Impl. | Test | Demo | Živé API |
|---|---|---|---|---|
| Validace URL matchroomu (jen faceit.com, jedno ID) | ✅ | ✅ | – | – |
| Identifikace soupeřů přes player_id, chyba místo hádání | ✅ | ✅ | – | ❌ neověřeno |
| Čekání na veto (polling 5 s, zrušený zápas, timeout 20 min) | ✅ | ✅ | – | ❌ neověřeno |
| Posledních 30 dokončených zápasů, řazení, `from` param., stránkování | ✅ | ✅ | – | ❌ neověřeno |
| Filtrování mapy (veto → záloha /stats), deduplikace zápasů | ✅ | ✅ | – | ❌ neověřeno |
| Normalizace `demo_url` (seznam / řetězec / prázdné / objekty) | ✅ | ✅ | – | ❌ neověřeno |
| Downloads API (POST, payload.download_url) | ✅ | ✅ (mock) | – | ⛔ Blokováno – vyžaduje schválený token |
| Rate limit 429 / 5xx s backoffem, 401/403/404 | ✅ | ✅ | – | ❌ neověřeno |
| Bezpečné stahování (HTTPS, limit velikosti), gzip/zstd/bz2 s limitem, kontrola hlavičky CS2 | ✅ | ✅ | ✅ | – |
| Parsování: čas od konce freeze time z herního času, tickrate odvozený z dema | ✅ | ✅ | ✅ (64 tick, t=0 na spawnu) | – |
| Strany T/CT, jen živí hráči, smrt, délka kola, restart kol, warmup | ✅ | ✅ | ✅ | – |
| Oblasti mapy z herních calloutů (`last_place_name`) | ✅ | ✅ | ✅ | – |
| Ekonomika (pistol/eco/force/full/neznámá) | ✅ | ✅ (syntetická data) | ✅ (běží) | – |
| Normalizace: kola / zápasy / čas, body = 1 kolo | ✅ | ✅ | ✅ | – |
| Transformace souřadnic = vzorec awpy (ověřeno porovnáním) | ✅ | ✅ | – | – |
| Stažení ověřených map. podkladů (awpy-data releases), bezpečné rozbalení | ✅ | ✅ (mock GitHub) | – | ⛔ GitHub releases tady nedostupné |
| Vizuální shoda pozic s reálným radarem | – | – | ⛔ | ⛔ ověří se na tvém PC |
| SQLite cache, atomický zápis, obnova po přerušení, invalidace verze parseru | ✅ | ✅ | ✅ | – |
| Úklid dem a limit místa na disku | ✅ | ✅ | – | – |
| Paralelní stahování a parsování (procesy, spawn jako na Windows) | ✅ | ✅ | ✅ | – |
| Měření 8 kroků (vč. vykreslení v prohlížeči) | ✅ | ✅ | ✅ | – |
| Server jen 127.0.0.1, kontrola Host/Origin, whitelist souborů, CSP | ✅ | ✅ | – | – |
| Webové UI (desktop i mobil) | ✅ | ✅ (prohlížeč) | ✅ | – |
| Windows SETUP/START.bat | ✅ | ⛔ neověřeno – nemám Windows | – | – |

## Naměřený výkon (zde, 2 CPU)
- Parsování 1 dema (9 kol, 58 MB): **1,75 s**; 8 dem paralelně na 2 procesech: **1,16 s/demo**.
- Plné FACEIT demo (24+ kol) bude zhruba 2–3× větší, takže ~4–5 s na jádro.
- Při opakované analýze se už zpracovaná dema berou z cache (0 stažení, 0 parsování).

## Proč 60 s není zaručeno
Typicky 15–30 unikátních dem × desítky MB. Úzké hrdlo je **stahování**: zhruba 1–2 GB.
Při 1 Gbit/s je to ~15–20 s, při 100 Mbit/s 2–3 minuty. Parsování na 4–8jádrovém PC trvá ~20–40 s
a běží souběžně se stahováním. Historie a detaily zápasů se načítají už během veta.

## Návrh: preventivní aktualizace cache (neimplementováno)
Pro hráče, se kterými se potkáváš opakovaně, by šlo v noci stáhnout a zpracovat jejich nová dema.
Je to legální jen se schváleným Downloads API a při dodržení limitů FACEIT. Než budeš mít token,
nemá smysl to stavět.
