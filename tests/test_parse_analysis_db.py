import collections

import numpy as np
import pandas as pd
import pytest

from app import analysis as A
from app.config import PARSER_VERSION
from app.db import Database
from tests.conftest import DEMO_CT, DEMO_T, fixture_mapinfo, mid, needs_demo


# ================================================================= parser (skutečné demo)
@needs_demo
def test_real_demo_header_and_time_model(real_parse):
    r = real_parse
    assert r["map"] == "de_mirage"
    assert r["tickrate"] == pytest.approx(64.0, abs=0.01)      # odvozeno z dema, ne předpokládáno
    assert r["time_source"] == "game_time"
    assert r["n_rounds"] == 9
    by_round = collections.defaultdict(list)
    for s in r["samples"]:
        by_round[s[0]].append(s[2])
    assert sorted(by_round) == list(range(1, 10))
    for rn, ts in by_round.items():
        assert min(ts) == pytest.approx(0.0, abs=0.02)           # čas začíná koncem freeze time
        steps = np.diff(sorted(set(ts)))
        assert np.allclose(steps[:-1], 1.0, atol=0.02)          # vzorek ~ každou sekundu herního času


@needs_demo
def test_real_demo_sides_and_players(real_parse):
    rounds = real_parse["rounds"]
    per_round = collections.Counter(r[0] for r in rounds)
    assert all(n == 10 for n in per_round.values())
    for sid in DEMO_T.values():
        assert {r[2] for r in rounds if r[1] == sid} == {"T"}
    for sid in DEMO_CT.values():
        assert {r[2] for r in rounds if r[1] == sid} == {"CT"}
    assert set(real_parse["players"]) == set(DEMO_T.values()) | set(DEMO_CT.values())


@needs_demo
def test_real_demo_only_alive_positions_and_round_bounds(real_parse):
    death = {(r[0], r[1]): r[4] for r in real_parse["rounds"]}
    length = {r[0]: r[5] for r in real_parse["rounds"]}
    for rn, sid, t, *_ in real_parse["samples"]:
        assert t <= length[rn] + 1e-6
        d = death[(rn, sid)]
        assert d is None or t < d                                # po smrti žádné pozice
    places = collections.Counter(s[6] for s in real_parse["samples"])
    assert places["TSpawn"] > 0 and places["CTSpawn"] > 0 and places["BombsiteA"] > 0
    assert places[""] / sum(places.values()) < 0.05


@needs_demo
def test_real_demo_spawn_callouts_match_start_positions(real_parse):
    """Pozice na začátku kola musí ležet v callout oblasti spawnu své strany."""
    side = {(r[0], r[1]): r[2] for r in real_parse["rounds"]}
    starts = [(side[(s[0], s[1])], s[6]) for s in real_parse["samples"] if s[2] == 0.0]
    assert starts
    for sd, place in starts:
        assert place == ("TSpawn" if sd == "T" else "CTSpawn")


@needs_demo
def test_parse_is_deterministic(real_parse):
    from app.parse import parse_demo
    from tests.conftest import REAL_DEMO
    again = parse_demo(str(REAL_DEMO))
    assert again["rounds"] == real_parse["rounds"] and len(again["samples"]) == len(real_parse["samples"])


def test_parse_rejects_garbage(tmp_path):
    from app.parse import parse_demo
    p = tmp_path / "bad.dem"
    p.write_bytes(b"PBDEMS2\x00" + b"\x00" * 5000)
    with pytest.raises(Exception):
        parse_demo(str(p))


# ================================================================= intervaly
@pytest.mark.parametrize("t,iv", [(0, 0), (4.99, 0), (5.0, 1), (9.9, 1), (15, 3), (19.99, 3), (20, 4),
                                  (29.9, 4), (30, 5), (45, 6), (59.99, 6), (60, 7), (150, 7)])
def test_interval_index(t, iv):
    assert int(A.interval_index(t)) == iv
    assert A.INTERVAL_KEYS[iv] in ("0-5", "5-10", "10-15", "15-20", "20-30", "30-45", "45-60", "60+")


# ================================================================= normalizace
def _samples(spec):
    """spec: list (match, round, sid, n_samples)"""
    rows = []
    for m, r, sid, n in spec:
        rows += [{"match_id": m, "round_no": r, "steam_id": sid, "t": float(i)} for i in range(n)]
    return pd.DataFrame(rows)


def test_weights_rounds_one_round_is_one():
    df = _samples([("m1", 1, "a", 20)])          # 20 s na jednom místě
    assert A.compute_weights(df, "rounds").sum() == pytest.approx(1.0)
    df = _samples([("m1", 1, "a", 20), ("m1", 2, "a", 3), ("m2", 1, "a", 7)])
    assert A.compute_weights(df, "rounds").sum() == pytest.approx(3.0)


def test_weights_matches_and_time():
    df = _samples([("m1", r, "a", 5) for r in range(1, 25)] + [("m2", 1, "a", 40)])
    w = A.compute_weights(df, "matches")
    assert w.sum() == pytest.approx(2.0)
    assert w[df["match_id"] == "m1"].sum() == pytest.approx(1.0)   # 24 kol nepřebije 1 kolo
    assert A.compute_weights(df, "time").sum() == pytest.approx(len(df))
    with pytest.raises(ValueError):
        A.compute_weights(df, "nic")
    assert len(A.compute_weights(df.iloc[0:0], "rounds")) == 0


# ================================================================= ekonomika
def _rounds(rows):
    return pd.DataFrame(rows, columns=["match_id", "round_no", "steam_id", "side", "equip"])


def test_economy_classification():
    rows = []
    T, CT = [f"t{i}" for i in range(5)], [f"c{i}" for i in range(5)]
    plan = {1: (850, 900), 2: (1500, 4500), 3: (2600, 4200), 4: (4100, 1200), 13: (900, 950), 25: (5500, 5600)}
    for rn, (te, ce) in plan.items():
        swap = rn >= 13 and rn < 25 or rn >= 25 and False
        tside, cside = ("CT", "T") if rn >= 13 and rn < 25 else ("T", "CT")
        if rn == 25:
            tside, cside = ("T", "CT")
        for p in T:
            rows.append(("m", rn, p, tside, te))
        for p in CT:
            rows.append(("m", rn, p, cside, ce))
    # kolo 12 kvůli detekci výměny stran mezi 12 -> 13 a 24 -> 25
    for p in T:
        rows.append(("m", 12, p, "T", 4000)); rows.append(("m", 24, p, "CT", 4000))
    for p in CT:
        rows.append(("m", 12, p, "CT", 4000)); rows.append(("m", 24, p, "T", 4000))
    e = A.classify_economy(_rounds(rows))
    get = lambda rn, side: e[(e.round_no == rn) & (e.side == side)].econ.iloc[0]  # noqa: E731
    assert get(1, "T") == "pistol" and get(1, "CT") == "pistol"
    assert get(2, "T") == "eco" and get(2, "CT") == "full"
    assert get(3, "T") == "force" and get(3, "CT") == "full"
    assert get(4, "T") == "full" and get(4, "CT") == "eco"
    assert get(13, "CT") == "pistol" and get(13, "T") == "pistol"        # druhý poločas
    assert get(25, "T") == "full"                                         # prodloužení: výměna, ale plné peníze


def test_economy_unknown_without_data():
    rows = [("m", 2, f"p{i}", "T", 4000 if i < 2 else None) for i in range(5)]
    e = A.classify_economy(_rounds(rows))
    assert e.econ.iloc[0] == "unknown"


# ================================================================= dataset / view (skutečné demo)
@needs_demo
def test_view_on_real_demo(real_parse, tmp_path):
    m = mid(1)
    rounds = [(m, *r) for r in real_parse["rounds"]]
    samples = [(m, *s) for s in real_parse["samples"]]
    targets = list(DEMO_T.values())
    ds = A.Dataset(rounds, samples, targets, fixture_mapinfo(tmp_path))
    v = ds.view(side="T", weight="rounds")
    assert v["totals"]["rounds"] == 45                               # 5 hráčů × 9 kol
    assert v["totals"]["weight_sum"] == pytest.approx(45.0, rel=1e-6)
    assert v["totals"]["matches"] == 1
    assert sum(a["share"] for a in v["areas"]) == pytest.approx(100, abs=0.5)
    assert sum(c["share"] for c in v["cells"].values()) == pytest.approx(100, abs=0.5)
    # CT filtr nesmí vrátit T hráče
    assert ds.view(side="CT")["totals"]["samples"] == 0
    # interval 0-5: každé kolo, kde hráč žil, má 1 tečku v intervalu
    v5 = ds.view(side="T", interval="0-5")
    assert len(v5["dots"]) == v5["totals"]["rounds"] == 45
    assert all(d[7] in ("TSpawn", "BackAlley", "TRamp", "OutsideLong", "TopofMid", "Middle", "Apartments",
                        "OutsideTapartments", "SideAlley", "Palace", "PalaceInterior") or d[7] for d in v5["dots"])
    assert v5["totals"]["alive_at_start"] == 45 and v5["totals"]["eliminated_before"] == 0
    # pozdější interval: součet živý + eliminovaný + konec kola = odehraná kola
    v60 = ds.view(side="T", interval="60+")
    t = v60["totals"]
    assert t["alive_at_start"] + t["eliminated_before"] + t["round_over_before"] == t["rounds_played"] == 45
    assert t["eliminated_before"] > 0
    # jeden hráč
    one = ds.view(players=[DEMO_T["NIGHTSOUL"]], side="T")
    assert one["totals"]["rounds"] == 9 and {d[0] for d in one["dots"]} == {DEMO_T["NIGHTSOUL"]}
    # časová váha = sekundy
    assert ds.view(side="T", weight="time")["totals"]["weight_sum"] == v["totals"]["samples"]
    with pytest.raises(ValueError):
        ds.view(interval="7-8")
    summ = ds.player_summary()
    assert summ[DEMO_T["NIGHTSOUL"]]["rounds_T"] == 9 and summ[DEMO_T["NIGHTSOUL"]]["rounds_CT"] == 0


# ================================================================= databáze
def _result(rounds=None, samples=None, version=PARSER_VERSION):
    return {"parser_version": version, "map": "de_mirage", "tickrate": 64.0, "time_source": "game_time",
            "n_rounds": 1, "players": {"7656": "a"},
            "rounds": rounds or [(1, "7656", "T", 800, None, 90.0)],
            "samples": samples or [(1, "7656", 0.0, 1.0, 2.0, 3.0, "TSpawn"), (1, "7656", 1.0, 1.5, 2.5, 3.0, "TSpawn")]}


def test_db_save_and_load(tmp_path):
    db = Database(tmp_path / "c.db")
    db.set_demo(mid(1), "parsing")
    db.save_parse_result(mid(1), _result())
    assert db.is_parsed(mid(1))
    r, s = db.load_positions([mid(1)], ["7656"])
    assert len(r) == 1 and len(s) == 2 and s[0][7] == "TSpawn"
    db.save_parse_result(mid(1), _result())          # opakované uložení nevytvoří duplicity
    assert len(db.load_positions([mid(1)], ["7656"])[1]) == 2


def test_db_interrupted_save_is_not_parsed(tmp_path, monkeypatch):
    db = Database(tmp_path / "c.db")
    db.set_demo(mid(1), "parsing")
    bad = _result(samples=[(1, "7656", 0.0, 1.0, 2.0, 3.0, "A"), ("x",)])   # vadný řádek uprostřed zápisu
    with pytest.raises(Exception):
        db.save_parse_result(mid(1), bad)
    assert not db.is_parsed(mid(1))
    assert db.load_positions([mid(1)], ["7656"]) == ([], [])


def test_db_recovers_interrupted_states(tmp_path):
    p = tmp_path / "c.db"
    db = Database(p)
    db.set_demo(mid(1), "parsing")
    db.set_demo(mid(2), "downloading")
    db2 = Database(p)                                 # "restart aplikace"
    assert db2.demo(mid(1))["state"] == "downloaded"
    assert db2.demo(mid(2))["state"] == "missing"
    assert not db2.is_parsed(mid(1))


def test_db_parser_version_invalidation(tmp_path):
    db = Database(tmp_path / "c.db")
    db.set_demo(mid(1), "parsing")
    db.save_parse_result(mid(1), _result())
    db.conn().execute("UPDATE demos SET parser_version=? WHERE match_id=?", (PARSER_VERSION - 1, mid(1)))
    assert not db.is_parsed(mid(1))
    with pytest.raises(ValueError):
        db.save_parse_result(mid(1), _result(version=PARSER_VERSION - 1))
