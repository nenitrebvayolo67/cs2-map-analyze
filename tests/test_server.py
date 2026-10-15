import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from app import pipeline as P
from app.server import make_handler
from tests.conftest import make_settings, mid
from tests.test_pipeline import CUR, FakeClient, FakeMapData, build_world, current_match


@pytest.fixture
def server(tmp_path):
    s = make_settings(tmp_path)
    s.download_token = "secret-dl-token"
    details, hist = build_world(demo_for=[])
    an = P.Analyzer(s, client_factory=lambda: FakeClient(details, hist, token=""),
                    mapdata=FakeMapData(tmp_path), use_processes=False, sleep=lambda x: None)
    port_ref = {"port": 0}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(an, port_ref))
    port_ref["port"] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield an, port_ref["port"]
    httpd.shutdown()


def req(port, method, path, body=None, host=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def test_status_does_not_leak_keys(server):
    an, port = server
    code, data = req(port, "GET", "/api/status")
    assert code == 200 and data["api_key"] is True and data["download_token"] is True
    assert "secret" not in json.dumps(data) and "test-key" not in json.dumps(data)


def test_static_whitelist(server):
    _, port = server
    assert req(port, "GET", "/")[0] == 200
    assert req(port, "GET", "/static/app.js")[0] == 200
    for p in ["/static/../app/config.py", "/static/..%2f..%2f.env", "/.env", "/app/config.py"]:
        assert req(port, "GET", p)[0] == 404


def test_rejects_foreign_host_and_origin(server):
    _, port = server
    assert req(port, "GET", "/api/status", host="evil.example")[0] == 403
    code, _ = req(port, "POST", "/api/analyze", {"url": f"https://www.faceit.com/cs/cs2/room/{CUR}"},
                  origin="https://evil.example")
    assert code == 403


def test_validates_url(server):
    _, port = server
    code, data = req(port, "POST", "/api/analyze", {"url": "https://evil.com/x"})
    assert code == 400 and "faceit" in data["error"].lower()


def test_full_api_roundtrip(server):
    an, port = server
    code, data = req(port, "POST", "/api/analyze", {"url": f"https://www.faceit.com/cs/cs2/room/{CUR}"},
                     origin=f"http://127.0.0.1:{port}")
    assert code == 202
    jid = data["job"]
    for _ in range(100):
        code, snap = req(port, "GET", f"/api/job/{jid}")
        if snap["final"]:
            break
        time.sleep(0.1)
    assert snap["state"] == "partial" and snap["state_label"] == "Částečně hotovo"
    assert len(snap["opponents"]) == 5
    assert req(port, "GET", f"/api/job/{jid}/view?side=XX&interval=nope")[0] in (400, 409)
    assert req(port, "POST", f"/api/job/{jid}/render", {"ms": 12.5}, origin=f"http://127.0.0.1:{port}")[0] == 200
    assert req(port, "GET", f"/api/job/{jid}")[1]["steps"]["render"]["seconds"] == 0.013
    assert req(port, "GET", "/api/job/000000000000")[0] == 404


def test_new_job_cancels_running_one(tmp_path):
    """Přerušení: běžící analýza (čekající na veto) se při nové analýze zruší a neoznačí jako hotová."""
    details, hist = build_world(demo_for=[])
    waiting = FakeClient(details, hist, veto_seq=[current_match(None, "VOTING")])
    an = P.Analyzer(make_settings(tmp_path), client_factory=lambda: waiting, mapdata=FakeMapData(tmp_path),
                    use_processes=False)
    first = an.submit(f"https://www.faceit.com/cs/cs2/room/{CUR}")
    for _ in range(100):
        if first.state == "waiting_veto":
            break
        time.sleep(0.05)
    assert first.state == "waiting_veto"
    an.submit(f"https://www.faceit.com/cs/cs2/room/{CUR}")
    for _ in range(100):
        if first.state == "cancelled":
            break
        time.sleep(0.05)
    assert first.state == "cancelled"
    for j in an.jobs.values():
        j.cancel_event.set()
