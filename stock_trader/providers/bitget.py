"""Bitget provider: crypto, DEMO trading only (Bitget's simulated futures).

Bitget's demo environment is USDT-margined futures on S-prefixed symbols
(SBTC/SUSDT, SETH/SUSDT, ...) traded with demo funds. ccxt's sandbox mode
sends the `paptrading` header so every request stays inside demo trading —
real funds are never touched, and there is no switch to change that.

Crypto trades 24/7, but the strategy is defined on the New York session, so
bars are still filtered to 9:30–16:00 ET and the bot only acts inside that
window — every day of the week, since there is no exchange calendar.
"""

import functools
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd

from ..config import Settings
from ..data import aggregate_hourly
from .base import Provider

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

_HINTS = {
    "40099": "Bitget rejected the demo-trading request (40099: wrong environment). "
             "These API keys are production keys — switch bitget.com to Demo Trading "
             "mode, create an API key *inside* the demo environment, and put those "
             "credentials in .env.",
    "40085": "Bitget says the account is in Unified Account mode (40085). "
             "Set BITGET_UTA=1 in .env and restart.",
}


def _with_hints(fn):
    """Re-raise known Bitget API errors with an actionable message."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ccxt.BaseError as e:
            for code, hint in _HINTS.items():
                if code in str(e):
                    raise RuntimeError(hint) from e
            raise
    return wrapper


class BitgetProvider(Provider):
    name = "bitget"
    mode_label = "DEMO"

    def __init__(self, settings: Settings):
        self.s = settings
        self.x = ccxt.bitget({
            "apiKey": settings.bitget_api_key,
            "secret": settings.bitget_secret_key,
            "password": settings.bitget_passphrase,
            "options": {"defaultType": "swap", "uta": settings.bitget_uta},
        })
        self.x.set_sandbox_mode(True)  # demo trading — mandatory, never disabled
        self._markets_loaded = False

    def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            self.x.load_markets()
            self._markets_loaded = True
            missing = [s for s in self.s.bitget_symbols if s not in self.x.markets]
            if missing:
                log.warning("bitget demo does not list %s — check BITGET_SYMBOLS", missing)

    def _universe(self) -> list[str]:
        """Configured symbols, or every USDT-margined future demo offers."""
        self._ensure_markets()
        if self.s.bitget_symbols:
            return [s for s in self.s.bitget_symbols if s in self.x.markets]
        return [
            s for s, m in self.x.markets.items()
            if m.get("swap") and m.get("quote") in ("USDT", "SUSDT") and m.get("active", True)
        ]

    # ------------------------------------------------------------- market --
    def market_is_open(self) -> bool:
        return True  # crypto never closes; the session window still gates trades

    def clock_info(self) -> dict:
        return {"is_open": True, "next_open": "24/7", "next_close": "24/7"}

    @_with_hints
    def scan(self) -> list[str]:
        """Same relative-volume rule as stocks, against the UTC daily candle.
        The rule-10 ADV > 20M *shares* floor is not applied here — coin
        volumes aren't comparable to share counts."""
        now = datetime.now(timezone.utc)
        pace = max((now.hour * 3600 + now.minute * 60) / 86_400, 0.02)
        picks: list[tuple[str, float]] = []
        for sym in self._universe():
            try:
                daily = self.x.fetch_ohlcv(sym, "1d", limit=self.s.adv_lookback_days + 2)
            except Exception:
                log.exception("bitget: daily candles failed for %s", sym)
                continue
            if len(daily) < 3:
                continue
            vols = [c[5] for c in daily]
            adv = sum(vols[:-1][-self.s.adv_lookback_days:]) / len(vols[:-1][-self.s.adv_lookback_days:])
            if adv <= 0:
                continue
            rel = vols[-1] / (adv * pace)
            if rel > self.s.relative_volume_min:
                picks.append((sym, rel))
        picks.sort(key=lambda p: p[1], reverse=True)
        chosen = [sym for sym, _ in picks[: self.s.max_watchlist]]
        log.info("scanner: %d/%d demo pairs above %.0f%% relative volume: %s",
                 len(picks), len(self._universe()),
                 (self.s.relative_volume_min - 1) * 100, chosen)
        return chosen

    @_with_hints
    def quote_sizes(self, symbol: str) -> tuple[float, float] | None:
        """Top-of-book sizes in base units. The 20k-share default threshold
        maps poorly to crypto — tune quote_size_max if it blocks everything."""
        self._ensure_markets()
        book = self.x.fetch_order_book(symbol, limit=5)
        if not book.get("bids") or not book.get("asks"):
            return None
        return float(book["bids"][0][1]), float(book["asks"][0][1])

    @_with_hints
    def hourly_bars(self, symbol: str) -> pd.DataFrame:
        """Paginate 5m candles over the history window, then reuse the shared
        NY-session hourly aggregation so the strategy sees identical shapes."""
        self._ensure_markets()
        since = int((datetime.now(timezone.utc)
                     - timedelta(days=self.s.intraday_history_days)).timestamp() * 1000)
        rows: list[list] = []
        while True:
            batch = self.x.fetch_ohlcv(symbol, "5m", since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 1000:
                break
            since = batch[-1][0] + 1
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(NY)
        df = df.drop(columns="ts")
        return aggregate_hourly(df, anchor=self.s.bar_anchor)

    # ------------------------------------------------------------ account --
    @_with_hints
    def account(self) -> dict:
        bal = self.x.fetch_balance()
        usdt = bal.get("SUSDT") or bal.get("USDT") or {}
        total = float(usdt.get("total") or 0)
        free = float(usdt.get("free") or 0)
        return {"equity": total, "cash": free, "buying_power": free, "day_pl": None}

    @_with_hints
    def positions(self) -> list[dict]:
        out = []
        for p in self.x.fetch_positions(self._universe()):
            qty = float(p.get("contracts") or 0)
            if qty == 0:
                continue
            out.append({
                "symbol": p["symbol"],
                "qty": qty,
                "avg_entry": float(p.get("entryPrice") or 0),
                "price": float(p.get("markPrice") or 0),
                "market_value": float(p.get("notional") or 0),
                "unrealized_pl": float(p.get("unrealizedPnl") or 0),
                "unrealized_plpc": float(p.get("percentage") or 0) / 100,
            })
        return out

    # ------------------------------------------------------------- orders --
    @_with_hints
    def buy_fraction(self, symbol: str, fraction: float, price: float, reason: str) -> None:
        self._ensure_markets()
        amount = float(self.x.amount_to_precision(
            symbol, self.s.allocation() * fraction / price
        ))
        if amount <= 0:
            log.warning("%s: buy skipped, allocation fraction too small at %.2f", symbol, price)
            return
        log.info("BUY %s x%s (~%.0f%% of allocation) — %s", symbol, amount, fraction * 100, reason)
        self.x.create_order(symbol, "market", "buy", amount)

    @_with_hints
    def sell_fraction(self, symbol: str, fraction: float, reason: str) -> None:
        held = self.position_qty(symbol)
        if held <= 0:
            return
        amount = held if fraction >= 1.0 else float(
            self.x.amount_to_precision(symbol, held * fraction)
        )
        if amount <= 0:
            return
        log.info("SELL %s x%s (%.0f%% of position) — %s", symbol, amount, fraction * 100, reason)
        self.x.create_order(symbol, "market", "sell", amount, params={"reduceOnly": True})

    @_with_hints
    def flatten_all(self) -> None:
        log.info("closing all open demo positions")
        for p in self.positions():
            try:
                self.x.create_order(
                    p["symbol"], "market", "sell", p["qty"], params={"reduceOnly": True}
                )
            except Exception:
                log.exception("failed to close %s", p["symbol"])
