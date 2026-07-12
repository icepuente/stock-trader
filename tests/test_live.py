"""Live-trading gates: env-only switch, account verification, kill switches."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from stock_trader.bot import Bot
from stock_trader.providers.ibkr import assert_paper_accounts, verify_accounts
from stock_trader.server import create_app
from stock_trader.telegram import TelegramBridge

from conftest import clean_settings

NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------ config gates --
def test_live_active_requires_switch_and_ibkr():
    assert not clean_settings(provider="ibkr").live_active()
    assert not clean_settings(provider="alpaca", live_trading=True).live_active()
    assert not clean_settings(provider="sim", live_trading=True).live_active()
    assert clean_settings(provider="ibkr", live_trading=True).live_active()


def test_enabling_live_from_dashboard_requires_typed_confirmation(tmp_path, monkeypatch):
    import stock_trader.config as config
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / ".env"))
    settings = clean_settings(provider="ibkr")
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    # no confirmation -> refused, nothing changed or persisted
    r = client.post("/api/config", json={"live_trading": True})
    assert r.status_code == 428 and "LIVE" in r.json()["detail"]
    assert settings.live_trading is False
    assert not (tmp_path / ".env").exists()

    # wrong confirmation -> still refused
    r = client.post("/api/config", json={"live_trading": True, "live_confirm": "live"})
    assert r.status_code == 428 and settings.live_trading is False

    # typed confirmation -> enabled and persisted
    r = client.post("/api/config", json={"live_trading": True, "live_confirm": "LIVE"})
    assert r.status_code == 200 and settings.live_trading is True
    assert "LIVE_TRADING=1" in (tmp_path / ".env").read_text()
    assert client.get("/api/status").json()["live"] is True

    # keeping it on needs no re-confirmation; turning it OFF never does
    assert client.post("/api/config", json={"live_trading": True}).status_code == 200
    r = client.post("/api/config", json={"live_trading": False})
    assert r.status_code == 200 and settings.live_trading is False
    assert "LIVE_TRADING=0" in (tmp_path / ".env").read_text()


def test_allocation_switches_with_live_mode():
    s = clean_settings(provider="ibkr", live_trading=True)
    assert s.allocation() == s.live_allocation_usd == 500.0
    s.live_trading = False
    assert s.allocation() == s.allocation_usd == 10_000.0


# -------------------------------------------------------- account checking --
def test_verify_accounts_paper_and_live():
    assert verify_accounts(["DU1234567"]) == "PAPER"
    with pytest.raises(RuntimeError, match="LIVE account"):
        verify_accounts(["U9876543"])                    # live without opt-in
    assert verify_accounts(["U9876543"], allow_live=True) == "LIVE"
    assert verify_accounts(["DU1"], allow_live=True) == "PAPER"
    with pytest.raises(RuntimeError, match="no managed accounts"):
        verify_accounts([], allow_live=True)
    # legacy strict guard still refuses live
    with pytest.raises(RuntimeError):
        assert_paper_accounts(["U9876543"])


# --------------------------------------------------------------- API gates --
def test_bot_start_requires_typed_confirmation_when_live():
    settings = clean_settings(provider="ibkr", live_trading=True)
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    r = client.post("/api/bot/start")
    assert r.status_code == 428 and "LIVE" in r.json()["detail"]
    status = client.get("/api/status").json()
    assert status["live"] is True and status["mode"] == "LIVE"
    assert status["running"] is False
    # paper mode needs no confirmation and reports live=False
    s2 = clean_settings(provider="sim", sim_scenario="demo")
    c2 = TestClient(create_app(s2), raise_server_exceptions=False)
    assert c2.get("/api/status").json()["live"] is False


def test_telegram_run_requires_live_confirmation():
    class FakeRunner:
        running = False
        bot = None
        started = 0
        def start(self): self.started += 1

    runner = FakeRunner()
    settings = clean_settings(provider="ibkr", live_trading=True,
                              telegram_bot_token="tok", telegram_chat_id="1")
    bridge = TelegramBridge(settings, runner)
    sent = []
    bridge._api = lambda method, **p: sent.append(p) or []
    bridge._handle("1", "/run")
    assert runner.started == 0 and "/run live" in sent[-1]["text"]
    bridge._handle("1", "/run live")
    assert runner.started == 1 and "LIVE" in sent[-1]["text"]
    bridge.stop()


# ----------------------------------------------------------- daily loss cut --
class DrainingProvider:
    """Minimal provider stub whose equity falls $200 after the first read."""
    name = "stub"
    mode_label = "LIVE"

    def __init__(self):
        self.reads = 0
        self.flattened = False

    def now(self):
        return datetime(2026, 7, 10, 10, 0, tzinfo=NY)

    def account(self):
        self.reads += 1
        return {"equity": 5_000.0 if self.reads == 1 else 4_800.0, "cash": 4_800.0}

    def flatten_all(self):
        self.flattened = True


def make_live_bot(provider):
    settings = clean_settings(provider="ibkr", live_trading=True)
    bot = Bot.__new__(Bot)  # skip __init__ (would build a real provider)
    bot.s = settings
    bot.provider = provider
    bot._day_start_equity = None
    bot._equity_day = None
    return bot


def test_daily_loss_kill_switch_trips_after_baseline():
    p = DrainingProvider()
    bot = make_live_bot(p)
    now = p.now()
    assert not bot.daily_loss_exceeded(now)   # first read sets the baseline
    assert bot.daily_loss_exceeded(now)       # -$200 > $150 limit -> trip


def test_daily_loss_tolerates_account_errors():
    class Broken(DrainingProvider):
        def account(self):
            raise RuntimeError("gateway down")

    bot = make_live_bot(Broken())
    assert not bot.daily_loss_exceeded(datetime(2026, 7, 10, 10, 0, tzinfo=NY))
