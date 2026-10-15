"""FACEIT Data API v4 + Downloads API.

Použité endpointy (ověřeno proti docs.faceit.com):
  GET  /data/v4/matches/{match_id}                  – stav, týmy (roster), veto, demo_url
  GET  /data/v4/matches/{match_id}/stats            – záloha pro zjištění mapy
  GET  /data/v4/players?nickname=..&game=cs2        – převod přezdívky na player_id
  GET  /data/v4/players/{player_id}                 – steam ID (games.cs2.game_player_id)
  GET  /data/v4/players/{player_id}/history         – game, from, to, offset, limit(<=100)
       POZOR: 'from' má výchozí hodnotu "před měsícem" – proto ho posíláme explicitně.
  POST /download/v2/demos/download                  – {"resource_url"} -> payload.download_url
       vyžaduje schválený přístup (token se scope Downloads API).
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DATA_API = "https://open.faceit.com/data/v4"
DOWNLOAD_API = "https://open.faceit.com/download/v2/demos/download"
HISTORY_FROM_TS = 1262304000  # 1. 1. 2010 – aby API nepoužilo výchozí okno 1 měsíc

MATCH_ID_RE = re.compile(r"1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FULL_ID_RE = re.compile(rf"^{MATCH_ID_RE.pattern}$")
STEAM64_RE = re.compile(r"^7656\d{13}$")
MAP_RE = re.compile(r"^[a-z]{2,3}_[a-z0-9_]+$")


class FaceitError(Exception):
    """Chyba s českou zprávou pro uživatele."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class IdentificationError(FaceitError):
    pass


# ------------------------------------------------------------------ čisté funkce

def parse_match_input(text: str) -> str:
    """Z URL matchroomu (nebo samotného ID) vrátí match ID. Jinak FaceitError."""
    text = (text or "").strip()
    if _FULL_ID_RE.match(text):
        return text
    try:
        u = urllib.parse.urlparse(text)
    except ValueError:
        u = None
    if not u or u.scheme not in ("http", "https") or not u.hostname:
        raise FaceitError("Vlož odkaz na FACEIT matchroom (začíná https://www.faceit.com/…).")
    host = u.hostname.lower()
    if host != "faceit.com" and not host.endswith(".faceit.com"):
        raise FaceitError("Odkaz nevede na faceit.com.")
    ids = MATCH_ID_RE.findall(u.path)
    if len(set(ids)) != 1:
        raise FaceitError("V odkazu jsem nenašel jednoznačné ID zápasu (tvar 1-xxxxxxxx-xxxx-…).")
    return ids[0]


def normalize_demo_urls(value: Any) -> list[str]:
    """demo_url může být seznam řetězců (dokumentováno), ale bráníme se i jiným tvarům."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for it in items:
        if isinstance(it, dict):
            it = it.get("resource_url") or it.get("url") or it.get("demo_url")
        if isinstance(it, str):
            s = it.strip()
            if s.lower().startswith("https://") or s.lower().startswith("http://"):
                if s not in out:
                    out.append(s)
    return out


def match_status(match: dict) -> str:
    return str(match.get("status") or "").upper()


def picked_map(match: dict) -> str | None:
    """Mapa vybraná ve vetu, nebo None, pokud veto ještě neskončilo."""
    voting = match.get("voting")
    if not isinstance(voting, dict):
        return None
    mp = voting.get("map")
    if not isinstance(mp, dict):
        return None
    pick = mp.get("pick")
    if isinstance(pick, str):
        pick = [pick]
    if not isinstance(pick, list) or len(pick) != 1:
        return None  # 0 = veto běží, >1 = nejednoznačné (např. BO3) – nepředpokládáme
    m = str(pick[0]).strip().lower()
    return m if MAP_RE.match(m) else None


def map_from_stats(stats: dict) -> str | None:
    rounds = stats.get("rounds") if isinstance(stats, dict) else None
    if isinstance(rounds, list) and len(rounds) == 1:
        m = str((rounds[0].get("round_stats") or {}).get("Map") or "").strip().lower()
        return m if MAP_RE.match(m) else None
    return None


def rosters(match: dict) -> dict[str, list[dict]]:
    teams = match.get("teams")
    if not isinstance(teams, dict):
        return {}
    out = {}
    for key, team in teams.items():
        if isinstance(team, dict):
            r = team.get("roster") or team.get("players") or []
            out[key] = [p for p in r if isinstance(p, dict) and p.get("player_id")]
    return out


def team_name(match: dict, faction: str) -> str:
    t = (match.get("teams") or {}).get(faction) or {}
    return str(t.get("name") or faction)


def identify_opponents(match: dict, my_player_id: str) -> tuple[str, str, list[dict]]:
    """Vrátí (můj_tým, soupeřův_tým, soupeři). Při nejistotě vyhodí chybu – nikdy nehádá."""
    if not my_player_id:
        raise IdentificationError("Chybí tvoje FACEIT identita (FACEIT_NICKNAME v .env).")
    rs = rosters(match)
    if len(rs) != 2:
        raise IdentificationError("Zápas nemá dva týmy se soupiskou – nejde určit soupeře.")
    mine = [k for k, r in rs.items() if any(p["player_id"] == my_player_id for p in r)]
    if len(mine) == 0:
        raise IdentificationError("Tvůj FACEIT účet v tomto zápase není. Zkontroluj FACEIT_NICKNAME v .env.")
    if len(mine) > 1:
        raise IdentificationError("Tvůj účet je v obou týmech – data zápasu jsou nekonzistentní.")
    my_key = mine[0]
    opp_key = next(k for k in rs if k != my_key)
    opp = rs[opp_key]
    ids = [p["player_id"] for p in opp]
    if len(set(ids)) != len(ids):
        raise IdentificationError("Soupiska soupeře obsahuje duplicitní hráče.")
    if not opp:
        raise IdentificationError("Soupiska soupeře je prázdná.")
    return my_key, opp_key, opp


def _ts(item: dict, key: str) -> int:
    v = item.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def select_recent_finished(items: list[dict], exclude_match_id: str, n: int) -> list[dict]:
    """Posledních n DOKONČENÝCH zápasů od nejnovějšího (bez aktuálního zápasu a duplicit)."""
    seen, rows = set(), []
    for idx, it in enumerate(items):
        mid = it.get("match_id")
        if not isinstance(mid, str) or not _FULL_ID_RE.match(mid) or mid == exclude_match_id or mid in seen:
            continue
        if str(it.get("status") or "").lower() != "finished":
            continue
        seen.add(mid)
        rows.append((-(_ts(it, "finished_at") or _ts(it, "started_at")), idx, mid, it))
    rows.sort()
    return [{"match_id": r[2], "finished_at": _ts(r[3], "finished_at") or _ts(r[3], "started_at")}
            for r in rows[:n]]


def steam_id_of(player: dict) -> str | None:
    sid = str(player.get("game_player_id") or "").strip()
    return sid if STEAM64_RE.match(sid) else None


# ------------------------------------------------------------------ HTTP klient

Opener = Callable[[urllib.request.Request, float], Any]


def _default_open(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)


class FaceitClient:
    def __init__(self, api_key: str, download_token: str = "", opener: Opener | None = None,
                 max_retries: int = 5, sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise FaceitError("Chybí FACEIT_API_KEY v souboru .env.")
        self.api_key = api_key
        self.download_token = download_token
        self._open = opener or _default_open
        self.max_retries = max_retries
        self._sleep = sleep
        self.requests_made = 0

    # -- nízká úroveň
    def _request(self, req: urllib.request.Request, timeout: float = 20.0) -> dict:
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                self.requests_made += 1
                with self._open(req, timeout) as r:
                    raw = r.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise FaceitError("FACEIT záznam neexistuje (404).", 404) from e
                if e.code in (401, 403):
                    raise FaceitError("FACEIT odmítl přístup (klíč je neplatný nebo nemá oprávnění).", e.code) from e
                if e.code == 429 or 500 <= e.code < 600:
                    retry_after = 0.0
                    try:
                        retry_after = float(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError, AttributeError):
                        pass
                    last = e
                    self._sleep(min(30.0, max(retry_after, (2 ** attempt) * 0.5 + random.random() * 0.3)))
                    continue
                raise FaceitError(f"FACEIT vrátil chybu {e.code}.", e.code) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                self._sleep(min(10.0, (2 ** attempt) * 0.5))
                continue
            except json.JSONDecodeError as e:
                raise FaceitError("FACEIT vrátil neplatnou odpověď.") from e
        if isinstance(last, urllib.error.HTTPError) and last.code == 429:
            raise FaceitError("FACEIT omezil počet požadavků (429). Zkus to za chvíli.", 429)
        raise FaceitError("Nepodařilo se spojit s FACEIT (síť nebo výpadek).")

    def get(self, path: str, params: dict | None = None) -> dict:
        url = DATA_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})
        return self._request(req)

    # -- Data API
    def get_match(self, match_id: str) -> dict:
        return self.get(f"/matches/{match_id}")

    def get_match_stats(self, match_id: str) -> dict:
        return self.get(f"/matches/{match_id}/stats")

    def find_player_id(self, nickname: str) -> str:
        data = self.get("/players", {"nickname": nickname, "game": "cs2"})
        pid = data.get("player_id")
        if not pid:
            raise FaceitError(f"FACEIT hráč '{nickname}' nebyl nalezen.")
        return str(pid)

    def get_player(self, player_id: str) -> dict:
        return self.get(f"/players/{player_id}")

    def history(self, player_id: str, need: int, exclude_match_id: str = "") -> tuple[list[dict], int]:
        """Vrátí (vybrané zápasy, počet prošlých položek). Stránkuje, dokud nemá `need` dokončených."""
        items: list[dict] = []
        offset, page = 0, 100
        while offset <= 1000:
            data = self.get(f"/players/{player_id}/history",
                            {"game": "cs2", "from": HISTORY_FROM_TS, "to": int(time.time()) + 3600,
                             "offset": offset, "limit": page})
            batch = data.get("items") or []
            items.extend(b for b in batch if isinstance(b, dict))
            if len(select_recent_finished(items, exclude_match_id, need)) >= need or len(batch) < page:
                break
            offset += page
        return select_recent_finished(items, exclude_match_id, need), len(items)

    # -- Downloads API
    def signed_demo_url(self, resource_url: str) -> str:
        if not self.download_token:
            raise FaceitError("Chybí FACEIT_DOWNLOAD_TOKEN (schválený přístup k Downloads API).")
        body = json.dumps({"resource_url": resource_url}).encode()
        req = urllib.request.Request(DOWNLOAD_API, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.download_token}", "Content-Type": "application/json",
            "Accept": "application/json"})
        data = self._request(req)
        url = (data.get("payload") or {}).get("download_url") if isinstance(data.get("payload"), dict) else None
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            raise FaceitError("Downloads API nevrátilo platný odkaz ke stažení.")
        return url
