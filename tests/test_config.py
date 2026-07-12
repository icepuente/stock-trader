"""In-app configuration: apply_config validation, .env persistence, API."""

import pytest
from fastapi.testclient import TestClient

import stock_trader.config as config
from stock_trader.config import apply_config, public_config, save_env
from stock_trader.server import create_app

from conftest import clean_settings


# ------------------------------------------------------------ apply_config --
def test_apply_config_types_and_env_names():
    s = clean_settings()
    env = apply_config(s, {
        "provider": "ibkr",
        "ibkr_port": "7497",
        "sim_speed": 120,
        "bitget_uta": True,
        "bitget_symbols": " BTC/USDT:USDT , ETH/USDT:USDT ",
    })
    assert s.provider == "ibkr" and s.ibkr_port == 7497
    assert s.sim_speed == 120.0 and s.bitget_uta is True
    assert s.bitget_symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert env == {
        "PROVIDER": "ibkr", "IBKR_PORT": "7497", "SIM_SPEED": "120.0",
        "BITGET_UTA": "1", "BITGET_SYMBOLS": "BTC/USDT:USDT,ETH/USDT:USDT",
    }


def test_apply_config_rejects_bad_values():
    s = clean_settings()
    with pytest.raises(ValueError, match="provider"):
        apply_config(s, {"provider": "robinhood"})
    with pytest.raises(ValueError, match="number"):
        apply_config(s, {"ibkr_port": "abc"})
    with pytest.raises(ValueError, match="unknown"):
        apply_config(s, {"hard_stop_pct": "0.5"})  # not dashboard-editable


def test_secret_blank_keeps_mask_keeps_dash_clears():
    s = clean_settings(api_key="SECRETKEY123")
    masked = public_config(s)["alpaca_api_key"]
    assert masked == "••••Y123" and "SECRETKEY123" not in masked

    assert apply_config(s, {"alpaca_api_key": ""}) == {}
    assert apply_config(s, {"alpaca_api_key": masked}) == {}
    assert s.api_key == "SECRETKEY123"

    env = apply_config(s, {"alpaca_api_key": "NEWKEY9876"})
    assert s.api_key == "NEWKEY9876" and env["ALPACA_API_KEY"] == "NEWKEY9876"

    env = apply_config(s, {"alpaca_api_key": "-"})
    assert s.api_key == "" and env["ALPACA_API_KEY"] == ""


# ----------------------------------------------------------------- save_env --
def test_save_env_updates_in_place(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment stays\n"
        "PROVIDER=alpaca\n"
        "ALPACA_API_KEY=old\n"
        "#TELEGRAM_BOT_TOKEN=123456789:AA...\n"
    )
    save_env({"PROVIDER": "sim", "TELEGRAM_BOT_TOKEN": "tok:abc",
              "IBKR_PORT": "4002"}, p)
    text = p.read_text()
    assert "# comment stays" in text
    assert "PROVIDER=sim" in text and "PROVIDER=alpaca" not in text
    assert "ALPACA_API_KEY=old" in text                 # untouched
    assert "TELEGRAM_BOT_TOKEN=tok:abc" in text         # placeholder uncommented
    assert "#TELEGRAM_BOT_TOKEN" not in text
    assert text.rstrip().endswith("IBKR_PORT=4002")     # appended


def test_save_env_quotes_awkward_values(tmp_path):
    p = tmp_path / ".env"
    save_env({"X": "a b#c", "Y": ""}, p)
    assert 'X="a b#c"' in p.read_text() and 'Y=""' in p.read_text()


# --------------------------------------------------------------------- api --
def test_config_api_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    settings = clean_settings(provider="sim", sim_scenario="demo")
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    c = client.get("/api/config").json()
    assert c["provider"] == "sim" and c["sim_scenario"] == "demo"
    assert c["alpaca_api_key"] == ""    # nothing saved, nothing leaked

    r = client.post("/api/config", json={"provider": "alpaca",
                                         "alpaca_api_key": "PKTESTKEY123",
                                         "alpaca_secret_key": "supersecret99"})
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["provider"] == "alpaca"
    assert body["config"]["alpaca_api_key"] == "••••Y123"
    assert set(body["saved"]) == {"PROVIDER", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"}
    assert settings.provider == "alpaca" and settings.api_key == "PKTESTKEY123"

    env_text = (tmp_path / ".env").read_text()
    assert "PROVIDER=alpaca" in env_text and "PKTESTKEY123" in env_text

    status = client.get("/api/status").json()
    assert status["provider"] == "alpaca" and status["mode"] == "PAPER"


def test_config_api_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    client = TestClient(create_app(clean_settings()), raise_server_exceptions=False)
    r = client.post("/api/config", json={"provider": "etrade"})
    assert r.status_code == 400 and "provider" in r.json()["detail"]
    assert not (tmp_path / ".env").exists()   # nothing persisted on error
