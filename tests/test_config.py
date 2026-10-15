import pytest

from app import config as C
from app import setup_env as SE


def test_env_placeholders_are_not_values(tmp_path, monkeypatch):
    for k in ("FACEIT_API_KEY", "FACEIT_NICKNAME", "FACEIT_DOWNLOAD_TOKEN", "FACEIT_PLAYER_ID", "DATA_DIR", "PORT"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("FACEIT_API_KEY=SEM_KLIC\nFACEIT_NICKNAME=hrac\n# komentar\nPORT=9000\nKEEP_DEMOS=1\n", encoding="utf-8")
    s = C.load_settings(p)
    assert s.api_key == "" and s.nickname == "hrac" and s.port == 9000 and s.keep_demos
    assert s.host == "127.0.0.1"


def test_write_env_preserves_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# klice\nFACEIT_API_KEY=SEM_KLIC\nPORT=8765\n", encoding="utf-8")
    SE.write_env(p, {"FACEIT_API_KEY": "abc-123", "FACEIT_NICKNAME": "hrac"})
    txt = p.read_text(encoding="utf-8")
    assert "# klice" in txt and "FACEIT_API_KEY=abc-123" in txt and "FACEIT_NICKNAME=hrac" in txt and "PORT=8765" in txt


def test_write_env_rejects_injection(tmp_path):
    with pytest.raises(ValueError):
        SE.write_env(tmp_path / ".env", {"FACEIT_API_KEY": "abc\nPORT=1"})
