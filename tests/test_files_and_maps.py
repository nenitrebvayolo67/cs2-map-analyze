import bz2
import gzip
import io
import json
import os
import zipfile

import pytest

from app import demofiles as D
from app import mapdata as M
from tests.conftest import mid

DEM = D.CS2_MAGIC + b"\x00" * 1000


# ---------------------------------------------------------------- dema
def test_decompress_formats(tmp_path):
    import zstandard
    src = {"a.dem.gz": gzip.compress(DEM), "a.dem.zst": zstandard.ZstdCompressor().compress(DEM),
           "a.dem.bz2": bz2.compress(DEM), "a.dem": DEM}
    for name, data in src.items():
        p = tmp_path / name
        p.write_bytes(data)
        out = D.decompress_to_dem(p, tmp_path / f"out_{name}.dem")
        assert out.read_bytes() == DEM


def test_decompress_rejects_non_demo(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(gzip.compress(b"MZ not a demo" * 10))
    with pytest.raises(D.DemoFileError, match="není CS2"):
        D.decompress_to_dem(p, tmp_path / "o.dem")
    p.write_bytes(b"<html>login</html>")
    with pytest.raises(D.DemoFileError):
        D.decompress_to_dem(p, tmp_path / "o.dem")
    assert not (tmp_path / "o.dem").exists() and not (tmp_path / "o.dem.tmp").exists()


def test_decompress_bomb_is_stopped(tmp_path):
    p = tmp_path / "bomb.gz"
    p.write_bytes(gzip.compress(D.CS2_MAGIC + b"\x00" * (5 * 1024 * 1024)))
    with pytest.raises(D.DemoFileError, match="povolenou velikost"):
        D.decompress_to_dem(p, tmp_path / "o.dem", max_out=1024 * 1024)
    assert not (tmp_path / "o.dem").exists() and not (tmp_path / "o.dem.tmp").exists()


def test_truncated_archive(tmp_path):
    p = tmp_path / "cut.gz"
    p.write_bytes(gzip.compress(DEM * 50)[:-30])
    with pytest.raises(D.DemoFileError, match="poškozené"):
        D.decompress_to_dem(p, tmp_path / "o.dem")


def test_demo_path_rejects_traversal(tmp_path):
    assert D.demo_path(tmp_path, mid(1)).name == f"{mid(1)}.dem"
    for bad in ["../../x", f"{mid(1)}/../../a", "", "1-abc"]:
        with pytest.raises(D.DemoFileError):
            D.demo_path(tmp_path, bad)


def test_download_limits(tmp_path):
    class R(io.BytesIO):
        headers = {"Content-Length": "999999999"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with pytest.raises(D.DemoFileError, match="limit"):
        D.download("https://x/y", tmp_path / "a", max_bytes=100, opener=lambda r, timeout: R(b"x" * 10))
    R.headers = {}
    with pytest.raises(D.DemoFileError, match="limit"):
        D.download("https://x/y", tmp_path / "a", max_bytes=100, opener=lambda r, timeout: R(b"x" * 1000))
    assert not (tmp_path / "a").exists() and not (tmp_path / "a.part").exists()
    with pytest.raises(D.DemoFileError, match="HTTPS"):
        D.download("http://x/y", tmp_path / "a")
    assert D.download("https://x/y", tmp_path / "a", max_bytes=100, opener=lambda r, timeout: R(b"x" * 50)) == 50


def test_find_imports_requires_exact_id(tmp_path):
    (tmp_path / f"{mid(1)}-1-1.dem.gz").write_bytes(b"x")
    (tmp_path / "random_demo.dem").write_bytes(b"x")
    (tmp_path / f"{mid(2)}_{mid(3)}.dem").write_bytes(b"x")   # dvě ID -> nejednoznačné pro mid(2)? jen hledané
    found = D.find_imports(tmp_path, [mid(1), mid(2), mid(3)])
    assert set(found) == {mid(1)}


def test_enforce_budget_keeps_unparsed(tmp_path):
    for i in range(4):
        f = tmp_path / f"{mid(i)}.dem"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (i, i))
    removed = D.enforce_budget(tmp_path, 1500, deletable=lambda m: m != mid(0))
    left = sorted(f.stem for f in tmp_path.glob("*.dem"))
    assert removed == 3 and left == [mid(0)]


# ---------------------------------------------------------------- mapy / souřadnice
def test_transform_matches_awpy_formula(monkeypatch):
    awpy_plot = pytest.importorskip("awpy.plot")
    entry = {"pos_x": -3230, "pos_y": 1713, "scale": 5.0}
    monkeypatch.setattr(awpy_plot.data, "map_data", lambda version=None: {"de_mirage": entry})
    px, py, sc, lower = M.parse_entry("de_mirage", entry)
    mi = M.MapInfo("de_mirage", px, py, sc, lower, None, None, "t")
    for pt in [(0, 0), (-3230, 1713), (1376.0, -208.0), (-1500.5, -600.25)]:
        assert mi.to_px(*pt) == pytest.approx(awpy_plot.world_to_pixel("de_mirage", pt))


def test_transform_orientation():
    mi = M.MapInfo("m", -100, 100, 2.0, None, None, None, "t")
    assert mi.to_px(-100, 100) == (0, 0)            # levý horní roh
    assert mi.to_px(100, 100)[0] == 100             # +X doprava
    assert mi.to_px(-100, -100)[1] == 100           # -Y dolů (osa obrázku)


def test_lower_level_and_bad_entries():
    _, _, _, lower = M.parse_entry("de_nuke", {"pos_x": 1, "pos_y": 1, "scale": 7,
                                               "verticalsections": {"lower": {"AltitudeMin": -10000, "AltitudeMax": -495}}})
    mi = M.MapInfo("de_nuke", 1, 1, 7, lower, None, None, "t")
    assert mi.is_lower(-600) and not mi.is_lower(-495) and not mi.is_lower(0)
    for bad in [{}, {"pos_x": 1, "pos_y": 1, "scale": 0}, {"pos_x": "a", "pos_y": 1, "scale": 1}]:
        with pytest.raises(M.MapDataError):
            M.parse_entry("x", bad)


def test_safe_extract_radars(tmp_path):
    z = tmp_path / "images.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("radars/de_mirage.png", b"png")
        zf.writestr("radars/de_nuke_lower.png", b"png2")
        zf.writestr("../../evil/de_mirage.png", b"evil")
        zf.writestr("radars/notes.txt", b"x")
    out = M.safe_extract_radars(z, tmp_path / "r", {"de_mirage", "de_nuke_lower"})
    assert sorted(p.name for p in out) == ["de_mirage.png", "de_nuke_lower.png"]
    assert (tmp_path / "r" / "de_mirage.png").read_bytes() == b"png"
    assert not (tmp_path / "evil").exists()


def test_mapdata_ensure_with_fake_github(tmp_path):
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("radars/de_mirage.png", b"PNGDATA")
    assets = {
        M.LATEST_API: json.dumps({"tag_name": "2000930"}).encode(),
        M.ASSET_URL.format(repo=M.REPO, version="2000930", name="map_data.json"):
            json.dumps({"de_mirage": {"pos_x": -3230, "pos_y": 1713, "scale": 5.0}}).encode(),
        M.ASSET_URL.format(repo=M.REPO, version="2000930", name="images.zip"): zbuf.getvalue(),
    }
    calls = []

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout):
        calls.append(req.full_url)
        return R(assets[req.full_url])

    md = M.MapData(tmp_path, opener=opener)
    info = md.ensure("de_mirage")
    assert (info.pos_x, info.pos_y, info.scale, info.version) == (-3230, 1713, 5.0, "2000930")
    assert info.radar.read_bytes() == b"PNGDATA"
    n = len(calls)
    md.ensure("de_mirage")                     # podruhé jen kontrola verze, nic se nestahuje
    assert len(calls) == n + 1
    with pytest.raises(M.MapDataError, match="de_unknown"):
        md.ensure("de_unknown")


def test_mapdata_offline_uses_cache(tmp_path):
    def fail(req, timeout):
        raise OSError("offline")
    with pytest.raises(M.MapDataError, match="GitHub"):
        M.MapData(tmp_path, opener=fail).ensure("de_mirage")
