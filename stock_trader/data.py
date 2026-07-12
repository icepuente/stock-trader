"""Shared bar-aggregation helpers used by all providers."""

from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")


def aggregate_hourly(five_min: pd.DataFrame, anchor: str = "open") -> pd.DataFrame:
    """Aggregate 5-minute bars into New York-session hourly candles.

    anchor="open": buckets start at 9:30 (candle 1 = 9:30-10:30).
    anchor="hour": buckets start on the hour (candle 1 = 9:30-10:00 partial).
    Returns a frame indexed by bucket start time with a `candle_no` column
    (1-based position within its trading day).
    """
    t = five_min.index
    rth = five_min[
        ((t.hour > 9) | ((t.hour == 9) & (t.minute >= 30))) & (t.hour < 16)
    ]
    if rth.empty:
        return pd.DataFrame()

    idx = rth.index
    if anchor == "open":
        minutes = (idx.hour - 9) * 60 + (idx.minute - 30)
        bucket_start = idx - pd.to_timedelta(minutes % 60, unit="m")
    else:
        bucket_start = idx.floor("h")
        # first bucket of the day actually begins at 9:30
        bucket_start = bucket_start.where(
            ~((idx.hour == 9)), bucket_start + pd.Timedelta(minutes=30)
        )

    g = rth.groupby(bucket_start)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    ).sort_index()
    out["candle_no"] = out.groupby(out.index.date).cumcount() + 1
    return out
