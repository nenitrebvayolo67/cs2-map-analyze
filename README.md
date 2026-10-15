# CS2 Map Analyze

Windowsová aplikace pro analýzu předchozích zápasů soupeřů z FACEIT. Zobrazuje jejich pozice na vybrané mapě; aplikace běží lokálně na vašem počítači.

Zdrojový kód: [github.com/nenitrebvayolo67/cs2-map-analyze](https://github.com/nenitrebvayolo67/cs2-map-analyze).

## Instalace a použití

Podrobný návod v češtině je v souboru [NAVOD.md](NAVOD.md). Pro rychlé spuštění:

1. Nainstalujte Python 3.11 nebo novější.
2. Spusťte `SETUP.bat` a zadejte vlastní FACEIT přezdívku a API klíč.
3. Spusťte `START.bat` a otevřete aplikaci na `http://127.0.0.1:8765`.

Automatické stahování dem vyžaduje schválený FACEIT Downloads API token. Bez něj aplikace podporuje ruční import dem; podrobnosti jsou v návodu.

## Soukromí a klíče

Klíče ukládejte pouze do místního souboru `.env`. Tento soubor je ignorován Gitem a nesmí se zveřejňovat. `.env.example` obsahuje pouze ukázkové hodnoty.

## FACEIT

Tento projekt není oficiálně spojený s FACEIT. Použití FACEIT API podléhá podmínkám FACEIT.
