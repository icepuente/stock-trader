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
