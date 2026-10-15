"""Nastavení aplikace: cesty, konstanty a načtení .env (klíče nikdy nejdou do frontendu)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# Verze parseru. Změna = všechna dema zpracovaná starší verzí se při dalším
# použití zpracují znovu (pozice v DB se nahradí).
PARSER_VERSION = 3

# Analytické definice (intervaly, ekonomika, váhy) se počítají až při dotazu
# z uložených surových pozic, takže jejich změna nevyžaduje nové parsování.

# Časové intervaly od skutečného začátku aktivního kola (konec freeze time), v sekundách.
INTERVALS = [
    ("0-5", 0, 5), ("5-10", 5, 10), ("10-15", 10, 15), ("15-20", 15, 20),
    ("20-30", 20, 30), ("30-45", 30, 45), ("45-60", 45, 60), ("60+", 60, None),
]
SAMPLE_STEP_S = 1.0        # vzorkování pozic: 1x za sekundu herního času
MAX_ROUND_S = 200.0        # strop délky kola pro vzorkování (1:55 + bomba 40 s + rezerva)
BUY_WINDOW_S = 20.0        # okno nákupu po konci freeze time (mp_buytime 20 s)

HISTORY_SIZE = 30          # posledních N dokončených zápasů každého soupeře
VETO_POLL_S = 5            # interval dotazování na konec veta
VETO_TIMEOUT_S = 20 * 60   # max. čekání na veto

MAX_COMPRESSED_BYTES = 1_500 * 1024 * 1024    # 1,5 GB stažený soubor
MAX_DEMO_BYTES = 3 * 1024 * 1024 * 1024       # 3 GB rozbalené demo
MAX_ZIP_ENTRY_BYTES = 200 * 1024 * 1024       # obrázek radaru v ZIPu


@dataclass
class Settings:
    api_key: str = ""
    download_token: str = ""
    nickname: str = ""
    player_id: str = ""
    port: int = 8765
    host: str = "127.0.0.1"
    data_dir: Path = ROOT / "data"
    keep_demos: bool = False
    max_demo_disk_gb: float = 5.0
    download_workers: int = 4
    parse_workers: int = 0          # 0 = automaticky podle CPU

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cache.db"

    @property
    def demo_dir(self) -> Path:
        return self.data_dir / "demos"

    @property
    def import_dir(self) -> Path:
        return self.data_dir / "import"

    @property
    def map_dir(self) -> Path:
        return self.data_dir / "maps"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.demo_dir, self.import_dir, self.map_dir):
            d.mkdir(parents=True, exist_ok=True)


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_settings(env_path: Path | None = None) -> Settings:
    env = _parse_env_file(env_path or ROOT / ".env")
    get = lambda k, d="": os.environ.get(k, env.get(k, d))  # noqa: E731
    s = Settings(
        api_key=get("FACEIT_API_KEY"),
        download_token=get("FACEIT_DOWNLOAD_TOKEN"),
        nickname=get("FACEIT_NICKNAME"),
        player_id=get("FACEIT_PLAYER_ID"),
        port=int(get("PORT", "8765") or 8765),
        keep_demos=get("KEEP_DEMOS", "0").lower() in ("1", "true", "ano", "yes"),
        max_demo_disk_gb=float(get("MAX_DEMO_DISK_GB", "5") or 5),
        download_workers=max(1, min(8, int(get("DOWNLOAD_WORKERS", "4") or 4))),
        parse_workers=max(0, int(get("PARSE_WORKERS", "0") or 0)),
    )
    if get("DATA_DIR"):
        s.data_dir = Path(get("DATA_DIR")).expanduser()
    # Placeholdery z .env.example nejsou skutečné hodnoty
    for attr in ("api_key", "download_token", "nickname", "player_id"):
        if getattr(s, attr).upper().startswith("SEM_"):
            setattr(s, attr, "")
    return s
