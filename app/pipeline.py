"""Orchestrace jedné analýzy matchroomu (běží ve vlákně, stav čte webové UI)."""
from __future__ import annotations

import multiprocessing
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from . import demofiles
from .analysis import Dataset
from .config import HISTORY_SIZE, VETO_POLL_S, VETO_TIMEOUT_S, Settings
from .db import Database
from .faceit import (FaceitClient, FaceitError, IdentificationError, identify_opponents, map_from_stats,
                     match_status, normalize_demo_urls, parse_match_input, picked_map, steam_id_of, team_name)
from .mapdata import MapData, MapDataError
from .parse import parse_demo

STATES = {
    "loading": "Načítání matchroomu", "waiting_veto": "Čekání na veto", "players": "Načítání soupeřů",
    "history": "Získávání historie", "downloading": "Stahování dem", "parsing": "Parsování",
    "rendering": "Vykreslování", "done": "Hotovo", "partial": "Částečně hotovo", "error": "Chyba",
    "cancelled": "Zrušeno",
}
STEPS = [("matchroom", "Načtení matchroomu"), ("identify", "Identifikace hráčů"),
         ("matches", "Načtení zápasů"), ("demo_lookup", "Získání dem"), ("download", "Stažení dem"),
         ("parse", "Parsování"), ("aggregate", "Agregace"), ("render", "Vykreslení")]
FINAL_STATES = {"done", "partial", "error", "cancelled"}
TERMINAL_CANCELLED = {"CANCELLED", "ABORTED"}


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, url: str):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.created = time.time()
        self.state = "loading"
        self.message = ""
        self.error = ""
        self.match_id = ""
        self.map = ""
        self.match_status = ""
        self.my_team = ""
        self.opp_team = ""
        self.opponents: list[dict] = []
        self.matches: dict[str, dict] = {}   # unikátní zápasy na mapě
        self.history: dict[str, dict] = {}   # player_id -> {"selected": [...], "scanned": n}
        self.steps = {k: {"label": l, "status": "pending", "seconds": None} for k, l in STEPS}
        self._t0: dict[str, float] = {}
        self.data_version = 0
        self.dataset: Dataset | None = None
        self.mapinfo = None
        self.warnings: list[str] = []
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()

    # --- měření kroků
    def start(self, key: str) -> None:
        with self.lock:
            self._t0[key] = time.perf_counter()
            self.steps[key]["status"] = "running"

    def finish(self, key: str, status: str = "done", note: str = "") -> None:
        with self.lock:
            t0 = self._t0.get(key)
            self.steps[key]["seconds"] = round(time.perf_counter() - t0, 3) if t0 else None
            self.steps[key]["status"] = status
            if note:
                self.steps[key]["note"] = note

    def set_state(self, state: str, message: str = "") -> None:
        with self.lock:
            self.state = state
            self.message = message

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    # --- serializace pro UI
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "state": self.state, "state_label": STATES.get(self.state, self.state),
                "final": self.state in FINAL_STATES, "message": self.message, "error": self.error,
                "match_id": self.match_id, "match_status": self.match_status, "map": self.map,
                "my_team": self.my_team, "opp_team": self.opp_team,
                "opponents": self._opponent_stats(), "steps": self.steps,
                "matches": [{"match_id": m, **{k: v for k, v in d.items() if k != "players_sids"}}
                            for m, d in sorted(self.matches.items(), key=lambda kv: -(kv[1].get("finished_at") or 0))],
                "data_version": self.data_version, "warnings": self.warnings,
                "has_lower": bool(self.mapinfo and self.mapinfo.lower),
                "map_ready": bool(self.mapinfo and self.mapinfo.radar),
            }

    def _opponent_stats(self) -> list[dict]:
        out = []
        summary = self.dataset.player_summary() if self.dataset is not None else {}
        for o in self.opponents:
            h = self.history.get(o["player_id"])
            sel = h["selected"] if h else None
            on_map = [m for m in (sel or []) if m["match_id"] in self.matches]
            unknown_map = [m for m in (sel or []) if m.get("map") is None]
            st = {"parsed": 0, "unavailable": 0, "error": 0, "pending": 0, "not_in_demo": 0}
            reasons: dict[str, int] = {}
            for m in on_map:
                d = self.matches[m["match_id"]]
                s = d.get("state", "pending")
                if s == "parsed":
                    if o.get("steam_id") and o["steam_id"] in d.get("players_sids", ()):
                        st["parsed"] += 1
                    else:
                        st["not_in_demo"] += 1
                elif s in ("unavailable", "error"):
                    st[s] += 1
                    reasons[d.get("reason", "")] = reasons.get(d.get("reason", ""), 0) + 1
                else:
                    st["pending"] += 1
            out.append({**o,
                        "history_found": len(sel) if sel is not None else None,
                        "history_target": HISTORY_SIZE,
                        "on_map": len(on_map) if sel is not None else None,
                        "map_unknown": len(unknown_map),
                        "demos": st, "missing_reasons": reasons,
                        "summary": summary.get(o.get("steam_id") or "", None)})
        return out


class Analyzer:
    """Spouští Joby. Závislosti jsou injektovatelné kvůli testům."""

    def __init__(self, settings: Settings, db: Database | None = None,
                 client_factory: Callable[[], FaceitClient] | None = None,
                 mapdata: MapData | None = None, parse_fn: Callable = parse_demo,
                 use_processes: bool = True, sleep: Callable[[float], None] | None = None):
        self.s = settings
        settings.ensure_dirs()
        self.db = db or Database(settings.db_path)
        self.client_factory = client_factory or (lambda: FaceitClient(settings.api_key, settings.download_token))
        self.mapdata = mapdata or MapData(settings.map_dir)
        self.parse_fn = parse_fn
        self.use_processes = use_processes
        self.sleep = sleep
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, url: str) -> Job:
        job = Job(url)
        with self._lock:
            for j in self.jobs.values():
                j.cancel_event.set()
            self.jobs = {k: v for k, v in self.jobs.items() if time.time() - v.created < 3600}
            self.jobs[job.id] = job
        threading.Thread(target=self.run, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job

    # ------------------------------------------------------------------ běh
    def run(self, job: Job) -> None:
        try:
            self._run(job)
        except Cancelled:
            job.set_state("cancelled", "Analýza byla nahrazena novou.")
        except (FaceitError, demofiles.DemoFileError, MapDataError) as e:
            job.error = str(e)
            for st in job.steps.values():
                if st["status"] == "running":
                    st["status"] = "error"
            job.set_state("error", str(e))
        except Exception as e:  # neočekávané – nesmí vypadat jako úspěch
            traceback.print_exc()
            job.error = f"Neočekávaná chyba: {e.__class__.__name__}: {e}"
            for st in job.steps.values():
                if st["status"] == "running":
                    st["status"] = "error"
            job.set_state("error", job.error)

    def _sleep(self, seconds: float, job: Job) -> None:
        if self.sleep:
            self.sleep(seconds)
            job.check()
        elif job.cancel_event.wait(seconds):
            raise Cancelled()

    def _my_player_id(self, client: FaceitClient) -> str:
        if self.s.player_id:
            return self.s.player_id
        if not self.s.nickname:
            raise IdentificationError("V .env chybí FACEIT_NICKNAME (tvoje přezdívka na FACEIT).")
        key = f"pid:{self.s.nickname.lower()}"
        row = self.db.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row:
            return row[0]
        pid = client.find_player_id(self.s.nickname)
        self.db.conn().execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, pid))
        return pid

    def _run(self, job: Job) -> None:
        client = self.client_factory()

        # 1) matchroom
        job.start("matchroom")
        job.match_id = parse_match_input(job.url)
        match = client.get_match(job.match_id)
        job.match_status = match_status(match)
        job.finish("matchroom")
        job.check()

        # 2) identifikace
        job.set_state("players")
        job.start("identify")
        me = self._my_player_id(client)
        my_key, opp_key, opp = identify_opponents(match, me)
        job.my_team, job.opp_team = team_name(match, my_key), team_name(match, opp_key)
        opponents = []
        for p in opp:
            sid = steam_id_of(p)
            if not sid:
                try:
                    prof = client.get_player(p["player_id"])
                    sid = steam_id_of({"game_player_id": ((prof.get("games") or {}).get("cs2") or {}).get("game_player_id")})
                except FaceitError:
                    sid = None
            self.db.upsert_player(p["player_id"], str(p.get("nickname") or ""), sid, str(p.get("avatar") or ""))
            opponents.append({"player_id": p["player_id"], "nickname": str(p.get("nickname") or "?"),
                              "steam_id": sid, "avatar": str(p.get("avatar") or ""), "verified_steam": bool(sid)})
        job.opponents = opponents
        if len(opponents) != 5:
            job.warnings.append(f"Soupeřův tým má {len(opponents)} hráčů (očekáváno 5).")
        for o in opponents:
            if not o["steam_id"]:
                job.warnings.append(f"{o['nickname']}: FACEIT neuvádí ověřené Steam ID – nelze spárovat s demy.")
        job.finish("identify")

        # Historie a detaily zápasů nezávisí na mapě -> stahují se už během veta
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="faceit")
        hist_fut: Future = pool.submit(self._load_histories, client, job)

        # 3) veto
        waited = time.time()
        chosen = picked_map(match)
        while chosen is None:
            if job.match_status in TERMINAL_CANCELLED:
                pool.shutdown(wait=False, cancel_futures=True)
                raise FaceitError("Zápas byl zrušen – veto se nedokončí.")
            if time.time() - waited > VETO_TIMEOUT_S:
                pool.shutdown(wait=False, cancel_futures=True)
                raise FaceitError("Veto neskončilo do 20 minut – analýza ukončena.")
            job.set_state("waiting_veto", "Čekám na dokončení veta… (kontrola každých 5 s)")
            self._sleep(VETO_POLL_S, job)
            match = client.get_match(job.match_id)
            job.match_status = match_status(match)
            chosen = picked_map(match)
        job.map = chosen
        map_fut: Future = pool.submit(self.mapdata.ensure, chosen)

        # 4) zápasy
        job.set_state("history", f"Hledám zápasy na {chosen}…")
        job.start("matches")
        details = hist_fut.result()
        job.check()
        unique: dict[str, dict] = {}
        for o in opponents:
            h = job.history[o["player_id"]]
            for m in h["selected"]:
                info = details.get(m["match_id"]) or {}
                m["map"] = info.get("map")
                if m["map"] == chosen:
                    u = unique.setdefault(m["match_id"], {"finished_at": m.get("finished_at"),
                                                          "players": [], "demo_urls": info.get("demo_urls", []),
                                                          "state": "pending", "reason": ""})
                    u["players"].append(o["nickname"])
        job.matches = unique
        job.finish("matches", note=f"{len(unique)} unikátních zápasů na mapě")

        try:
            job.mapinfo = map_fut.result()
        except MapDataError as e:
            job.warnings.append(str(e))
        pool.shutdown(wait=False)

        # 5) dema
        self._process_demos(job)

        # 6) agregace
        job.start("aggregate")
        self._rebuild(job)
        job.finish("aggregate")

        parsed = sum(1 for d in job.matches.values() if d["state"] == "parsed")
        total = len(job.matches)
        incomplete = (parsed < total or job.mapinfo is None or job.mapinfo.radar is None
                      or any(not o["steam_id"] for o in opponents)
                      or any(len(job.history[o["player_id"]]["selected"]) < HISTORY_SIZE for o in opponents))
        if job.mapinfo is None:
            job.set_state("partial", "Pozice jsou zpracované, ale chybí ověřené mapové podklady – nelze je vykreslit.")
        elif total == 0:
            job.set_state("partial", f"Soupeři v posledních {HISTORY_SIZE} zápasech nehráli {chosen}.")
        elif parsed == 0:
            job.set_state("partial", "Žádné demo se nepodařilo získat – analýza pozic není k dispozici.")
        elif incomplete:
            job.set_state("partial", f"Analýza vychází z {parsed} z {total} zápasů na mapě.")
        else:
            job.set_state("done", f"Zpracováno všech {total} zápasů na mapě.")

    # ------------------------------------------------------------------ historie
    def _load_histories(self, client: FaceitClient, job: Job) -> dict[str, dict]:
        def one(o):
            sel, scanned = client.history(o["player_id"], HISTORY_SIZE, exclude_match_id=job.match_id)
            self.db.save_history(o["player_id"], sel)
            return o["player_id"], {"selected": sel, "scanned": scanned}

        with ThreadPoolExecutor(5) as ex:
            results = list(ex.map(one, job.opponents))
        for pid, h in results:
            job.history[pid] = h
        ids = sorted({m["match_id"] for _, h in results for m in h["selected"]})

        def detail(mid):
            cached = self.db.get_match(mid)
            if cached and cached["status"] == "FINISHED" and cached["map"]:
                return mid, {"map": cached["map"], "demo_urls": cached["demo_urls"]}
            try:
                md = client.get_match(mid)
            except FaceitError:
                return mid, {"map": None, "demo_urls": []}
            mp, src = picked_map(md), "voting"
            if mp is None:
                try:
                    mp, src = map_from_stats(client.get_match_stats(mid)), "stats"
                except FaceitError:
                    mp = None
            urls = normalize_demo_urls(md.get("demo_url"))
            self.db.save_match(mid, match_status(md), mp, src, md.get("finished_at"), urls)
            return mid, {"map": mp, "demo_urls": urls}

        with ThreadPoolExecutor(10) as ex:
            return dict(ex.map(detail, ids))

    # ------------------------------------------------------------------ dema
    def _process_demos(self, job: Job) -> None:
        s, db = self.s, self.db
        job.start("demo_lookup")
        imports = demofiles.find_imports(s.import_dir, job.matches)
        to_parse, to_download = [], []
        for mid, d in job.matches.items():
            if db.is_parsed(mid):
                d["state"], d["source"] = "parsed", "cache"
                d["players_sids"] = set(db.demo_players(mid))
                continue
            path = demofiles.demo_path(s.demo_dir, mid)
            if path.exists():
                to_parse.append(mid)
                d["source"] = "disk"
            elif mid in imports:
                try:
                    demofiles.decompress_to_dem(imports[mid], path)
                    to_parse.append(mid)
                    d["source"] = "import"
                except demofiles.DemoFileError as e:
                    d["state"], d["reason"] = "error", str(e)
                    db.set_demo(mid, "error", error=str(e))
            elif not d["demo_urls"]:
                d["state"], d["reason"] = "unavailable", "FACEIT u zápasu neuvádí demo."
                db.set_demo(mid, "unavailable", error=d["reason"])
            elif not s.download_token:
                d["state"], d["reason"] = "unavailable", "Chybí schválený přístup k FACEIT Downloads API."
                db.set_demo(mid, "unavailable", resource_url=d["demo_urls"][0], error=d["reason"])
            else:
                to_download.append(mid)
                d["source"] = "download"
        job.finish("demo_lookup", note=f"cache {sum(1 for d in job.matches.values() if d.get('source') == 'cache')}, "
                                       f"stáhnout {len(to_download)}, zpracovat {len(to_parse)}")
        job.check()

        if not to_download:
            job.steps["download"].update(status="skipped", seconds=0.0)
        if not to_parse and not to_download:
            job.steps["parse"].update(status="skipped", seconds=0.0)
            return

        workers = s.parse_workers or max(1, min(4, (os.cpu_count() or 2) // 2))
        executor = (ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
                    if self.use_processes else ThreadPoolExecutor(workers))
        dl_pool = ThreadPoolExecutor(max_workers=s.download_workers)
        client = self.client_factory() if to_download else None
        parse_futs: dict[Future, str] = {}
        dl_futs: dict[Future, str] = {}
        last_rebuild = 0.0

        def start_parse(mid: str) -> None:
            if not parse_futs and job.steps["parse"]["status"] == "pending":
                job.start("parse")
            db.set_demo(mid, "parsing", file_path=str(demofiles.demo_path(s.demo_dir, mid)))
            job.matches[mid]["state"] = "parsing"
            f = executor.submit(self.parse_fn, str(demofiles.demo_path(s.demo_dir, mid)))
            parse_futs[f] = mid

        def do_download(mid: str) -> str:
            job.check()
            path = demofiles.demo_path(s.demo_dir, mid)
            res = job.matches[mid]["demo_urls"][0]
            budget = int(s.max_demo_disk_gb * 1024**3)
            demofiles.enforce_budget(s.demo_dir, budget, lambda m: db.is_parsed(m))
            demofiles.check_disk(s.demo_dir, budget, 300 * 1024**2)
            db.set_demo(mid, "downloading", resource_url=res)
            signed = client.signed_demo_url(res)
            raw = path.with_name(path.name + ".download")
            try:
                demofiles.download(signed, raw)
                demofiles.decompress_to_dem(raw, path)
            finally:
                raw.unlink(missing_ok=True)
            db.set_demo(mid, "downloaded", file_path=str(path), file_size=path.stat().st_size)
            return mid

        try:
            if to_download:
                job.set_state("downloading", f"Stahuji {len(to_download)} dem…")
                job.start("download")
                for mid in to_download:
                    job.matches[mid]["state"] = "downloading"
                    dl_futs[dl_pool.submit(do_download, mid)] = mid
            for mid in to_parse:
                start_parse(mid)
            if to_parse:
                job.set_state("parsing", "Zpracovávám dema…")

            while dl_futs or parse_futs:
                done, _ = wait(list(dl_futs) + list(parse_futs), timeout=1.0, return_when=FIRST_COMPLETED)
                if job.cancel_event.is_set():
                    raise Cancelled()
                for f in done:
                    if f in dl_futs:
                        mid = dl_futs.pop(f)
                        try:
                            f.result()
                            start_parse(mid)
                            job.set_state("parsing", "Zpracovávám dema…")
                        except Cancelled:
                            raise
                        except Exception as e:
                            msg = str(e) if isinstance(e, (FaceitError, demofiles.DemoFileError)) else \
                                f"Stažení selhalo ({e.__class__.__name__})."
                            job.matches[mid].update(state="error", reason=msg)
                            db.set_demo(mid, "error", error=msg)
                        if not dl_futs and to_download:
                            job.finish("download")
                    else:
                        mid = parse_futs.pop(f)
                        self._handle_parse_result(job, mid, f)
                        if time.time() - last_rebuild > 2.0:
                            self._rebuild(job)
                            last_rebuild = time.time()
            if job.steps["parse"]["status"] == "running":
                job.finish("parse")
            elif job.steps["parse"]["status"] == "pending":
                job.steps["parse"].update(status="skipped", seconds=0.0)
        finally:
            dl_pool.shutdown(wait=False, cancel_futures=True)
            executor.shutdown(wait=False, cancel_futures=True)

    def _handle_parse_result(self, job: Job, mid: str, fut: Future) -> None:
        db, d = self.db, job.matches[mid]
        path = demofiles.demo_path(self.s.demo_dir, mid)
        try:
            result = fut.result()
        except Exception as e:
            msg = f"Demo se nepodařilo zpracovat ({e.__class__.__name__}: {str(e)[:160]})."
            d.update(state="error", reason=msg)
            db.set_demo(mid, "error", error=msg)
            return
        if result.get("map") != job.map:
            msg = f"Demo je z mapy {result.get('map')}, ne {job.map} – vynecháno."
            d.update(state="error", reason=msg)
            db.set_demo(mid, "error", error=msg)
            return
        try:
            db.save_parse_result(mid, result)
        except Exception as e:
            d.update(state="error", reason=f"Uložení do cache selhalo ({e.__class__.__name__}).")
            db.set_demo(mid, "error", error=d["reason"])
            return
        d["state"] = "parsed"
        d["players_sids"] = set(result["players"])
        if not self.s.keep_demos:
            path.unlink(missing_ok=True)

    def _rebuild(self, job: Job) -> None:
        if job.mapinfo is None:
            return
        sids = [o["steam_id"] for o in job.opponents if o["steam_id"]]
        parsed = [m for m, d in job.matches.items() if d["state"] == "parsed"]
        rounds, samples = self.db.load_positions(parsed, sids)
        ds = Dataset(rounds, samples, sids, job.mapinfo)
        with job.lock:
            job.dataset = ds
            job.data_version += 1
