"""Průvodce nastavením .env (spouští SETUP.bat). Ověří klíč a přezdívku proti FACEIT API."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from .config import ROOT, _parse_env_file
from .faceit import FaceitClient, FaceitError

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
_SAFE = re.compile(r"^[A-Za-z0-9_\-.:+/=]{0,4000}$")


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Zapíše hodnoty do .env; zachová komentáře a ostatní řádky."""
    for k, v in updates.items():
        if not _SAFE.match(v):
            raise ValueError(f"Hodnota pro {k} obsahuje nepovolené znaky.")
    base = path if path.exists() else EXAMPLE
    lines = base.read_text(encoding="utf-8-sig").splitlines() if base.exists() else []
    done = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def ask(prompt: str, current: str, secret: bool = False) -> str:
    shown = " [ponechat současnou]" if current else ""
    val = (getpass.getpass if secret else input)(f"{prompt}{shown}: ").strip()
    return val or current


def main() -> int:
    cur = _parse_env_file(ENV) or _parse_env_file(EXAMPLE)
    clean = lambda v: "" if v.upper().startswith("SEM_") else v  # noqa: E731
    print("\n=== Nastavení Mappio DIY ===")
    print("API klíč: https://developers.faceit.com -> App Studio -> tvoje aplikace -> API keys -> typ 'Server side'.")
    nick = ask("Tvoje FACEIT přezdívka", clean(cur.get("FACEIT_NICKNAME", "")))
    key = ask("FACEIT API klíč (Server side)", clean(cur.get("FACEIT_API_KEY", "")), secret=True)
    token = ask("FACEIT Downloads API token (nech prázdné, pokud ho zatím nemáš)",
                clean(cur.get("FACEIT_DOWNLOAD_TOKEN", "")), secret=True)
    updates = {"FACEIT_NICKNAME": nick, "FACEIT_API_KEY": key, "FACEIT_DOWNLOAD_TOKEN": token}
    if nick and key:
        try:
            pid = FaceitClient(key, max_retries=1).find_player_id(nick)
            updates["FACEIT_PLAYER_ID"] = pid
            print(f"OK: klíč funguje a účet '{nick}' byl nalezen.")
        except FaceitError as e:
            print(f"POZOR: ověření se nepodařilo: {e}")
            print("Nastavení se uloží, ale zkontroluj klíč a přezdívku.")
            updates["FACEIT_PLAYER_ID"] = ""
    try:
        write_env(ENV, updates)
    except ValueError as e:
        print(f"Chyba: {e}")
        return 1
    print(f"Uloženo do {ENV}")
    if not token:
        print("Bez Downloads API tokenu nepůjde stahovat dema automaticky (viz NAVOD.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
