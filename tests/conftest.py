"""Sdílené pomůcky pro testy."""
from __future__ import annotations

import gzip
import shutil
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.mapdata import MapInfo  # noqa: E402

REAL_DEMO = ROOT / "tests" / "fixtures" / "mirage_sample.dem"

# Hráči ve skutečném testovacím demu (de_mirage, 9 kol, tým T na začátku)
DEMO_T = {
    "NIGHTSOUL": "76561198244754626", "123": "76561198265366770", "Golova": "76561198118803912",
    "Podsosnik": "76561197964020430", "IMI Negev": "76561198202353993",
}
DEMO_CT = {
    "Trahun": "76561198324843075", "povergo": "76561198280975787", "Dog": "76561198194694750",
    "miu miu": "76561198073049527", "ExOtiC": "76561198258044111",
}

needs_demo = pytest.mark.skipif(not REAL_DEMO.exists(), reason="chybí skutečné testovací demo")


def mid(n: int) -> str:
    """Deterministické FACEIT match ID."""
    return f"1-{n:08x}-0000-4000-8000-{n:012x}"


def tiny_png(path: Path) -> Path:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"\x00\x00\x00\x00"
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


# Testovací transformace pro de_mirage (pouze pro testy; aplikace používá stažená map_data.json).
def fixture_mapinfo(tmp: Path, lower=None) -> MapInfo:
    return MapInfo("de_mirage", -3230.0, 1713.0, 5.0, lower, tiny_png(tmp / "r.png"), None, "test")


class FakeMapData:
    def __init__(self, tmp: Path, fail: bool = False):
        self.tmp, self.fail, self.calls = tmp, fail, 0

    def ensure(self, map_name):
        from app.mapdata import MapDataError
        self.calls += 1
        if self.fail:
            raise MapDataError("Nepodařilo se stáhnout mapové podklady (test).")
        return fixture_mapinfo(self.tmp)


def make_settings(tmp: Path, **kw) -> Settings:
    s = Settings(api_key="test-key", nickname="me", data_dir=tmp / "data", **kw)
    s.ensure_dirs()
    return s


@pytest.fixture(scope="session")
def real_parse():
    if not REAL_DEMO.exists():
        pytest.skip("chybí demo")
    from app.parse import parse_demo
    return parse_demo(str(REAL_DEMO))


@pytest.fixture(scope="session")
def demo_gz(tmp_path_factory):
    if not REAL_DEMO.exists():
        pytest.skip("chybí demo")
    p = tmp_path_factory.mktemp("gz") / "demo.dem.gz"
    with REAL_DEMO.open("rb") as fi, gzip.open(p, "wb", compresslevel=1) as fo:
        shutil.copyfileobj(fi, fo)
    return p
