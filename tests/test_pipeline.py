"""Integrační testy celé analýzy s mockovaným FACEIT API a skutečným demem."""
import copy
import shutil
import time

import pytest

from app import faceit as F
from app import pipeline as P
from tests.conftest import DEMO_T, FakeMapData, make_settings, mid, needs_demo

CUR = mid(1)
ME = "me-id"
OPP = [(f"opp{i}", nick, sid) for i, (nick, sid) in enumerate(DEMO_T.items())]
SHARED = [mid(900), mid(901)]


def current_match(pick: str | None = "de_mirage", status="READY"):
    return {
        "match_id": CUR, "status": status,
        "voting": {"map": {"pick": [pick]}} if pick else {"map": {"entities": []}},
        "teams": {
            "faction1": {"name": "Moji", "roster": [{"player_id": ME, "nickname": "me", "game_player_id": "76561190000000001"}]
                         + [{"player_id": f"mate{i}", "nickname": f"mate{i}"} for i in range(4)]},
            "faction2": {"name": "Soupeři", "roster": [{"player_id": pid, "nickname": n, "game_player_id": sid,
                                                         "avatar": ""} for pid, n, sid in OPP]},
        },
    }


class FakeClient:
    def __init__(self, details, histories, veto_seq=None, token="tok", me_in_match=True):
        self.details, self.histories = details, histories
        self.veto_seq = list(veto_seq or [current_match()])
        self.download_token = token
        self.me_in_match = me_in_match
        self.calls = {"get_match": 0, "history": 0, "signed": 0}

    def get_match(self, match_id):
        self.calls["get_match"] += 1
        if match_id == CUR:
            m = self.veto_seq.pop(0) if len(self.veto_seq) > 1 else self.veto_seq[0]
            m = copy.deepcopy(m)
            if not self.me_in_match:
                m["teams"]["faction1"]["roster"][0]["player_id"] = "someone"
            return m
        if match_id not in self.details:
            raise F.FaceitError("404", 404)
        return copy.deepcopy(self.details[match_id])

    def get_match_stats(self, match_id):
        raise F.FaceitError("404", 404)

    def find_player_id(self, nickname):
        return ME

    def get_player(self, pid):
        return {}

    def history(self, pid, need, exclude_match_id=""):
        self.calls["history"] += 1
        items = self.histories[pid]
        return F.select_recent_finished(items, exclude_match_id, need), len(items)

    def signed_demo_url(self, res):
        self.calls["signed"] += 1
        if not self.download_token:
            raise F.FaceitError("no token")
        return "https://signed.example/" + res.rsplit("/", 1)[-1]


def build_world(on_map=SHARED, history_len=35, demo_for=None):
    """Každý soupeř: history_len dokončených zápasů; zápasy SHARED (spol. pro všech 5) jsou na de_mirage."""
    details, histories = {}, {}
    demo_for = set(on_map if demo_for is None else demo_for)
    for i, (pid, _, _) in enumerate(OPP):
        items = []
        for k in range(history_len):
            m = on_map[k] if k < len(on_map) else mid(1000 + i * 100 + k)
            items.append({"match_id": m, "status": "finished", "finished_at": 2_000_000 - k})
            if m not in details:
                mp = "de_mirage" if m in on_map else "de_nuke"
                details[m] = {"status": "FINISHED", "voting": {"map": {"pick": [mp]}}, "finished_at": 2_000_000 - k,
                              "demo_url": [f"https://demos.faceit.com/cs2/{m}-1-1.dem.gz"] if m in demo_for else []}
        items.append({"match_id": mid(5000 + i), "status": "cancelled", "finished_at": 3_000_000})
        histories[pid] = items
    return details, histories


@pytest.fixture
def downloads(monkeypatch, demo_gz):
    log = []

    def fake_download(url, dest, **kw):
        log.append(url)
        shutil.copyfile(demo_gz, dest)
        return dest.stat().st_size

    monkeypatch.setattr(P.demofiles, "download", fake_download)
    monkeypatch.setattr(P.demofiles, "check_disk", lambda *a, **k: None)
    return log


def run(analyzer, timeout=180):
    job = P.Job(f"https://www.faceit.com/cs/cs2/room/{CUR}")
    states = []
    orig = job.set_state

    def track(state, message=""):
        states.append(state)
        orig(state, message)

    job.set_state = track
    analyzer.jobs[job.id] = job
    t0 = time.time()
    analyzer.run(job)
    assert time.time() - t0 < timeout
    return job, states


def make(tmp_path, client, token="tok", processes=False, mapfail=False, **kw):
    s = make_settings(tmp_path, download_token=token, **kw)
    return P.Analyzer(s, client_factory=lambda: client, mapdata=FakeMapData(tmp_path, fail=mapfail),
                      use_processes=processes, sleep=lambda x: None)


# ---------------------------------------------------------------------------------------------
@needs_demo
def test_full_flow_waits_for_veto_dedupes_and_finishes(tmp_path, downloads):
    details, hist = build_world()
    client = FakeClient(details, hist, veto_seq=[current_match(None, "VOTING"), current_match(None, "VOTING"),
                                                 current_match("de_mirage")])
    an = make(tmp_path, client)
    job, states = run(an)
    snap = job.snapshot()
    assert "waiting_veto" in states
    assert job.state == "done", job.message
    assert job.map == "de_mirage"
    assert len(job.matches) == 2 and len(downloads) == 2            # 5 hráčů sdílí 2 zápasy -> 2 stažení
    assert client.calls["history"] == 5
    for o in snap["opponents"]:
        assert o["history_found"] == 30 and o["on_map"] == 2 and o["demos"]["parsed"] == 2
        assert o["summary"]["rounds_T"] == 18                        # 2 dema × 9 kol
    for key, st in snap["steps"].items():
        if key != "render":
            assert st["status"] == "done" and st["seconds"] is not None, key
    v = job.dataset.view(side="T")
    assert v["totals"]["matches"] == 2 and v["totals"]["rounds"] == 90
    assert not list((an.s.demo_dir).glob("*.dem"))                  # stažená dema po zpracování smazána


@needs_demo
def test_cache_reuse_second_run_downloads_nothing(tmp_path, downloads):
    details, hist = build_world()
    client = FakeClient(details, hist)
    an = make(tmp_path, client)
    run(an)
    n = len(downloads)
    job, _ = run(an)
    assert len(downloads) == n
    assert job.state == "done"
    assert {d["source"] for d in job.matches.values()} == {"cache"}
    assert job.steps["download"]["status"] == "skipped"


@needs_demo
def test_partial_without_token_reports_exact_reasons(tmp_path, demo_gz):
    on_map = [mid(900), mid(901), mid(902)]
    details, hist = build_world(on_map=on_map, demo_for=[mid(900), mid(901)])
    an = make(tmp_path, FakeClient(details, hist, token=""), token="")
    shutil.copyfile(demo_gz, an.s.import_dir / f"{mid(900)}-1-1.dem.gz")   # ručně dodané demo
    job, states = run(an)
    snap = job.snapshot()
    assert job.state == "partial" and "done" not in states
    assert job.matches[mid(900)]["state"] == "parsed" and job.matches[mid(900)]["source"] == "import"
    assert job.matches[mid(901)]["reason"].startswith("Chybí schválený přístup")
    assert job.matches[mid(902)]["reason"] == "FACEIT u zápasu neuvádí demo."
    o = snap["opponents"][0]
    assert (o["on_map"], o["demos"]["parsed"], o["demos"]["unavailable"]) == (3, 1, 2)
    assert "1 z 3" in job.message


@needs_demo
def test_short_history_is_partial(tmp_path, downloads):
    details, hist = build_world(history_len=12)
    job, _ = run(make(tmp_path, FakeClient(details, hist)))
    assert job.state == "partial"
    assert job.snapshot()["opponents"][0]["history_found"] == 12


@needs_demo
def test_parse_failure_and_wrong_map_are_excluded(tmp_path, downloads):
    from app.parse import parse_demo
    details, hist = build_world(on_map=[mid(900), mid(901), mid(902)])

    def flaky(path):
        if mid(901) in path:
            raise RuntimeError("poškozené demo")
        r = parse_demo(path)
        if mid(902) in path:
            r = dict(r, map="de_nuke")
        return r

    an = make(tmp_path, FakeClient(details, hist))
    an.parse_fn = flaky
    job, _ = run(an)
    assert job.state == "partial"
    assert job.matches[mid(900)]["state"] == "parsed"
    assert job.matches[mid(901)]["state"] == "error" and "nepodařilo zpracovat" in job.matches[mid(901)]["reason"]
    assert job.matches[mid(902)]["state"] == "error" and "de_nuke" in job.matches[mid(902)]["reason"]
    assert not an.db.is_parsed(mid(901)) and not an.db.is_parsed(mid(902))
    assert job.dataset.view(side="T")["totals"]["matches"] == 1


def test_not_in_match_is_error_not_both_teams(tmp_path):
    details, hist = build_world()
    job, _ = run(make(tmp_path, FakeClient(details, hist, me_in_match=False)))
    assert job.state == "error" and "není" in job.error
    assert job.opponents == [] and job.matches == {}


def test_cancelled_during_veto(tmp_path):
    details, hist = build_world()
    client = FakeClient(details, hist, veto_seq=[current_match(None, "VOTING"), current_match(None, "CANCELLED")])
    job, _ = run(make(tmp_path, client))
    assert job.state == "error" and "zrušen" in job.error


def test_missing_map_data_is_partial(tmp_path, monkeypatch):
    details, hist = build_world(demo_for=[])
    job, _ = run(make(tmp_path, FakeClient(details, hist), mapfail=True))
    assert job.state == "partial" and "mapové podklady" in job.message


def test_invalid_url_error(tmp_path):
    an = make(tmp_path, FakeClient(*build_world()))
    job = P.Job("https://evil.com/room/x")
    an.run(job)
    assert job.state == "error"


@needs_demo
def test_real_process_pool(tmp_path, downloads):
    details, hist = build_world(on_map=[mid(900)])
    job, _ = run(make(tmp_path, FakeClient(details, hist), processes=True))
    assert job.state == "done" and job.matches[mid(900)]["state"] == "parsed"
