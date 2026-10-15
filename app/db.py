"""SQLite cache. Jedno spojení na vlákno, zápisy parsování v jedné transakci."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import PARSER_VERSION

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS players(
  player_id TEXT PRIMARY KEY, nickname TEXT, steam_id TEXT, avatar TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS matches(
  match_id TEXT PRIMARY KEY, status TEXT, map TEXT, map_source TEXT, finished_at INTEGER,
  demo_urls TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS player_history(
  player_id TEXT, match_id TEXT, finished_at INTEGER, fetched_at REAL,
  PRIMARY KEY(player_id, match_id));
CREATE TABLE IF NOT EXISTS demos(
  match_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,          -- missing | downloading | downloaded | parsing | parsed | error | unavailable
  resource_url TEXT, file_path TEXT, file_size INTEGER, demo_map TEXT, tickrate REAL,
  time_source TEXT, n_rounds INTEGER, parser_version INTEGER, error TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS places(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS rounds(
  match_id TEXT, round_no INTEGER, steam_id TEXT, side TEXT, equip INTEGER, death_t REAL, round_len REAL,
  PRIMARY KEY(match_id, round_no, steam_id));
CREATE TABLE IF NOT EXISTS samples(
  match_id TEXT, round_no INTEGER, steam_id TEXT, t REAL, x REAL, y REAL, z REAL, place_id INTEGER,
  PRIMARY KEY(match_id, round_no, steam_id, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS demo_players(match_id TEXT, steam_id TEXT, name TEXT,
  PRIMARY KEY(match_id, steam_id));
CREATE INDEX IF NOT EXISTS idx_samples_sid ON samples(steam_id, match_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
        self.recover_interrupted()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    # ---------------------------------------------------------- konzistence
    def recover_interrupted(self) -> None:
        """Rozběhnuté stavy z přerušeného běhu nesmí vypadat jako hotové."""
        with self._write_lock, self.conn() as c:
            c.execute("UPDATE demos SET state='downloaded', error='Přerušené zpracování – zpracuje se znovu.' "
                      "WHERE state='parsing'")
            c.execute("UPDATE demos SET state='missing' WHERE state='downloading'")

    # ---------------------------------------------------------- hráči / zápasy
    def upsert_player(self, player_id: str, nickname: str, steam_id: str | None, avatar: str = "") -> None:
        with self._write_lock, self.conn() as c:
            c.execute("INSERT INTO players VALUES(?,?,?,?,?) ON CONFLICT(player_id) DO UPDATE SET "
                      "nickname=excluded.nickname, steam_id=COALESCE(excluded.steam_id, players.steam_id), "
                      "avatar=excluded.avatar, updated_at=excluded.updated_at",
                      (player_id, nickname, steam_id, avatar, time.time()))

    def get_player(self, player_id: str) -> sqlite3.Row | None:
        return self.conn().execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()

    def save_match(self, match_id: str, status: str, map_name: str | None, map_source: str,
                   finished_at: int | None, demo_urls: list[str]) -> None:
        with self._write_lock, self.conn() as c:
            c.execute("INSERT INTO matches VALUES(?,?,?,?,?,?,?) ON CONFLICT(match_id) DO UPDATE SET "
                      "status=excluded.status, map=excluded.map, map_source=excluded.map_source, "
                      "finished_at=COALESCE(excluded.finished_at, matches.finished_at), "
                      "demo_urls=excluded.demo_urls, fetched_at=excluded.fetched_at",
                      (match_id, status, map_name, map_source, finished_at, json.dumps(demo_urls), time.time()))

    def get_match(self, match_id: str) -> dict | None:
        r = self.conn().execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["demo_urls"] = json.loads(d["demo_urls"] or "[]")
        return d

    def save_history(self, player_id: str, rows: list[dict]) -> None:
        now = time.time()
        with self._write_lock, self.conn() as c:
            c.executemany("INSERT OR REPLACE INTO player_history VALUES(?,?,?,?)",
                          [(player_id, r["match_id"], r.get("finished_at"), now) for r in rows])

    # ---------------------------------------------------------- dema
    def demo(self, match_id: str) -> dict | None:
        r = self.conn().execute("SELECT * FROM demos WHERE match_id=?", (match_id,)).fetchone()
        return dict(r) if r else None

    def set_demo(self, match_id: str, state: str, **fields) -> None:
        cols = ["state", "updated_at"] + list(fields)
        vals = [state, time.time()] + list(fields.values())
        with self._write_lock, self.conn() as c:
            c.execute("INSERT OR IGNORE INTO demos(match_id, state) VALUES(?, 'missing')", (match_id,))
            c.execute(f"UPDATE demos SET {', '.join(f'{k}=?' for k in cols)} WHERE match_id=?",
                      vals + [match_id])

    def is_parsed(self, match_id: str) -> bool:
        d = self.demo(match_id)
        return bool(d and d["state"] == "parsed" and d["parser_version"] == PARSER_VERSION)

    def save_parse_result(self, match_id: str, result: dict) -> None:
        """Atomicky: buď se uloží všechna kola+pozice a stav 'parsed', nebo nic."""
        if result.get("parser_version") != PARSER_VERSION:
            raise ValueError("Výsledek parseru má jinou verzi.")
        with self._write_lock:
            c = self.conn()
            try:
                c.execute("BEGIN IMMEDIATE")
                for tbl in ("rounds", "samples", "demo_players"):
                    c.execute(f"DELETE FROM {tbl} WHERE match_id=?", (match_id,))
                place_ids: dict[str, int] = {}
                for name in {s[6] for s in result["samples"]}:
                    c.execute("INSERT OR IGNORE INTO places(name) VALUES(?)", (name,))
                    place_ids[name] = c.execute("SELECT id FROM places WHERE name=?", (name,)).fetchone()[0]
                c.executemany("INSERT INTO rounds VALUES(?,?,?,?,?,?,?)",
                              [(match_id, *r) for r in result["rounds"]])
                c.executemany("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)",
                              [(match_id, s[0], s[1], s[2], s[3], s[4], s[5], place_ids[s[6]])
                               for s in result["samples"]])
                c.executemany("INSERT INTO demo_players VALUES(?,?,?)",
                              [(match_id, sid, n) for sid, n in result["players"].items()])
                cur = c.execute("UPDATE demos SET state='parsed', demo_map=?, tickrate=?, time_source=?, n_rounds=?, "
                          "parser_version=?, error=NULL, updated_at=? WHERE match_id=?",
                          (result["map"], result["tickrate"], result["time_source"], result["n_rounds"],
                           result["parser_version"], time.time(), match_id))
                if cur.rowcount != 1:
                    raise RuntimeError("Záznam dema v databázi chybí.")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def demo_players(self, match_id: str) -> dict[str, str]:
        return {r[0]: r[1] for r in self.conn().execute(
            "SELECT steam_id, name FROM demo_players WHERE match_id=?", (match_id,))}

    def load_positions(self, match_ids: list[str], steam_ids: list[str]):
        """Vrátí (rounds_rows, samples_rows) pro dané zápasy a hráče (pro analýzu)."""
        if not match_ids:
            return [], []
        c = self.conn()
        mq = ",".join("?" * len(match_ids))
        rounds = c.execute(f"SELECT match_id, round_no, steam_id, side, equip, death_t, round_len FROM rounds "
                           f"WHERE match_id IN ({mq})", match_ids).fetchall()
        sq = ",".join("?" * len(steam_ids)) or "''"
        samples = c.execute(
            f"SELECT s.match_id, s.round_no, s.steam_id, s.t, s.x, s.y, s.z, p.name FROM samples s "
            f"JOIN places p ON p.id=s.place_id WHERE s.match_id IN ({mq}) AND s.steam_id IN ({sq})",
            match_ids + steam_ids).fetchall()
        return [tuple(r) for r in rounds], [tuple(r) for r in samples]
