"""Bezpečná práce se soubory dem: stahování, rozbalování s limity, import, úklid disku."""
from __future__ import annotations

import bz2
import gzip
import shutil
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .config import MAX_COMPRESSED_BYTES, MAX_DEMO_BYTES
from .faceit import MATCH_ID_RE, _FULL_ID_RE

CS2_MAGIC = b"PBDEMS2\x00"
GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BZ2_MAGIC = b"BZh"
CHUNK = 1 << 20


class DemoFileError(Exception):
    pass


def demo_path(demo_dir: Path, match_id: str) -> Path:
    """Cesta k demu – název je VŽDY odvozen z ověřeného match ID (žádný path traversal)."""
    if not _FULL_ID_RE.match(match_id or ""):
        raise DemoFileError("Neplatné ID zápasu pro název souboru.")
    p = (demo_dir / f"{match_id}.dem").resolve()
    if p.parent != demo_dir.resolve():
        raise DemoFileError("Neplatná cesta k demu.")
    return p


def download(url: str, dest: Path, max_bytes: int = MAX_COMPRESSED_BYTES,
             opener: Callable | None = None, progress: Callable[[int], None] | None = None) -> int:
    if not url.lower().startswith("https://"):
        raise DemoFileError("Odkaz ke stažení dema není HTTPS.")
    tmp = dest.with_name(dest.name + ".part")
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mappio-diy"})
        with (opener or urllib.request.urlopen)(req, timeout=60) as r, tmp.open("wb") as fh:
            length = r.headers.get("Content-Length") if getattr(r, "headers", None) else None
            if length and length.isdigit() and int(length) > max_bytes:
                raise DemoFileError("Demo je větší než povolený limit.")
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DemoFileError("Demo je větší než povolený limit – stahování přerušeno.")
                fh.write(chunk)
                if progress:
                    progress(len(chunk))
        tmp.replace(dest)
        return total
    finally:
        tmp.unlink(missing_ok=True)


def _copy_limited(src, dst, limit: int) -> int:
    written = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return written
        written += len(chunk)
        if written > limit:
            raise DemoFileError("Rozbalené demo překročilo povolenou velikost (ochrana proti zahlcení disku).")
        dst.write(chunk)


def decompress_to_dem(src: Path, dest: Path, max_out: int = MAX_DEMO_BYTES) -> Path:
    """Rozbalí .gz / .zst / .bz2 nebo zkopíruje .dem. Ověří, že výsledek je CS2 demo."""
    with src.open("rb") as fh:
        head = fh.read(8)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tmp.open("wb") as out:
            if head[:2] == GZIP_MAGIC:
                with gzip.open(src, "rb") as fi:
                    _copy_limited(fi, out, max_out)
            elif head[:4] == ZSTD_MAGIC:
                import zstandard
                with src.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as fi:
                    _copy_limited(fi, out, max_out)
            elif head[:3] == BZ2_MAGIC:
                with bz2.open(src, "rb") as fi:
                    _copy_limited(fi, out, max_out)
            elif head == CS2_MAGIC:
                if src.stat().st_size > max_out:
                    raise DemoFileError("Demo překračuje povolenou velikost.")
                with src.open("rb") as fi:
                    _copy_limited(fi, out, max_out)
            else:
                raise DemoFileError("Soubor není CS2 demo ani podporovaný archiv.")
        with tmp.open("rb") as fh:
            if fh.read(8) != CS2_MAGIC:
                raise DemoFileError("Rozbalený soubor není CS2 demo (chybí hlavička PBDEMS2).")
        tmp.replace(dest)
        return dest
    except (OSError, EOFError) as e:
        raise DemoFileError(f"Demo je poškozené nebo neúplné ({e.__class__.__name__}).") from e
    finally:
        tmp.unlink(missing_ok=True)


def find_imports(import_dir: Path, match_ids: Iterable[str]) -> dict[str, Path]:
    """Ručně vložená dema: přiřadí se jen soubory, jejichž název obsahuje přesné match ID."""
    wanted = set(match_ids)
    found: dict[str, Path] = {}
    if not import_dir.exists():
        return found
    for f in sorted(import_dir.iterdir()):
        if not f.is_file() or f.name.endswith((".part", ".tmp")):
            continue
        ids = set(MATCH_ID_RE.findall(f.name)) & wanted
        if len(ids) == 1:
            found.setdefault(ids.pop(), f)
    return found


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.glob("*") if f.is_file()) if path.exists() else 0


def check_disk(demo_dir: Path, budget_bytes: int, need_bytes: int) -> None:
    free = shutil.disk_usage(demo_dir).free
    if free < need_bytes + 2 * 1024**3:
        raise DemoFileError("Na disku je málo místa (méně než 2 GB rezervy) – stahování dem zastaveno.")
    if folder_size(demo_dir) + need_bytes > budget_bytes:
        raise DemoFileError("Byl dosažen limit místa pro dema (MAX_DEMO_DISK_GB).")


def enforce_budget(demo_dir: Path, budget_bytes: int, deletable: Callable[[str], bool]) -> int:
    """Smaže nejstarší dema, která už jsou zpracovaná (pozice zůstávají v cache)."""
    files = sorted((f for f in demo_dir.glob("*.dem") if f.is_file()), key=lambda f: f.stat().st_mtime)
    size = sum(f.stat().st_size for f in files)
    removed = 0
    for f in files:
        if size <= budget_bytes:
            break
        if deletable(f.stem):
            s = f.stat().st_size
            f.unlink(missing_ok=True)
            size -= s
            removed += 1
    return removed
