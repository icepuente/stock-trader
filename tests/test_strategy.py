"""Offline tests: indicator math, hourly aggregation, and the tier/exit state
machine — driven by synthetic bars, no API keys required."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from stock_trader.config import Settings
from stock_trader.data import aggregate_hourly
from stock_trader.indicators import add_indicators, is_full_body, is_green
from stock_trader.strategy import SymbolState, evaluate

NY = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 10, tzinfo=NY)  # a Friday


def ts(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


# ---------------------------------------------------------------- helpers --
def make_bars(rows):
    """rows: list of (hour, minute, open, high, low, close, volume, candle_no)."""
    idx = [ts(h, m) for h, m, *_ in rows]
    df = pd.DataFrame(
        [r[2:] for r in rows],
        index=pd.DatetimeIndex(idx),
        columns=["open", "high", "low", "close", "volume", "candle_no"],
    )
    return df


def with_signals(bars, ema9=None, ema20=None, hist=None, vmar=None):
    """Attach hand-crafted indicator columns to drive the state machine."""
    out = bars.copy()
    n = len(out)
    out["ema9"] = ema9 if ema9 is not None else out["close"] * 0.99
    out["ema20"] = ema20 if ema20 is not None else out["close"] * 0.97
    out["macd_hist"] = hist if hist is not None else [0.1 * (k + 1) for k in range(n)]
    out["vmar"] = vmar if vmar is not None else 1.5
    return out


def base_day():
    """Candle 1 (9:30) red-ish, candle 2 (10:30) full-body green on higher
    volume, candle 3 (11:30) forming. Evaluated at 12:00 -> candles 1-2 closed."""
    return make_bars([
        (9, 30, 100, 101, 99, 100.2, 50_000, 1),
        (10, 30, 100.2, 103.2, 100.0, 103.0, 80_000, 2),
        (11, 30, 103.0, 104, 102.8, 103.5, 30_000, 3),
    ])


NOON = ts(12, 0)
SETTINGS = Settings(api_key="x", secret_key="x")


# ------------------------------------------------------------- indicators --
def test_candle_helpers():
    green = pd.Series({"open": 10, "high": 11, "low": 9.9, "close": 10.9})
    assert is_green(green)
    assert is_full_body(green, 0.7)
    doji = pd.Series({"open": 10, "high": 11, "low": 9, "close": 10.05})
    assert not is_full_body(doji, 0.7)


def test_add_indicators_columns_and_sanity():
    n = 60
    df = pd.DataFrame({
        "open": range(100, 100 + n), "high": range(101, 101 + n),
        "low": range(99, 99 + n), "close": range(100, 100 + n),
        "volume": [10_000] * n,
    })
    out = add_indicators(df)
    # steadily rising close -> fast EMA above slow, positive MACD histogram
    assert out["ema9"].iloc[-1] > out["ema20"].iloc[-1]
    assert out["macd_hist"].iloc[-1] > 0
    assert out["vmar"].iloc[-1] == pytest.approx(1.0)


def test_aggregate_hourly_open_anchor():
    idx = pd.date_range(ts(9, 30), ts(15, 55), freq="5min", tz=NY)
    five = pd.DataFrame({
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
    }, index=idx)
    out = aggregate_hourly(five, anchor="open")
    assert list(out["candle_no"]) == [1, 2, 3, 4, 5, 6, 7]
    assert out.index[0].time() == time(9, 30)
    assert out.index[1].time() == time(10, 30)
    assert out["volume"].iloc[0] == 1200  # 12 five-minute bars

    hourly = aggregate_hourly(five, anchor="hour")
    assert hourly.index[0].time() == time(9, 30)   # partial opening bucket
    assert hourly.index[1].time() == time(10, 0)
    assert hourly["volume"].iloc[0] == 600         # only 30 minutes


# ---------------------------------------------------------------- entries --
def test_gate_blocks_red_candle2():
    bars = base_day()
    bars.loc[ts(10, 30), "close"] = 99.0  # candle 2 red
    st = SymbolState()
    actions = evaluate(with_signals(bars), st, SETTINGS, NOON, has_position=False)
    assert actions == [] and not st.gate_passed


def test_gate_blocks_low_volume_candle2():
    bars = base_day()
    bars.loc[ts(10, 30), "volume"] = 10_000  # below candle 1
    st = SymbolState()
    evaluate(with_signals(bars), st, SETTINGS, NOON, has_position=False)
    assert not st.gate_passed


def test_tier_b_fires_on_full_body_green_candle2():
    st = SymbolState()
    actions = evaluate(with_signals(base_day()), st, SETTINGS, NOON, has_position=False)
    buys = [a for a in actions if a.side == "buy"]
    assert st.gate_passed
    assert any(a.fraction == SETTINGS.tier_b_pct for a in buys)
    # tier C also fires: candle 3 started, histogram rising, volume up on last
    # closed bar (candle 2 vs 1)
    assert any(a.fraction == SETTINGS.tier_c_pct for a in buys)
    # and never twice
    again = evaluate(with_signals(base_day()), st, SETTINGS, NOON, has_position=True)
    assert [a for a in again if a.side == "buy"] == []


def test_validation_filter_blocks_buys():
    st = SymbolState()
    weak_vmar = with_signals(base_day(), vmar=0.5)
    actions = evaluate(weak_vmar, st, SETTINGS, NOON, has_position=False)
    assert [a for a in actions if a.side == "buy"] == []
    assert st.gate_passed and st.tiers_filled == set()

    st2 = SymbolState()
    downtrend = with_signals(base_day(), ema9=[90.0] * 3, ema20=[95.0] * 3)
    actions = evaluate(downtrend, st2, SETTINGS, NOON, has_position=False)
    assert [a for a in actions if a.side == "buy"] == []


def test_rule10_quote_size_blocks_buys():
    st = SymbolState()
    actions = evaluate(with_signals(base_day()), st, SETTINGS, NOON,
                       has_position=False, quote_sizes=(50_000.0, 5_000.0))
    assert st.gate_passed
    assert [a for a in actions if a.side == "buy"] == []
    # a calm book passes
    st2 = SymbolState()
    actions = evaluate(with_signals(base_day()), st2, SETTINGS, NOON,
                       has_position=False, quote_sizes=(5_000.0, 5_000.0))
    assert any(a.side == "buy" for a in actions)


def test_rule12_fake_dip_on_candle_3_suppresses_stop():
    bars = make_bars([
        (9, 30, 100, 101, 99, 100.5, 90_000, 1),
        (10, 30, 100.5, 102, 100, 101.5, 95_000, 2),
        (11, 30, 101.5, 102, 98, 98.5, 40_000, 3),  # candle 3: volume dip
    ])
    # reversal conditions met, but histogram still green on candle 3 -> hold
    sig = with_signals(bars, ema9=[100.0, 101.0, 99.0], ema20=[100.5, 100.8, 99.5],
                       hist=[0.5, 0.8, 0.4])
    st = SymbolState()
    actions = evaluate(sig, st, SETTINGS, ts(12, 30), has_position=True)
    assert all(a.reason != "trend reversal stop" for a in actions)
    assert not st.done_for_day

    # same shape but histogram already negative -> stop fires
    sig2 = with_signals(bars, ema9=[100.0, 101.0, 99.0], ema20=[100.5, 100.8, 99.5],
                        hist=[0.5, 0.8, -0.2])
    st2 = SymbolState()
    actions = evaluate(sig2, st2, SETTINGS, ts(12, 30), has_position=True)
    assert any(a.reason == "trend reversal stop" for a in actions)
    assert st2.done_for_day


# ------------------------------------------------------------------ exits --
def test_partial_exit_on_histogram_plateau_then_drop():
    bars = make_bars([
        (9, 30, 100, 101, 99, 100.5, 50_000, 1),
        (10, 30, 100.5, 102, 100, 101.5, 80_000, 2),
        (11, 30, 101.5, 102, 100, 101.0, 90_000, 3),
        (12, 30, 101.0, 102, 100, 101.2, 95_000, 4),
    ])
    # histogram: rises, plateaus, then drops on the last closed bar
    sig = with_signals(bars, hist=[0.5, 1.0, 1.02, 0.6])
    st = SymbolState()
    st.gate_passed = True
    st.tiers_filled = {"A", "B", "C"}
    actions = evaluate(sig, st, SETTINGS, ts(13, 30), has_position=True)
    sells = [a for a in actions if a.side == "sell"]
    assert len(sells) == 1 and sells[0].fraction == SETTINGS.partial_exit_pct
    assert st.partial_exit_done


def test_reversal_stop_sells_everything():
    bars = make_bars([
        (9, 30, 100, 101, 99, 100.5, 90_000, 1),
        (10, 30, 100.5, 101, 98, 98.5, 40_000, 2),  # volume drop, close weak
    ])
    sig = with_signals(bars, ema9=[100.0, 99.0], ema20=[100.5, 99.5],
                       hist=[0.5, -0.2])
    st = SymbolState()
    actions = evaluate(sig, st, SETTINGS, ts(11, 30), has_position=True)
    assert any(a.side == "sell" and a.fraction == 1.0 for a in actions)
    assert st.done_for_day


def test_flatten_at_session_end():
    st = SymbolState()
    actions = evaluate(with_signals(base_day()), st, SETTINGS, ts(15, 56),
                       has_position=True)
    assert actions == [
        a for a in actions if a.side == "sell" and a.fraction == 1.0
    ] and len(actions) == 1


def test_state_resets_on_new_day():
    st = SymbolState()
    st.gate_passed = True
    st.tiers_filled = {"B"}
    st.day = DAY.date()
    next_day = NOON.replace(day=NOON.day + 1)
    evaluate(pd.DataFrame(), st, SETTINGS, next_day, has_position=False)
    assert not st.gate_passed and st.tiers_filled == set()
