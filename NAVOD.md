# CS2 Map Analyze – návod

Lokální aplikace pro Windows: vložíš odkaz na FACEIT matchroom a uvidíš, kde tvoji soupeři
skutečně stáli v jejich posledních zápasech na vybrané mapě. Bez předplatného, vše běží u tebe v PC.

## Instalace (jednou, ~5 minut)

1. Rozbal ZIP do libovolné složky (např. `Dokumenty\cs2-map-analyze`).
2. Získej **FACEIT API klíč** (zdarma):
   developers.faceit.com → přihlásit → **App Studio** → *Create app*. Vyplň:
   - **App Name:** `CS2 Map Analyze`
   - **Organization:** `Rylop`
   - **Description:** `Personal CS2 analytics tool for analyzing opponents' historical positioning using FACEIT match demos.`
   - **API Key Name:** `CS2-Map-Analyze-Server`

   Potom v aplikaci otevři záložku **API keys** → *Create* → typ **Server side** a zkopíruj vygenerovaný klíč.
   Skutečný API klíč ukládej pouze do místního souboru `.env`, nikdy do projektu ani do Gitu.
3. Dvojklik na **`SETUP.bat`**.
   - Najde Python (když chybí, zkusí ho nainstalovat přes `winget`).
   - Vytvoří vlastní prostředí a nainstaluje knihovny.
   - Zeptá se na tvoji FACEIT přezdívku a API klíč a **ověří je proti FACEIT**.
4. Hotovo.

## Používání

1. Dvojklik na **`START.bat`** – otevře se prohlížeč (`http://127.0.0.1:8765`).
2. Vlož odkaz na matchroom a klikni **Analyzovat**. Můžeš to udělat i před vetem, aplikace na mapu počká.
3. Na mapě přepínáš: hráče (kliknutím na kartu), T/CT, čas kola, heatmapu nebo body,
   ekonomiku kola a váhu (kola / zápasy / čas). Najetím myší na mapu uvidíš statistiku oblasti.

Okno `START.bat` nech otevřené. Jeho zavřením aplikaci ukončíš.

## Automatické stahování dem: důležité

FACEIT vydává dema jen přes **Downloads API**, ke kterému musí přístup schválit FACEIT:

- Žádost: https://fce.gg/downloads-api-application (FACEIT uvádí odpověď do 30 dní).
- Po schválení spusť znovu `SETUP.bat` a vlož token (nebo ho doplň do `.env` jako `FACEIT_DOWNLOAD_TOKEN`).

**Bez tokenu** aplikace funguje (pozná soupeře, počká na veto, najde jejich zápasy na mapě), ale dema
nestáhne. V části „Co bylo ověřeno“ přesně uvidíš, která dema chybí a proč.
Dočasné řešení: demo stažené z FACEITu vlož do složky `data\import`. Musí mít v názvu ID zápasu,
což soubory z FACEITu mají. Aplikace ho při další analýze použije.

## Jak číst výsledky

- **Podíl kol** (výchozí): každé kolo hráče má váhu 1. Kdo stál 20 s na jednom místě, počítá se jako 1 kolo, ne 20.
- **Podíl zápasů**: každý zápas má váhu 1.
- **Čas**: kolik času tam hráči strávili.
- **Body**: 1 tečka = 1 kolo v daném časovém intervalu.
- **Oblasti** jsou herní callouty přímo z dema (např. TopofMid, BombsiteA).
- **Ekonomika** se určuje podle průměrné hodnoty vybavení strany na konci nákupního okna:
  pistolová = první kolo poločasu a ≤ 1250 $, eco < 2000 $, force < 3500 $, full-buy ≥ 3500 $.
  Pokud nejsou data aspoň od 3 hráčů, kolo je „Neznámá“.
- Čas v kole se počítá od **konce freeze time** z herního času v demu.

Stav **Hotovo** se zobrazí jen tehdy, když se podařilo zpracovat všechny zápasy na mapě a každý soupeř
má 30 dohledaných zápasů. Jinak uvidíš **Částečně hotovo** a přesné počty.

## Úložiště a soukromí

- Klíče jsou jen v souboru `.env` na tvém PC. Nikomu ho neposílej.
- Server poslouchá jen na `127.0.0.1` (není dostupný ze sítě).
- Stažená dema se po zpracování mažou. Pozice zůstávají v `data\cache.db`, takže se stejné demo znovu nestahuje.
- Celou cache smažeš smazáním složky `data`.

## Když něco nefunguje

| Hláška | Co udělat |
|---|---|
| „Chybí FACEIT_API_KEY“ | Spusť `SETUP.bat` |
| „Tvůj FACEIT účet v tomto zápase není“ | Zkontroluj přezdívku (spusť `SETUP.bat`) |
| „FACEIT odmítl přístup“ | Klíč je neplatný nebo není typu *Server side* |
| „Chybí schválený přístup k FACEIT Downloads API“ | Viz kapitola o automatickém stahování výše |
| „Nepodařilo se stáhnout mapové podklady“ | Je potřeba internet (GitHub) při prvním použití mapy |
