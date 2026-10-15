import io
import json
import urllib.error
import urllib.parse

import pytest

from app import faceit as F
from tests.conftest import mid

ID = mid(1)


# ---------------------------------------------------------------- URL
@pytest.mark.parametrize("url", [
    f"https://www.faceit.com/cs/cs2/room/{ID}",
    f"https://www.faceit.com/en/cs2/room/{ID}/scoreboard",
    f"https://faceit.com/en/cs2/room/{ID}?src=share",
    f"  {ID}  ",
])
def test_parse_match_input_valid(url):
    assert F.parse_match_input(url) == ID


@pytest.mark.parametrize("url", [
    "", "ahoj", f"https://evil.com/cs2/room/{ID}", f"https://faceit.com.evil.com/room/{ID}",
    f"javascript:alert(1)//{ID}", f"https://www.faceit.com/cs/cs2/room/{ID}/{mid(2)}",
    "https://www.faceit.com/cs/cs2/room/1-abc", f"ftp://www.faceit.com/room/{ID}",
])
def test_parse_match_input_invalid(url):
    with pytest.raises(F.FaceitError):
        F.parse_match_input(url)


# ---------------------------------------------------------------- demo_url
@pytest.mark.parametrize("value,expected", [
    (None, []), ("", []), ([], []),
    ("https://demos.faceit.com/a.dem.gz", ["https://demos.faceit.com/a.dem.gz"]),
    (["https://d/a.dem.zst", "https://d/a.dem.zst", None, 5], ["https://d/a.dem.zst"]),
    ([{"resource_url": "https://d/b.dem.gz"}], ["https://d/b.dem.gz"]),
    (["file:///etc/passwd", "relative/path"], []),
    ({"url": "https://d/c.dem"}, ["https://d/c.dem"]),
])
def test_normalize_demo_urls(value, expected):
    assert F.normalize_demo_urls(value) == expected


# ---------------------------------------------------------------- veto
@pytest.mark.parametrize("voting,expected", [
    (None, None), ({}, None), ({"map": {"pick": []}}, None),
    ({"map": {"pick": ["de_mirage"]}}, "de_mirage"), ({"map": {"pick": "de_nuke"}}, "de_nuke"),
    ({"map": {"pick": ["de_mirage", "de_nuke"]}}, None),          # BO3 apod. -> nejednoznačné
    ({"map": {"pick": ["<script>"]}}, None), ({"map": {"pick": ["De_Inferno"]}}, "de_inferno"),
])
def test_picked_map(voting, expected):
    assert F.picked_map({"voting": voting}) == expected


def test_map_from_stats():
    assert F.map_from_stats({"rounds": [{"round_stats": {"Map": "de_dust2"}}]}) == "de_dust2"
    assert F.map_from_stats({"rounds": []}) is None
    assert F.map_from_stats({"rounds": [{"round_stats": {"Map": "a"}}, {"round_stats": {"Map": "b"}}]}) is None


# ---------------------------------------------------------------- identifikace soupeřů
def _match(f1, f2):
    return {"teams": {"faction1": {"name": "A", "roster": [{"player_id": p, "nickname": p} for p in f1]},
                      "faction2": {"name": "B", "roster": [{"player_id": p, "nickname": p} for p in f2]}}}


def test_identify_me_in_faction1():
    my, opp_key, opp = F.identify_opponents(_match(["me", "a2", "a3", "a4", "a5"], ["b1", "b2", "b3", "b4", "b5"]), "me")
    assert (my, opp_key) == ("faction1", "faction2")
    assert [p["player_id"] for p in opp] == ["b1", "b2", "b3", "b4", "b5"]


def test_identify_me_in_faction2():
    _, _, opp = F.identify_opponents(_match(["a1", "a2"], ["x", "me"]), "me")
    assert [p["player_id"] for p in opp] == ["a1", "a2"]


def test_identify_not_in_match_never_analyses_both_teams():
    with pytest.raises(F.IdentificationError, match="není"):
        F.identify_opponents(_match(["a1"], ["b1"]), "me")


def test_identify_ambiguous_and_broken():
    with pytest.raises(F.IdentificationError):
        F.identify_opponents(_match(["me"], ["me"]), "me")
    with pytest.raises(F.IdentificationError):
        F.identify_opponents({"teams": {}}, "me")
    with pytest.raises(F.IdentificationError):
        F.identify_opponents(_match(["me"], ["b", "b"]), "me")
    with pytest.raises(F.IdentificationError):
        F.identify_opponents(_match(["me"], ["b"]), "")


def test_steam_id_validation():
    assert F.steam_id_of({"game_player_id": "76561198244754626"}) == "76561198244754626"
    assert F.steam_id_of({"game_player_id": "STEAM_1:0:1"}) is None
    assert F.steam_id_of({}) is None


# ---------------------------------------------------------------- výběr posledních 30
def test_select_recent_finished_order_filter_dedupe():
    items = []
    for i in range(40):
        items.append({"match_id": mid(100 + i), "status": "finished", "finished_at": 1000 + i})
    items.insert(3, {"match_id": mid(500), "status": "cancelled", "finished_at": 99999})
    items.insert(5, {"match_id": mid(501), "status": "ONGOING", "finished_at": 99998})
    items.append({"match_id": mid(139), "status": "finished", "finished_at": 1039})  # duplicita
    items.append({"match_id": mid(7), "status": "finished", "finished_at": 5000})    # aktuální zápas
    import random
    random.Random(1).shuffle(items)
    sel = F.select_recent_finished(items, mid(7), 30)
    assert len(sel) == 30
    assert [s["match_id"] for s in sel] == [mid(100 + i) for i in range(39, 9, -1)]  # od nejnovějšího
    assert all(s["finished_at"] >= 1010 for s in sel)


def test_select_recent_uses_started_at_fallback_and_status_case():
    items = [{"match_id": mid(1), "status": "FINISHED", "started_at": 50},
             {"match_id": mid(2), "status": "finished", "finished_at": 60},
             {"match_id": "neplatne", "status": "finished", "finished_at": 70}]
    assert [s["match_id"] for s in F.select_recent_finished(items, "", 30)] == [mid(2), mid(1)]


# ---------------------------------------------------------------- HTTP klient
class FakeResp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, None)


def test_history_sends_from_and_paginates():
    calls = []

    def opener(req, timeout):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        calls.append(q)
        off = int(q["offset"][0])
        n = 100 if off == 0 else 20
        items = [{"match_id": mid(off + i + 1), "status": "finished" if i % 4 else "cancelled",
                  "finished_at": 10_000 - off - i} for i in range(n)]
        return FakeResp(json.dumps({"items": items}).encode())

    c = F.FaceitClient("k", opener=opener, sleep=lambda s: None)
    sel, scanned = c.history("p", 30)
    assert len(sel) == 30 and scanned == 100
    assert calls[0]["from"] == [str(F.HISTORY_FROM_TS)] and calls[0]["game"] == ["cs2"]


def test_history_pages_until_enough_finished():
    def opener(req, timeout):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        off = int(q["offset"][0])
        items = [{"match_id": mid(off + i + 1), "status": "finished" if (off >= 100 or i < 10) else "cancelled",
                  "finished_at": 10_000 - off - i} for i in range(100 if off < 200 else 0)]
        return FakeResp(json.dumps({"items": items}).encode())

    sel, scanned = F.FaceitClient("k", opener=opener, sleep=lambda s: None).history("p", 30)
    assert len(sel) == 30 and scanned == 200


def test_retry_on_429_then_success():
    seq = [_http_error(429, {"Retry-After": "1"}), _http_error(503)]
    slept = []

    def opener(req, timeout):
        if seq:
            raise seq.pop(0)
        return FakeResp(b'{"status":"FINISHED"}')

    c = F.FaceitClient("k", opener=opener, sleep=slept.append)
    assert c.get_match(ID)["status"] == "FINISHED"
    assert len(slept) == 2 and slept[0] >= 1


@pytest.mark.parametrize("code,match", [(401, "přístup"), (403, "přístup"), (404, "404")])
def test_auth_and_404_errors(code, match):
    def opener(req, timeout):
        raise _http_error(code)
    with pytest.raises(F.FaceitError, match=match):
        F.FaceitClient("k", opener=opener, sleep=lambda s: None).get_match(ID)


def test_429_forever_reports_rate_limit():
    def opener(req, timeout):
        raise _http_error(429)
    with pytest.raises(F.FaceitError, match="429"):
        F.FaceitClient("k", opener=opener, sleep=lambda s: None, max_retries=2).get_match(ID)


def test_missing_api_key():
    with pytest.raises(F.FaceitError):
        F.FaceitClient("")


def test_downloads_api():
    sent = {}

    def opener(req, timeout):
        sent["url"], sent["body"], sent["auth"] = req.full_url, json.loads(req.data), req.get_header("Authorization")
        return FakeResp(b'{"payload":{"download_url":"https://cdn.example/x.dem.zst?sig=1"}}')

    c = F.FaceitClient("k", download_token="dl", opener=opener)
    assert c.signed_demo_url("https://demos.faceit.com/x.dem.zst") == "https://cdn.example/x.dem.zst?sig=1"
    assert sent["url"] == F.DOWNLOAD_API and sent["body"] == {"resource_url": "https://demos.faceit.com/x.dem.zst"}
    assert sent["auth"] == "Bearer dl"


def test_downloads_api_without_token_and_bad_response():
    with pytest.raises(F.FaceitError, match="DOWNLOAD_TOKEN"):
        F.FaceitClient("k").signed_demo_url("https://d/x")
    c = F.FaceitClient("k", download_token="t", opener=lambda r, t: FakeResp(b'{"payload":{"download_url":"http://x"}}'))
    with pytest.raises(F.FaceitError):
        c.signed_demo_url("https://d/x")
