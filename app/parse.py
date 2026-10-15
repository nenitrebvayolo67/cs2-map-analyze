"""Zpracování CS2 dema (běží v samostatném procesu).

Časový model (ověřeno na reálném demu):
  * začátek aktivního kola = událost round_freeze_end; ve stejném ticku platí
    game_time == round_start_time (m_fRoundStartTime z herních pravidel),
  * čas v kole = game_time - round_start_time  -> přímo herní čas, bez předpokladu tickrate,
  * tickrate se pouze ODVOZUJE z dema (Δtick / Δgame_time), aby šlo vybrat ticky ~1× za sekundu.
Ukládají se pozice VŠECH hráčů (cache je pak použitelná i pro jiné budoucí soupeře).
"""
from __future__ import annotations

import math

from .config import BUY_WINDOW_S, MAX_ROUND_S, PARSER_VERSION, SAMPLE_STEP_S

PROPS = ["X", "Y", "Z", "team_num", "is_alive", "is_warmup_period", "game_time",
         "round_start_time", "total_rounds_played", "last_place_name", "current_equip_value"]


class ParseError(Exception):
    pass


def _ticks_of(p, event: str) -> list[int]:
    try:
        df = p.parse_event(event)
    except Exception:
        return []
    if df is None or len(df) == 0 or "tick" not in df:
        return []
    return sorted({int(t) for t in df["tick"]})


def derive_tickrate(p, start: int, other: int) -> float:
    a, b = sorted((start, other))
    if b - a < 64:
        b = a + 640
    df = p.parse_ticks(["game_time"], ticks=[a, b])
    if df is None or len(df) == 0:
        raise ParseError("Nelze přečíst herní čas z dema.")
    g = df.groupby("tick")["game_time"].first()
    if a not in g.index or b not in g.index:
        raise ParseError("Nelze odvodit tickrate (chybí herní čas).")
    dt = float(g[b]) - float(g[a])
    if dt <= 0:
        raise ParseError("Herní čas v demu neroste – demo je poškozené.")
    rate = (b - a) / dt
    if not 8 <= rate <= 256:
        raise ParseError(f"Nepravděpodobný tickrate {rate:.1f}.")
    return rate


def round_windows(freeze: list[int], ends: list[int], tickrate: float) -> list[tuple[int, int]]:
    """(start, end) aktivní části každého kola: od konce freeze time do round_end."""
    wins = []
    for i, s in enumerate(freeze):
        nxt = freeze[i + 1] if i + 1 < len(freeze) else None
        end = next((e for e in ends if e > s and (nxt is None or e < nxt)), None)
        if end is None:
            end = (nxt - 1) if nxt else s + int(MAX_ROUND_S * tickrate)
        end = min(end, s + int(MAX_ROUND_S * tickrate))
        if end > s:
            wins.append((s, end))
    return wins


def sample_ticks(wins: list[tuple[int, int]], tickrate: float) -> tuple[list[int], dict[int, int]]:
    ticks, owner = [], {}
    step = SAMPLE_STEP_S * tickrate
    for wi, (s, e) in enumerate(wins):
        k = 0
        while True:
            t = s + int(round(k * step))
            if t > e:
                break
            ticks.append(t)
            owner[t] = wi
            k += 1
        if e not in owner:  # poslední stav kola (kdo přežil)
            ticks.append(e)
            owner[e] = wi
    return ticks, owner


def parse_demo(path: str) -> dict:
    from demoparser2 import DemoParser

    p = DemoParser(str(path))
    header = p.parse_header() or {}
    map_name = str(header.get("map_name") or "").lower()
    if not map_name:
        raise ParseError("Demo neobsahuje název mapy.")
    freeze = _ticks_of(p, "round_freeze_end")
    ends = _ticks_of(p, "round_end")
    if not freeze:
        raise ParseError("Demo neobsahuje žádné kolo.")
    tickrate = derive_tickrate(p, freeze[0], freeze[-1] if len(freeze) > 1 else freeze[0])
    wins = round_windows(freeze, ends, tickrate)
    ticks, owner = sample_ticks(wins, tickrate)
    df = p.parse_ticks(PROPS, ticks=ticks)
    if df is None or len(df) == 0:
        raise ParseError("Demo neobsahuje pozice hráčů.")
    missing = [c for c in ("X", "Y", "Z", "team_num", "is_alive", "steamid", "tick") if c not in df]
    if missing:
        raise ParseError(f"Demo neobsahuje potřebná pole: {', '.join(missing)}")

    df = df[df["tick"].isin(owner.keys())].copy()
    df["win"] = df["tick"].map(owner)
    df["steamid"] = df["steamid"].astype(str)

    # čas od začátku aktivního kola: herní čas (preferováno), jinak ticky / tickrate
    starts = {wi: s for wi, (s, _) in enumerate(wins)}
    time_source = "game_time"
    if "game_time" in df and "round_start_time" in df and df["round_start_time"].notna().all():
        df["t"] = df["game_time"].astype(float) - df["round_start_time"].astype(float)
        # kontrola: v ticku konce freeze time musí být t ≈ 0
        first = df[df["tick"] == df["win"].map(starts)]
        if len(first) and float(first["t"].abs().max()) > 0.25:
            time_source = "ticks"
    else:
        time_source = "ticks"
    if time_source == "ticks":
        df["t"] = (df["tick"] - df["win"].map(starts)) / tickrate
    df = df[df["t"] > -0.25]
    df["t"] = df["t"].clip(lower=0.0)

    players: dict[str, str] = {}
    rounds_out: list[tuple] = []
    samples_out: list[tuple] = []
    seen_round_no: dict[int, int] = {}

    for wi, g in df.groupby("win", sort=True):
        s = starts[wi]
        g0 = g[g["tick"] == s]
        if len(g0) == 0:
            continue
        if "is_warmup_period" in g0 and bool(g0["is_warmup_period"].fillna(False).astype(bool).mean() > 0.5):
            continue
        if "total_rounds_played" in g0 and g0["total_rounds_played"].notna().any():
            round_no = int(g0["total_rounds_played"].mode().iloc[0]) + 1
        else:
            round_no = wi + 1
        seen_round_no[round_no] = wi  # restart kola -> platí poslední výskyt
    keep = set(seen_round_no.values())
    round_of = {wi: rn for rn, wi in seen_round_no.items()}
    # délka aktivní části kola = čas v posledním vzorku (tick round_end)
    round_len = {wi: round(float(g["t"].max()), 3) for wi, g in df[df["win"].isin(keep)].groupby("win")}

    for (wi, sid), g in df[df["win"].isin(keep)].groupby(["win", "steamid"], sort=True):
        s = starts[wi]
        g = g.sort_values("tick")
        g_start = g[g["tick"] == s]
        if len(g_start) == 0:
            continue  # hráč nebyl na začátku kola na serveru
        team = int(g_start["team_num"].iloc[0]) if not math.isnan(float(g_start["team_num"].iloc[0])) else 0
        if team not in (2, 3):
            continue
        side = "T" if team == 2 else "CT"
        g = g[g["team_num"] == team]  # řádky z jiné strany (nemělo by nastat) se zahodí
        if "name" in g:
            players[sid] = str(g["name"].iloc[-1])
        alive = g[g["is_alive"].astype(bool)]
        dead = g[~g["is_alive"].astype(bool)]
        if len(g_start) and not bool(g_start["is_alive"].iloc[0]):
            continue  # na začátku kola mrtvý = nehrál
        death_t = float(dead["t"].min()) if len(dead) else None
        equip = None
        if "current_equip_value" in alive:
            buy = alive[alive["t"] <= BUY_WINDOW_S]["current_equip_value"].dropna()
            if len(buy):
                equip = int(buy.max())
        rn = round_of[wi]
        rounds_out.append((rn, sid, side, equip, death_t, round_len[wi]))
        places = alive["last_place_name"].fillna("").astype(str) if "last_place_name" in alive else [""] * len(alive)
        for t, x, y, z, pl in zip(alive["t"], alive["X"], alive["Y"], alive["Z"], places):
            samples_out.append((rn, sid, round(float(t), 3), float(x), float(y), float(z), pl))

    if not rounds_out:
        raise ParseError("V demu nejsou žádná platná kola.")
    return {
        "parser_version": PARSER_VERSION,
        "map": map_name,
        "tickrate": round(tickrate, 3),
        "time_source": time_source,
        "n_rounds": len(keep),
        "players": players,
        "rounds": rounds_out,
        "samples": samples_out,
    }
