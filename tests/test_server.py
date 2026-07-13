"""API tests using FastAPI's TestClient — no Alpaca keys or network needed."""

from conftest import clean_settings
from fastapi.testclient import TestClient

from stock_trader.server import create_app


def make_client(**overrides) -> TestClient:
    return TestClient(create_app(clean_settings(**overrides)))


def test_index_serves_dashboard():
    r = make_client().get("/")
    assert r.status_code == 200
    assert "stock_trader" in r.text and "<svg" in r.text.lower()


def test_status_without_keys():
    r = make_client().get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["keys_configured"] is False
    assert body["running"] is False
    assert body["provider"] == "alpaca"
    assert body["mode"] == "PAPER"


def test_status_bitget_provider():
    client = make_client(provider="bitget")
    body = client.get("/api/status").json()
    assert body["provider"] == "bitget"
    assert body["mode"] == "DEMO"
    assert body["keys_configured"] is False


def test_start_without_keys_conflicts():
    r = make_client().post("/api/bot/start")
    assert r.status_code == 409
    assert "keys" in r.json()["detail"].lower()


def test_account_without_keys_conflicts():
    assert make_client().get("/api/account").status_code == 409


def test_bars_without_keys_conflicts():
    assert make_client().get("/api/bars/AAPL").status_code == 409


def test_stop_is_idempotent():
    c = make_client()
    r = c.post("/api/bot/stop")
    assert r.status_code == 200 and r.json()["running"] is False


def test_logs_and_activity_empty_without_bot():
    c = make_client()
    assert c.get("/api/activity").json() == []
    assert c.get("/api/logs").status_code == 200


# ------------------------------------------------------ first-run wizard --
def test_first_run_until_env_exists(tmp_path, monkeypatch):
    import stock_trader.config as config
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    client = make_client(provider="sim")

    assert client.get("/api/status").json()["first_run"] is True

    # finishing the wizard saves settings -> .env created, flag clears
    r = client.post("/api/config", json={"provider": "sim", "sim_scenario": "demo"})
    assert r.status_code == 200
    assert (tmp_path / ".env").exists()
    assert client.get("/api/status").json()["first_run"] is False


def test_skipping_wizard_saves_empty_env(tmp_path, monkeypatch):
    import stock_trader.config as config
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    client = make_client(provider="sim")

    # the wizard's Skip link posts an empty config save
    r = client.post("/api/config", json={})
    assert r.status_code == 200 and r.json()["saved"] == []
    assert (tmp_path / ".env").exists()
    assert client.get("/api/status").json()["first_run"] is False


def test_dashboard_contains_wizard():
    text = make_client().get("/").text
    assert "wzoverlay" in text and "First-time setup" in text
