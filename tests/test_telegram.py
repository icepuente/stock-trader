"""Telegram bridge tests — fake transport, no network, no threads."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from stock_trader.telegram import HeadlessRunner, TelegramBridge, maybe_start_bridge

from conftest import clean_settings

NY = ZoneInfo("America/New_York")


class FakeProvider:
    name = "sim"
    mode_label = "SIM"

    def account(self):
        return {"equity": 100_000.0, "cash": 50_000.0, "day_pl": 12.5}

    def positions(self):
        return [{"symbol": "AAPL", "qty": 95, "avg_entry": 103.69, "price": 105.42,
                 "unrealized_pl": 164.35, "unrealized_plpc": 0.0167}]

    def now(self):
        return datetime(2026, 7, 10, 12, 0, tzinfo=NY)

    def market_is_open(self):
        return True


class FakeBot:
    def __init__(self):
        self.provider = FakeProvider()
        self.watchlist = ["AAPL"]
        self.states = {}


class FakeRunner:
    def __init__(self, bot=None):
        self.bot = bot
        self.running = False
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stopped += 1
        self.running = False


def make_bridge(runner=None, chat_id=""):
    settings = clean_settings(telegram_bot_token="tok:token",
                              telegram_chat_id=chat_id)
    bridge = TelegramBridge(settings, runner or FakeRunner())
    sent = []
    bridge._api = lambda method, **params: sent.append((method, params)) or []
    return bridge, sent


@pytest.fixture(autouse=True)
def _no_leaked_handlers():
    yield
    # bridges attach a log-ring handler to the root logger; drop any leftovers
    import logging
    from stock_trader.telegram import _LogRing
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, _LogRing):
            root.removeHandler(h)


def test_no_token_no_bridge():
    assert maybe_start_bridge(clean_settings(), FakeRunner()) is None


def test_start_binds_first_chat():
    bridge, sent = make_bridge()
    assert bridge.chat_id is None
    bridge._handle("111", "/start")
    assert bridge.chat_id == "111"
    assert sent and sent[0][1]["chat_id"] == "111"


def test_unbound_chat_ignored_until_start():
    bridge, sent = make_bridge()
    bridge._handle("111", "/status")
    assert sent == [] and bridge.chat_id is None


def test_pinned_chat_locks_out_strangers():
    bridge, sent = make_bridge(chat_id="999")
    bridge._handle("111", "/status")   # stranger: silently ignored
    assert sent == []
    bridge._handle("999", "/help")
    assert len(sent) == 1


def test_run_and_stop_commands_drive_runner():
    runner = FakeRunner()
    bridge, sent = make_bridge(runner, chat_id="1")
    bridge._handle("1", "/run")
    assert runner.started == 1 and runner.running
    bridge._handle("1", "/run")        # idempotent: replies "already running"
    assert runner.started == 1
    bridge._handle("1", "/stop")
    assert runner.stopped == 1 and not runner.running


def test_status_account_positions_replies():
    runner = FakeRunner(bot=FakeBot())
    runner.running = True
    bridge, sent = make_bridge(runner, chat_id="1")
    bridge._handle("1", "/status")
    bridge._handle("1", "/account")
    bridge._handle("1", "/positions")
    texts = [p["text"] for _, p in sent]
    assert "running" in texts[0] and "AAPL" in texts[0]
    assert "$100,000.00" in texts[1]
    assert "AAPL ×95" in texts[2] and "+1.7%" in texts[2]


def test_positions_without_bot_reports_error():
    bridge, sent = make_bridge(FakeRunner(bot=None), chat_id="1")
    with pytest.raises(RuntimeError, match="/run"):
        bridge._handle("1", "/positions")


def test_send_event_only_when_bound_and_escapes_html():
    bridge, sent = make_bridge()
    bridge.send_event("hello")               # unbound -> dropped
    assert sent == []
    bridge.chat_id = "1"
    bridge.send_event("A <b> & B")
    assert sent[0][1]["text"] == "A &lt;b&gt; &amp; B"


def test_send_event_never_raises():
    bridge, _ = make_bridge(chat_id="1")
    def boom(method, **params):
        raise RuntimeError("network down")
    bridge._api = boom
    bridge.send_event("hello")  # must not propagate into the bot loop


def test_log_command_returns_recent_lines():
    import logging
    bridge, sent = make_bridge(chat_id="1")
    lg = logging.getLogger("x")
    lg.setLevel(logging.INFO)
    lg.info("hello <world>")
    bridge._handle("1", "/log 5")
    assert "hello &lt;world&gt;" in sent[0][1]["text"]
    bridge.stop()


def test_headless_runner_stop_sets_event():
    import threading

    class MiniBot:
        stop_event = threading.Event()

    runner = HeadlessRunner(MiniBot())
    assert runner.running
    runner.stop()
    assert not runner.running
    with pytest.raises(RuntimeError):
        runner.start()
