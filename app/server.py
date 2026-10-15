"""Lokální webový server (jen 127.0.0.1). Spuštění: python -m app.server"""
from __future__ import annotations

import json
import re
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import INTERVALS, WEB_DIR, load_settings
from .faceit import FaceitError, parse_match_input
from .pipeline import Analyzer

STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/static/style.css": ("style.css", "text/css; charset=utf-8")}
JOB_RE = re.compile(r"^/api/job/([0-9a-f]{12})(?:/(view|radar|render))?$")
MAX_BODY = 16 * 1024


def make_handler(analyzer: Analyzer, port_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MappioDIY"

        def log_message(self, fmt, *args):  # tichý log, jen chyby
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                sys.stderr.write("%s\n" % (fmt % args))

        # -- bezpečnost: jen lokální host (ochrana proti DNS rebinding a CSRF)
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            allowed = {f"127.0.0.1:{port_ref['port']}", f"localhost:{port_ref['port']}"}
            return host in allowed

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return origin.lower() in {f"http://127.0.0.1:{port_ref['port']}", f"http://localhost:{port_ref['port']}"}

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _err(self, code: int, msg: str):
            self._json(code, {"error": msg})

        def do_GET(self):
            if not self._host_ok():
                return self._err(403, "Nepovolený host.")
            u = urlparse(self.path)
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                return self._send(200, (WEB_DIR / name).read_bytes(), ctype,
                                  {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: https:; "
                                   "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                                   "connect-src 'self'; script-src 'self'"})
            if u.path == "/api/status":
                s = analyzer.s
                return self._json(200, {"api_key": bool(s.api_key), "identity": bool(s.nickname or s.player_id),
                                        "download_token": bool(s.download_token),
                                        "intervals": [k for k, _, _ in INTERVALS],
                                        "latest_job": max(analyzer.jobs.values(), key=lambda j: j.created).id
                                        if analyzer.jobs else None})
            m = JOB_RE.match(u.path)
            if not m:
                return self._err(404, "Nenalezeno.")
            job = analyzer.jobs.get(m.group(1))
            if not job:
                return self._err(404, "Analýza neexistuje (server byl možná restartován).")
            if m.group(2) is None:
                return self._json(200, job.snapshot())
            if m.group(2) == "radar":
                lower = parse_qs(u.query).get("lower", ["0"])[0] == "1"
                mi = job.mapinfo
                path = (mi.radar_lower if lower else mi.radar) if mi else None
                if not path or not path.exists():
                    return self._err(404, "Radar není k dispozici.")
                return self._send(200, path.read_bytes(), "image/png", {"Cache-Control": "max-age=3600"})
            if m.group(2) == "view":
                ds = job.dataset
                if ds is None:
                    return self._err(409, "Data ještě nejsou připravena.")
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                players = [p for p in q.get("players", "").split(",") if p]
                try:
                    data = ds.view(players=players or None, side=q.get("side", "ALL"),
                                   interval=q.get("interval", "all"), econ=q.get("econ", "all"),
                                   weight=q.get("weight", "rounds"), level=int(q.get("level", "0") or 0))
                except ValueError as e:
                    return self._err(400, str(e))
                data["data_version"] = job.data_version
                return self._json(200, data)
            return self._err(405, "Nepovolená metoda.")

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                return self._err(403, "Nepovolený původ požadavku.")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._err(413, "Požadavek je příliš velký.")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._err(400, "Neplatný JSON.")
            u = urlparse(self.path)
            if u.path == "/api/analyze":
                s = analyzer.s
                if not s.api_key:
                    return self._err(400, "Chybí FACEIT_API_KEY v souboru .env – spusť SETUP.bat.")
                if not (s.nickname or s.player_id):
                    return self._err(400, "Chybí FACEIT_NICKNAME v souboru .env.")
                url = str(body.get("url") or "")[:500]
                try:
                    parse_match_input(url)
                except FaceitError as e:
                    return self._err(400, str(e))
                job = analyzer.submit(url)
                return self._json(202, {"job": job.id})
            m = JOB_RE.match(u.path)
            if m and m.group(2) == "render":
                job = analyzer.jobs.get(m.group(1))
                if not job:
                    return self._err(404, "Analýza neexistuje.")
                try:
                    ms = float(body.get("ms"))
                except (TypeError, ValueError):
                    return self._err(400, "Chybí čas.")
                with job.lock:
                    job.steps["render"].update(status="done", seconds=round(max(0.0, ms) / 1000, 3))
                return self._json(200, {"ok": True})
            return self._err(404, "Nenalezeno.")

    return Handler


def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit("Nenašel jsem volný port pro server.")


def main(open_browser: bool = True) -> None:
    settings = load_settings()
    settings.host = "127.0.0.1"   # záměrně napevno: aplikace není dostupná ze sítě
    analyzer = Analyzer(settings)
    port = _free_port(settings.host, settings.port)
    port_ref = {"port": port}
    httpd = ThreadingHTTPServer((settings.host, port), make_handler(analyzer, port_ref))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"Mappio DIY běží na {url}  (zavřením tohoto okna aplikaci ukončíš)")
    if not settings.api_key:
        print("POZOR: v .env chybí FACEIT_API_KEY.")
    if not settings.download_token:
        print("Info: bez FACEIT_DOWNLOAD_TOKEN nelze dema stahovat automaticky.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main("--no-browser" not in sys.argv)
