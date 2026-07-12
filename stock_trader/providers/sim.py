"""Simulation provider: replays the most recent trading day at high speed.

Real 5-minute US stock data is downloaded once via yfinance (no account
needed), then a virtual clock starts at 09:25 ET of the last completed
trading day and advances at `sim_speed`× real time (default 60× — the whole
session plays out in about seven minutes). The strategy, scanner, dashboard
and tiered execution all run exactly as they would live; orders fill
instantly at the current bar's close against an in-memory $100k portfolio.
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import Settings
from ..data import aggregate_hourly
from .base import Provider

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

START_CASH = 100_000.0

# Demo-day price path: (minutes since 9:30, price). Linear between anchors.
# Shaped so, on hour-anchored candles: candle 1 is a modest green half-hour,
# candle 2 a full-body green surge on triple volume (gate + tiers A/B), the
# rise continues into candle 3 (tier C), momentum plateaus around 13:00-14:00
# and rolls over (MACD-plateau 70% exit), then the afternoon dives hard enough
# to trip the -3% hard stop on the remainder before the 15:55 flatten.
_DEMO_PATH = [(0, 100.0), (30, 100.5), (90, 103.5), (150, 105.8),
              (210, 107.0), (270, 106.6), (330, 106.0), (390, 99.0)]
# Volume per 5-minute bar for the same segments: builds through the morning,
# dries up in the afternoon (the exit rules want falling volume). Sized so
# the warm-up average daily volume clears the rule-10 floor of 20M shares.
_DEMO_VOL = [(30, 400_000), (90, 600_000), (150, 700_000), (210, 750_000),
             (270, 700_000), (330, 500_000), (390, 300_000)]
_WARMUP_BAR_VOL = 300_000  # 78 bars/day -> ~23.4M shares average daily volume


def _demo_price(m: float) -> float:
    for (m0, p0), (m1, p1) in zip(_DEMO_PATH, _DEMO_PATH[1:]):
        if m <= m1:
            return p0 + (p1 - p0) * (m - m0) / (m1 - m0)
    return _DEMO_PATH[-1][1]


def _demo_vol(m: float) -> int:
    for m1, v in _DEMO_VOL:
        if m < m1:
            return v
    return _DEMO_VOL[-1][1]


def _synthetic_universe() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """One symbol, DEMO: quiet warm-up days, then the crafted demo day."""
    import math

    day = datetime.now(NY).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    warmup_days = []
    d = day
    while len(warmup_days) < 12:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            warmup_days.append(d)
    warmup_days.reverse()

    rows, daily_rows = [], []
    k = 0
    for wd in warmup_days:
        t0 = datetime(wd.year, wd.month, wd.day, 9, 30, tzinfo=NY)
        day_vol = 0
        for i in range(78):  # 9:30 .. 15:55
            ts = t0 + timedelta(minutes=5 * i)
            px = 100.0 + 0.2 * math.sin(k / 7)
            rows.append((ts, px, px + 0.1, px - 0.1, px + 0.02, _WARMUP_BAR_VOL))
            day_vol += _WARMUP_BAR_VOL
            k += 1
        daily_rows.append((t0, 100.0, 100.4, 99.6, 100.0, day_vol))

    t0 = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
    for i in range(78):
        m0, m1 = 5 * i, 5 * (i + 1)
        o, c = _demo_price(m0), _demo_price(m1)
        rows.append((t0 + timedelta(minutes=m0), o,
                     max(o, c) + 0.05, min(o, c) - 0.05, c, _demo_vol(m0)))

    five = pd.DataFrame(
        [r[1:] for r in rows],
        index=pd.DatetimeIndex([r[0] for r in rows]),
        columns=["open", "high", "low", "close", "volume"],
    )
    daily = pd.DataFrame(
        [r[1:] for r in daily_rows],
        index=pd.DatetimeIndex([r[0] for r in daily_rows]),
        columns=["open", "high", "low", "close", "volume"],
    )
    return {"DEMO": five}, {"DEMO": daily}


class SimProvider(Provider):
    name = "sim"
    mode_label = "SIM"

    def __init__(
        self,
        settings: Settings,
        data: dict[str, pd.DataFrame] | None = None,
        daily: dict[str, pd.DataFrame] | None = None,
    ):
        self.s = settings
        self._data = data          # symbol -> 5m OHLCV frame (NY tz index)
        self._daily = daily        # symbol -> daily OHLCV frame
        self.sim_day: date | None = None
        self._anchor_real: float | None = None
        self._anchor_sim: datetime | None = None
        # RLock: order methods call now()/_price() which re-enter _ensure_data
        self._lock = threading.RLock()
        self.cash = START_CASH
        self.start_equity = START_CASH
        self.pos: dict[str, dict] = {}   # symbol -> {"qty": float, "avg": float}

    # --------------------------------------------------------------- data --
    def _ensure_data(self) -> None:
        with self._lock:
            if self._data is not None and self.sim_day is not None:
                return
            if self._data is None and self.s.sim_scenario == "demo":
                self._data, self._daily = _synthetic_universe()
                log.info("sim: DEMO scenario — synthetic day crafted so every "
                         "strategy stage fires")
            if self._data is None:
                import yfinance as yf

                symbols = self.s.sim_symbols
                log.info("sim: downloading 5m history for %s …", ",".join(symbols))
                five = yf.download(symbols, period="30d", interval="5m",
                                   group_by="ticker", progress=False,
                                   auto_adjust=False, prepost=False)
                daily = yf.download(symbols, period="60d", interval="1d",
                                    group_by="ticker", progress=False, auto_adjust=False)
                self._data, self._daily = {}, {}
                for sym in symbols:
                    try:
                        d = five[sym].dropna()
                    except KeyError:
                        continue
                    if d.empty:
                        continue
                    d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
                    d.index = pd.DatetimeIndex(d.index).tz_convert(NY)
                    self._data[sym] = d
                    dd = daily[sym].dropna().rename(columns=str.lower)
                    self._daily[sym] = dd
                if not self._data:
                    raise RuntimeError("sim: yfinance returned no data — try again later")
            self.sim_day = max(df.index.max() for df in self._data.values()).date()
            log.info("sim: replaying %s at %.0fx speed, %d symbols",
                     self.sim_day, self.s.sim_speed, len(self._data))

    def now(self) -> datetime:
        self._ensure_data()
        real = time.monotonic()
        if self._anchor_real is None:
            self._anchor_real = real
            self._anchor_sim = datetime(
                self.sim_day.year, self.sim_day.month, self.sim_day.day,
                9, 25, tzinfo=NY,
            )
        return self._anchor_sim + timedelta(
            seconds=(real - self._anchor_real) * self.s.sim_speed
        )

    def _price(self, symbol: str, t: datetime) -> float | None:
        df = self._data.get(symbol)
        if df is None:
            return None
        upto = df[df.index <= t]
        return float(upto["close"].iloc[-1]) if len(upto) else None

    # ------------------------------------------------------------- market --
    def market_is_open(self) -> bool:
        t = self.now()
        return t.date() == self.sim_day and t.hour < 20  # replay day only

    def clock_info(self) -> dict:
        t = self.now()
        return {
            "is_open": self.market_is_open(),
            "next_open": f"replaying {self.sim_day} at {self.s.sim_speed:.0f}x",
            "next_close": t.strftime("%a 16:00"),
        }

    def scan(self) -> list[str]:
        self._ensure_data()
        t = self.now()
        session_minutes = 390
        elapsed = min(
            max((t - t.replace(hour=9, minute=30, second=0)).total_seconds() / 60, 1),
            session_minutes,
        )
        pace = elapsed / session_minutes
        picks: list[tuple[str, float]] = []
        for sym, df in self._data.items():
            day = df[(pd.Index(df.index.date) == self.sim_day) & (df.index <= t)]
            if day.empty:
                continue
            dd = self._daily.get(sym)
            if dd is None or len(dd) < 3:
                continue
            past = dd[pd.Index(dd.index.date) < self.sim_day]
            adv = past["volume"].tail(self.s.adv_lookback_days).mean()
            if not adv or adv <= 0 or adv < self.s.adv_min_shares:  # rule 10
                continue
            rel = day["volume"].sum() / (adv * pace)
            if rel > self.s.relative_volume_min:
                picks.append((sym, rel))
        picks.sort(key=lambda p: p[1], reverse=True)
        chosen = [sym for sym, _ in picks[: self.s.max_watchlist]]
        log.info("scanner: %d/%d symbols above %.0f%% relative volume: %s",
                 len(picks), len(self._data), (self.s.relative_volume_min - 1) * 100, chosen)
        return chosen

    def hourly_bars(self, symbol: str) -> pd.DataFrame:
        self._ensure_data()
        df = self._data.get(symbol)
        if df is None:
            return pd.DataFrame()
        return aggregate_hourly(df[df.index <= self.now()], anchor=self.s.bar_anchor)

    def quote_sizes(self, symbol: str) -> tuple[float, float] | None:
        return (5_000.0, 5_000.0)  # calm book — passes the rule-10 cap

    # ------------------------------------------------------------ account --
    def account(self) -> dict:
        self._ensure_data()
        t = self.now()
        mv = sum(
            (self._price(sym, t) or p["avg"]) * p["qty"] for sym, p in self.pos.items()
        )
        equity = self.cash + mv
        return {
            "equity": equity,
            "cash": self.cash,
            "buying_power": self.cash,
            "day_pl": equity - self.start_equity,
        }

    def positions(self) -> list[dict]:
        self._ensure_data()
        t = self.now()
        out = []
        for sym, p in self.pos.items():
            if p["qty"] <= 0:
                continue
            price = self._price(sym, t) or p["avg"]
            out.append({
                "symbol": sym,
                "qty": p["qty"],
                "avg_entry": p["avg"],
                "price": price,
                "market_value": price * p["qty"],
                "unrealized_pl": (price - p["avg"]) * p["qty"],
                "unrealized_plpc": (price - p["avg"]) / p["avg"] if p["avg"] else 0.0,
            })
        return out

    # ------------------------------------------------------------- orders --
    def buy_fraction(self, symbol: str, fraction: float, price: float, reason: str) -> None:
        qty = int(self.s.allocation() * fraction / price)
        if qty < 1:
            log.warning("%s: buy skipped, allocation fraction too small", symbol)
            return
        fill = self._price(symbol, self.now()) or price
        with self._lock:
            cost = qty * fill
            if cost > self.cash:
                log.warning("%s: buy skipped, insufficient sim cash", symbol)
                return
            p = self.pos.setdefault(symbol, {"qty": 0.0, "avg": 0.0})
            p["avg"] = (p["avg"] * p["qty"] + cost) / (p["qty"] + qty)
            p["qty"] += qty
            self.cash -= cost
        log.info("SIM BUY %s x%d @ %.2f (~%.0f%% of allocation) — %s",
                 symbol, qty, fill, fraction * 100, reason)

    def sell_fraction(self, symbol: str, fraction: float, reason: str) -> None:
        t = self.now()
        with self._lock:
            p = self.pos.get(symbol)
            if not p or p["qty"] <= 0:
                return
            qty = int(p["qty"]) if fraction >= 1.0 else max(int(p["qty"] * fraction), 1)
            fill = self._price(symbol, t) or p["avg"]
            p["qty"] -= qty
            self.cash += qty * fill
            if p["qty"] <= 0:
                del self.pos[symbol]
        log.info("SIM SELL %s x%d @ %.2f (%.0f%% of position) — %s",
                 symbol, qty, fill, fraction * 100, reason)

    def flatten_all(self) -> None:
        for sym in list(self.pos):
            self.sell_fraction(sym, 1.0, "flatten")
