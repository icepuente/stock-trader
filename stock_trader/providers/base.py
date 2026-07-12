"""Provider interface: market data + simulated order execution.

Implementations must be paper/demo only — there is deliberately no way to
route a live order through this codebase.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")


class Provider(ABC):
    name: str            # "alpaca" | "bitget" | "ibkr" | "sim"
    mode_label: str      # "PAPER" | "DEMO" | "SIM" — shown in the dashboard header

    def now(self) -> datetime:
        """Current New York time. The sim provider overrides this with an
        accelerated replay clock; everything downstream (session gating,
        candle-close detection) flows from it."""
        return datetime.now(NY)

    # ------------------------------------------------------------- market --
    @abstractmethod
    def market_is_open(self) -> bool:
        """Whether the venue is currently trading (crypto: always True)."""

    @abstractmethod
    def clock_info(self) -> dict:
        """{"is_open": bool, "next_open": str, "next_close": str}"""

    @abstractmethod
    def scan(self) -> list[str]:
        """Symbols whose volume pace beats their daily average by the
        configured margin (the strategy's > 8% relative-volume filter)."""

    @abstractmethod
    def hourly_bars(self, symbol: str) -> pd.DataFrame:
        """Session hourly candles (open/high/low/close/volume/candle_no),
        several days deep, indexed by New York bucket-start time."""

    def quote_sizes(self, symbol: str) -> tuple[float, float] | None:
        """Current (bid_size, ask_size) from the order book, in shares/units.
        None when the venue can't provide it — the rule-10 quote-size check
        is then skipped."""
        return None

    # ------------------------------------------------------------ account --
    @abstractmethod
    def account(self) -> dict:
        """{"equity", "cash", "buying_power", "day_pl" (may be None)}"""

    @abstractmethod
    def positions(self) -> list[dict]:
        """[{"symbol", "qty", "avg_entry", "price", "market_value",
             "unrealized_pl", "unrealized_plpc"}]"""

    def position_qty(self, symbol: str) -> float:
        for p in self.positions():
            if p["symbol"] == symbol:
                return p["qty"]
        return 0.0

    def unrealized_plpc(self, symbol: str) -> float | None:
        for p in self.positions():
            if p["symbol"] == symbol:
                return p["unrealized_plpc"]
        return None

    # ------------------------------------------------------------- orders --
    @abstractmethod
    def buy_fraction(self, symbol: str, fraction: float, price: float, reason: str) -> None:
        """Market-buy `fraction` of the per-symbol dollar allocation."""

    @abstractmethod
    def sell_fraction(self, symbol: str, fraction: float, reason: str) -> None:
        """Market-sell `fraction` of the currently held position."""

    @abstractmethod
    def flatten_all(self) -> None:
        """Close every open position."""
