"""Ověřené mapové podklady pro CS2.

Zdroj: releasy repozitáře pnxenopoulos/awpy-data (stejná data používá knihovna awpy).
map_data.json obsahuje transformaci z overview souborů hry (pos_x, pos_y, scale)
a výškové sekce pro vícepatrové mapy; images.zip obsahuje radary radars/<mapa>.png.

Transformace (stejná jako awpy.plot.world_to_pixel):
    px = (x - pos_x) / scale
    py = (pos_y - y) / scale        # osa Y obrázku roste dolů
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import MAX_ZIP_ENTRY_BYTES

REPO = "pnxenopoulos/awpy-data"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_URL = "https://github.com/{repo}/releases/download/{version}/{name}"
MAX_ASSET_BYTES = 400 * 1024 * 1024


class MapDataError(Exception):
    pass


@dataclass(frozen=True)
class MapInfo:
    name: str
    pos_x: float
    pos_y: float
    scale: float
    lower: tuple[float, float] | None
    radar: Path | None
    radar_lower: Path | None
    version: str

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.pos_x) / self.scale, (self.pos_y - y) / self.scale

    def is_lower(self, z: float) -> bool:
        return self.lower is not None and self.lower[0] <= z < self.lower[1]


def parse_entry(name: str, entry: dict) -> tuple[float, float, float, tuple[float, float] | None]:
    try:
        px, py, sc = float(entry["pos_x"]), float(entry["pos_y"]), float(entry["scale"])
    except (KeyError, TypeError, ValueError) as e:
        raise MapDataError(f"Mapová data pro {name} jsou neúplná.") from e
    if sc <= 0:
        raise MapDataError(f"Neplatné měřítko mapy {name}.")
    lower = None
    sec = (entry.get("verticalsections") or {}).get("lower")
    if isinstance(sec, dict):
        try:
            lower = (float(sec["AltitudeMin"]), float(sec["AltitudeMax"]))
        except (KeyError, TypeError, ValueError):
            lower = None
    return px, py, sc, lower


def _download(url: str, dest: Path, opener=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "CS2-Map-Analyze"})
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with (opener or urllib.request.urlopen)(req, timeout=60) as r, tmp.open("wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise MapDataError("Mapový soubor je podezřele velký – stahování přerušeno.")
                fh.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def safe_extract_radars(zip_path: Path, dest_dir: Path, wanted: set[str]) -> list[Path]:
    """Vybalí jen radary požadovaných map, se kontrolou cesty a velikosti."""
    out = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            base = name.rsplit("/", 1)[-1]
            if not base.endswith(".png") or base[:-4] not in wanted:
                continue
            if name.startswith("/") or ".." in name.split("/"):
                continue
            if info.file_size > MAX_ZIP_ENTRY_BYTES:
                raise MapDataError("Radar v archivu je podezřele velký.")
            target = dest_dir / base
            with zf.open(info) as src, target.open("wb") as dst:
                copied = 0
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_ZIP_ENTRY_BYTES:
                        raise MapDataError("Radar v archivu je podezřele velký.")
                    dst.write(chunk)
            out.append(target)
    return out


class MapData:
    def __init__(self, map_dir: Path, opener=None):
        self.dir = map_dir
        self.opener = opener
        self._lock = threading.Lock()

    def _version(self) -> str:
        cur = self.dir / "current.txt"
        try:
            req = urllib.request.Request(LATEST_API, headers={"User-Agent": "CS2-Map-Analyze",
                                                              "Accept": "application/vnd.github+json"})
            with (self.opener or urllib.request.urlopen)(req, timeout=20) as r:
                tag = str(json.loads(r.read().decode()).get("tag_name") or "").strip()
            if tag and tag.replace(".", "").isalnum():
                self.dir.mkdir(parents=True, exist_ok=True)
                cur.write_text(tag, encoding="utf-8")
                return tag
        except Exception:
            pass
        if cur.exists():
            return cur.read_text(encoding="utf-8").strip()
        raise MapDataError("Nepodařilo se stáhnout mapové podklady (GitHub nedostupný a nic není v cache).")

    def ensure(self, map_name: str) -> MapInfo:
        with self._lock:
            version = self._version()
            vdir = self.dir / version
            md_path = vdir / "map_data.json"
            if not md_path.exists():
                _download(ASSET_URL.format(repo=REPO, version=version, name="map_data.json"), md_path, self.opener)
            data = json.loads(md_path.read_text(encoding="utf-8"))
            if map_name not in data:
                raise MapDataError(f"Pro mapu {map_name} nejsou ověřené mapové podklady.")
            px, py, sc, lower = parse_entry(map_name, data[map_name])
            rdir = vdir / "radars"
            radar = rdir / f"{map_name}.png"
            radar_lower = rdir / f"{map_name}_lower.png"
            if not radar.exists():
                with tempfile.TemporaryDirectory() as td:
                    z = Path(td) / "images.zip"
                    _download(ASSET_URL.format(repo=REPO, version=version, name="images.zip"), z, self.opener)
                    safe_extract_radars(z, rdir, set(data.keys()) | {f"{k}_lower" for k in data})
            return MapInfo(map_name, px, py, sc, lower,
                           radar if radar.exists() else None,
                           radar_lower if (lower and radar_lower.exists()) else None, version)

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
