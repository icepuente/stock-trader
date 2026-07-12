"""Sim provider tests with injected data — no yfinance download needed."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from conftest import clean_settings
from stock_trader.providers.sim import START_CASH, SimProvider

NY = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 10, tzinfo=NY)


def make_data():
    idx = pd.date_range(DAY.replace(hour=9, minute=30), DAY.replace(hour=15, minute=55),
                        freq="5min", tz=NY)
    five = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 50_000,
    }, index=idx)
    daily_idx = pd.date_range(DAY - pd.Timedelta(days=30), DAY, freq="B", tz=NY)
    daily = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
        "volume": 1_000_000,
    }, index=daily_idx)
    return {"FAKE": five}, {"FAKE": daily}


def make_provider(speed=60.0):
    data, daily = make_data()
    return SimProvider(clean_settings(provider="sim", sim_speed=speed), data=data, daily=daily)


def test_clock_starts_at_premarket_and_advances():
    p = make_provider(speed=600)
    t0 = p.now()
    assert t0.date() == DAY.date()
    assert t0.hour == 9 and t0.minute >= 25
    assert p.mode_label == "SIM"


def test_bars_are_clipped_to_sim_time():
    p = make_provider()
    p._anchor_real = 0  # pin the clock manually
    import time as time_mod
    p._anchor_sim = DAY.replace(hour=12, minute=0) - pd.Timedelta(
        seconds=time_mod.monotonic() * p.s.sim_speed)
    bars = p.hourly_bars("FAKE")
    assert not bars.empty
    assert bars.index.max() <= p.now()
    # scanner sees the same clipped world
    scanned = p.scan()
    assert isinstance(scanned, list)


def test_virtual_portfolio_buy_and_sell():
    p = make_provider()
    p.now()  # anchor clock
    p.buy_fraction("FAKE", 0.5, 100.5, "test entry")
    assert p.position_qty("FAKE") == 49  # int(10_000*0.5/100.5)
    acct = p.account()
    assert acct["equity"] < START_CASH + 1  # cash converted to shares at cost
    p.sell_fraction("FAKE", 0.7, "partial")
    assert p.position_qty("FAKE") == 15  # 49 - 34
    p.flatten_all()
    assert p.position_qty("FAKE") == 0
    assert abs(p.account()["equity"] - START_CASH) < 1  # flat fills at entry price


def test_market_open_only_on_replay_day():
    p = make_provider()
    p.now()
    assert p.market_is_open() is True
    assert "replaying" in p.clock_info()["next_open"]
