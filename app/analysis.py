"""Analýza pozic: intervaly, ekonomika, normalizace vah, heatmapa, oblasti mapy.

Normalizace (aby se pozice nepočítaly jako kola):
  * "rounds"  – každé kolo hráče má celkovou váhu 1, rozdělenou mezi jeho vzorky
                (stál-li 20 s na jednom místě, je to stále jedno kolo).  VÝCHOZÍ.
  * "matches" – každý zápas hráče má váhu 1 (zápas s 30 koly nepřebije zápas s 15).
  * "time"    – každý vzorek = 1 s herního času (čas strávený v oblasti).
Body (tečky): 1 tečka = 1 kolo v daném intervalu (pozice nejblíže středu intervalu).

Ekonomika (průměrná hodnota vybavení hráčů dané strany na konci nákupního okna):
  pistol: první kolo poločasu (kolo 1 nebo kolo po výměně stran) a průměr ≤ 1250 $
  eco < 2000 $  |  force 2000–3499 $  |  full-buy ≥ 3500 $
  unknown: méně než 3 hráči strany s údajem o vybavení.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import INTERVALS, SAMPLE_STEP_S

ECON_THRESHOLDS = {"pistol_max": 1250, "eco_max": 2000, "force_max": 3500, "min_players": 3}
ECON_LABELS = {"pistol": "Pistolová", "eco": "Eco", "force": "Force", "full": "Full-buy", "unknown": "Neznámá"}
WEIGHT_MODES = ("rounds", "matches", "time")
HEAT_BIN = 4      # px (radar 1024 px) – jemná mřížka heatmapy
CELL = 32         # px – oblast pro hover statistiky
RADAR = 1024
_BOUNDS = [a for _, a, _ in INTERVALS[1:]]   # [5,10,15,20,30,45,60]
INTERVAL_KEYS = [k for k, _, _ in INTERVALS]


def interval_index(t: np.ndarray | float) -> np.ndarray:
    """0-5 -> 0, 5-10 -> 1, …, 60+ -> 7. Hranice patří do vyššího intervalu (5.0 -> '5-10')."""
    return np.digitize(np.asarray(t, dtype=float), _BOUNDS, right=False)


def interval_mid(idx: int) -> float:
    _, a, b = INTERVALS[idx]
    return (a + b) / 2 if b is not None else a + 10.0


def classify_economy(rounds: pd.DataFrame) -> pd.DataFrame:
    """rounds: match_id, round_no, steam_id, side, equip -> (match_id, round_no, side, econ, team_equip)."""
    th = ECON_THRESHOLDS
    if rounds.empty:
        return pd.DataFrame(columns=["match_id", "round_no", "side", "econ", "team_equip"])
    grp = rounds.groupby(["match_id", "round_no", "side"])["equip"]
    team = pd.DataFrame({"team_equip": grp.mean(), "n_eq": grp.count()}).reset_index()

    # první kolo poločasu: kolo 1, nebo většina hráčů změnila stranu oproti předchozímu kolu
    sides = rounds.pivot_table(index=["match_id", "round_no"], columns="steam_id", values="side",
                               aggfunc="first")
    half_start = set()
    for mid, g in sides.groupby(level=0):
        g = g.droplevel(0).sort_index()
        prev = None
        for rn, row in g.iterrows():
            if rn == 1:
                half_start.add((mid, rn))
            elif prev is not None and prev[0] == rn - 1:
                common = row.notna() & prev[1].notna()
                if common.sum() >= 2 and (row[common] != prev[1][common]).mean() > 0.5:
                    half_start.add((mid, rn))
            prev = (rn, row)

    def cls(r):
        if r.n_eq < th["min_players"] or pd.isna(r.team_equip):
            return "unknown"
        if (r.match_id, r.round_no) in half_start and r.team_equip <= th["pistol_max"]:
            return "pistol"
        if r.team_equip < th["eco_max"]:
            return "eco"
        if r.team_equip < th["force_max"]:
            return "force"
        return "full"

    team["econ"] = [cls(r) for r in team.itertuples()]
    return team[["match_id", "round_no", "side", "econ", "team_equip"]]


def compute_weights(df: pd.DataFrame, mode: str) -> np.ndarray:
    """df musí mít match_id, round_no, steam_id. Vrací váhu každého vzorku."""
    if mode not in WEIGHT_MODES:
        raise ValueError(f"Neznámý režim vah: {mode}")
    if df.empty:
        return np.zeros(0)
    if mode == "time":
        return np.full(len(df), SAMPLE_STEP_S)
    keys = ["match_id", "round_no", "steam_id"]
    n_samples = df.groupby(keys)["t"].transform("size").to_numpy(dtype=float)
    w = 1.0 / n_samples
    if mode == "matches":
        n_rounds = df.groupby(["match_id", "steam_id"])["round_no"].transform("nunique").to_numpy(dtype=float)
        w = w / n_rounds
    return w


class Dataset:
    """Data jednoho výpočtu (soupeři + vybrané zápasy), připravená pro rychlé filtrování."""

    def __init__(self, rounds_rows: list[tuple], sample_rows: list[tuple], target_sids: list[str], mapinfo):
        self.map = mapinfo
        R = pd.DataFrame(rounds_rows, columns=["match_id", "round_no", "steam_id", "side", "equip",
                                               "death_t", "round_len"])
        econ = classify_economy(R)
        self.rounds_all = R
        R = R[R["steam_id"].isin(target_sids)].merge(econ, on=["match_id", "round_no", "side"], how="left")
        R["econ"] = R["econ"].fillna("unknown")
        self.rounds = R
        S = pd.DataFrame(sample_rows, columns=["match_id", "round_no", "steam_id", "t", "x", "y", "z", "place"])
        S = S.merge(R[["match_id", "round_no", "steam_id", "side", "econ"]],
                    on=["match_id", "round_no", "steam_id"], how="inner")
        px, py = mapinfo.to_px(S["x"].to_numpy(), S["y"].to_numpy()) if len(S) else (np.zeros(0), np.zeros(0))
        S["px"], S["py"] = px, py
        S["lvl"] = (np.array([mapinfo.is_lower(z) for z in S["z"]], dtype=int) if len(S) else np.zeros(0, int))
        S["iv"] = interval_index(S["t"].to_numpy()) if len(S) else np.zeros(0, int)
        self.samples = S

    # ------------------------------------------------------------------ dotaz
    def view(self, players: list[str] | None = None, side: str = "ALL", interval: str = "all",
             econ: str = "all", weight: str = "rounds", level: int = 0) -> dict:
        S, R = self.samples, self.rounds
        if players:
            S, R = S[S["steam_id"].isin(players)], R[R["steam_id"].isin(players)]
        if side in ("T", "CT"):
            S, R = S[S["side"] == side], R[R["side"] == side]
        if econ != "all":
            S, R = S[S["econ"] == econ], R[R["econ"] == econ]
        iv = None
        if interval != "all":
            if interval not in INTERVAL_KEYS:
                raise ValueError("Neznámý interval.")
            iv = INTERVAL_KEYS.index(interval)
            S = S[S["iv"] == iv]
        S = S[S["lvl"] == int(level)]
        S = S.copy()
        S["w"] = compute_weights(S, weight)
        total_w = float(S["w"].sum())

        # stav hráčů v intervalu (živý / eliminovaný / kolo skončilo) – z kol, ne z pozic
        a = INTERVALS[iv][1] if iv is not None else 0.0
        played = len(R)
        ended = int((R["round_len"] < a).sum()) if iv is not None else 0
        dead = int(((R["death_t"].notna()) & (R["death_t"] <= a) & ~(R["round_len"] < a)).sum()) if iv is not None else 0
        status = {"rounds_played": played, "alive_at_start": played - ended - dead,
                  "eliminated_before": dead, "round_over_before": ended}

        rkeys = ["match_id", "round_no", "steam_id"]
        rounds_with = int(S[rkeys].drop_duplicates().shape[0]) if len(S) else 0

        # heatmapa: jemné biny s vahami
        heat = []
        if len(S):
            bx = np.clip((S["px"] // HEAT_BIN).astype(int), 0, RADAR // HEAT_BIN - 1)
            by = np.clip((S["py"] // HEAT_BIN).astype(int), 0, RADAR // HEAT_BIN - 1)
            hb = pd.DataFrame({"x": bx, "y": by, "w": S["w"]}).groupby(["x", "y"])["w"].sum().reset_index()
            heat = [[int(r.x), int(r.y), round(float(r.w), 5)] for r in hb.itertuples()]

        # body: 1 kolo (v intervalu) = 1 tečka, pozice nejblíže středu intervalu
        dots = []
        if len(S):
            D = S.assign(d=(S["t"] - S["iv"].map(interval_mid)).abs())
            D = D.loc[D.groupby(rkeys + ["iv"])["d"].idxmin()]
            dots = [[r.steam_id, r.side, round(float(r.px), 1), round(float(r.py), 1), int(r.round_no),
                     r.match_id, round(float(r.t), 1), r.place, r.econ] for r in D.itertuples()]

        # hover buňky
        cells = {}
        if len(S):
            cx, cy = (S["px"] // CELL).astype(int), (S["py"] // CELL).astype(int)
            C = S.assign(cx=cx, cy=cy)
            for (x, y), g in C.groupby(["cx", "cy"]):
                places = g.groupby("place")["w"].sum().sort_values(ascending=False)
                cells[f"{x},{y}"] = {
                    "share": round(float(g["w"].sum()) / total_w * 100, 2) if total_w else 0,
                    "rounds": int(g[rkeys].drop_duplicates().shape[0]),
                    "samples": int(len(g)),
                    "place": places.index[0] if len(places) and places.index[0] else "",
                    "players": sorted(g["steam_id"].unique().tolist()),
                }

        # oblasti mapy podle herních calloutů (last_place_name)
        areas = []
        if len(S):
            for place, g in S.groupby("place"):
                rw = int(g[rkeys].drop_duplicates().shape[0])
                areas.append({"place": place or "(bez názvu)",
                              "share": round(float(g["w"].sum()) / total_w * 100, 2) if total_w else 0,
                              "rounds": rw,
                              "rounds_pct": round(rw / rounds_with * 100, 1) if rounds_with else 0})
            areas.sort(key=lambda a_: -a_["share"])

        return {
            "filters": {"players": players or [], "side": side, "interval": interval, "econ": econ,
                        "weight": weight, "level": level},
            "totals": {"samples": int(len(S)), "rounds": rounds_with,
                       "matches": int(S["match_id"].nunique()) if len(S) else 0,
                       "weight_sum": round(total_w, 4), **status},
            "heat": heat, "heat_bin": HEAT_BIN, "dots": dots, "cells": cells, "cell": CELL,
            "areas": areas,
        }

    def player_summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sid, g in self.rounds.groupby("steam_id"):
            out[sid] = {
                "rounds_T": int((g["side"] == "T").sum()), "rounds_CT": int((g["side"] == "CT").sum()),
                "matches": int(g["match_id"].nunique()),
                "econ": {k: int((g["econ"] == k).sum()) for k in ECON_LABELS},
            }
        return out
