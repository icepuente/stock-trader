"""Indicator math and candlestick helpers. Pure pandas, no API calls."""

import pandas as pd


def add_indicators(bars: pd.DataFrame, vmar_period: int = 20) -> pd.DataFrame:
    """Add ema9/ema20, MACD(12,26,9) histogram, and VMAR columns.

    `bars` must have open/high/low/close/volume columns and be in
    chronological order. Indicators are computed across the whole series,
    so pass multi-day history for a meaningful warm-up.
    """
    out = bars.copy()
    close = out["close"]

    out["ema9"] = close.ewm(span=9, adjust=False).mean()
    out["ema20"] = close.ewm(span=20, adjust=False).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = macd - signal

    vol_sma = out["volume"].rolling(vmar_period, min_periods=1).mean()
    out["vmar"] = out["volume"] / vol_sma

    return out


def is_green(bar: pd.Series) -> bool:
    return bar["close"] > bar["open"]


def is_full_body(bar: pd.Series, min_ratio: float) -> bool:
    """Body covers at least `min_ratio` of the candle's high-low range."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False
    return abs(bar["close"] - bar["open"]) / rng >= min_ratio


def hist_green_and_rising(df: pd.DataFrame, i: int) -> bool:
    """MACD histogram positive and larger than the previous bar's."""
    if i < 1:
        return False
    h, prev = df["macd_hist"].iloc[i], df["macd_hist"].iloc[i - 1]
    return h > 0 and h > prev


def hist_plateau_then_drop(df: pd.DataFrame, i: int, tolerance: float) -> bool:
    """Green histogram stalled (two similar bars) and the current bar is lower."""
    if i < 2:
        return False
    h2, h1, h0 = df["macd_hist"].iloc[i - 2 : i + 1]
    if h1 <= 0:
        return False
    plateaued = abs(h1 - h2) <= tolerance * max(abs(h2), 1e-9)
    return plateaued and h0 < h1


def trend_up(df: pd.DataFrame, i: int) -> bool:
    row = df.iloc[i]
    return row["ema9"] > row["ema20"] and row["close"] > row["ema9"]


def trend_down(df: pd.DataFrame, i: int) -> bool:
    row = df.iloc[i]
    return row["ema9"] < row["ema20"] or row["close"] < row["ema20"]
