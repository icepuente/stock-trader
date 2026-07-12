"""Self-updater: zip extraction, version comparison, error surfacing.
No network, no git, and never a real update/restart."""

import io
import urllib.error
import zipfile
from pathlib import Path

from stock_trader.updater import Updater, _extract_over

from conftest import clean_settings


def make_zipball(path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        top = "icepuente-stock-trader-abc1234/"
        zf.writestr(top + "main.py", "print('new')\n")
        zf.writestr(top + "stock_trader/bot.py", "# updated\n")
        zf.writestr(top + "newfile.txt", "hello\n")
        zf.writestr(top + ".env", "STOLEN=1\n")            # must be skipped
        zf.writestr(top + ".venv/lib.py", "nope\n")        # must be skipped
        zf.writestr(top + "VERSION", "tampered\n")         # must be skipped
    path.write_bytes(buf.getvalue())


def test_extract_over_writes_code_and_skips_local_files(tmp_path):
    zip_path = tmp_path / "ball.zip"
    make_zipball(zip_path)
    root = tmp_path / "install"
    root.mkdir()
    (root / ".env").write_text("SECRET=mine\n")

    written = _extract_over(zip_path, root)
    assert written == 3
    assert (root / "main.py").read_text() == "print('new')\n"
    assert (root / "stock_trader/bot.py").exists()
    assert (root / "newfile.txt").exists()
    assert (root / ".env").read_text() == "SECRET=mine\n"   # untouched
    assert not (root / ".venv").exists()
    assert not (root / "VERSION").exists()


def test_check_compares_shas(tmp_path, monkeypatch):
    upd = Updater(clean_settings(), root=tmp_path)          # no .git, no VERSION
    monkeypatch.setattr(upd, "remote_sha", lambda: "cafe" * 10)
    out = upd.check()
    assert out["local"] is None and out["update_available"] is True

    (tmp_path / "VERSION").write_text("cafe" * 10 + "\n")
    out = upd.check()
    assert out["local"] == "cafe" * 10 and out["update_available"] is False


def test_check_surfaces_private_repo_hint(tmp_path, monkeypatch):
    upd = Updater(clean_settings(), root=tmp_path)
    def raise_404():
        raise urllib.error.HTTPError("u", 404, "not found", {}, None)
    monkeypatch.setattr(upd, "remote_sha", raise_404)
    out = upd.check()
    assert out["update_available"] is False
    assert "token" in out["error"]


def test_update_kicks_off_worker_once(tmp_path, monkeypatch):
    upd = Updater(clean_settings(), root=tmp_path)
    calls = []
    monkeypatch.setattr(upd, "_worker", lambda: calls.append(1))
    monkeypatch.setattr(upd, "remote_sha", lambda: "beef" * 10)
    upd.update()
    assert upd.updating is True
    out = upd.update()                       # second call refuses
    assert "in progress" in out["message"]


def test_token_attached_to_api_requests(tmp_path, monkeypatch):
    upd = Updater(clean_settings(github_token="tok123"), root=tmp_path)
    seen = {}
    class FakeResp:
        def __enter__(self): return io.StringIO('{"sha": "abc"}')
        def __exit__(self, *a): return False
    def fake_open(req, timeout=0):
        seen["auth"] = req.headers.get("Authorization")
        return FakeResp()
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert upd.remote_sha() == "abc"
    assert seen["auth"] == "Bearer tok123"
