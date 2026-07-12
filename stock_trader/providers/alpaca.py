"""Alpaca provider: US shares, PAPER account only.

The TradingClient is constructed with paper=True and there is no switch to
change that.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (
    MostActivesRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from ..config import Settings
from ..data import aggregate_hourly
from .base import Provider

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class AlpacaProvider(Provider):
    name = "alpaca"
    mode_label = "PAPER"

    def __init__(self, settings: Settings):
        self.s = settings
        self.hist = StockHistoricalDataClient(settings.api_key, settings.secret_key)
        self.screener = ScreenerClient(settings.api_key, settings.secret_key)
        self.trading = TradingClient(settings.api_key, settings.secret_key, paper=True)

    # ------------------------------------------------------------- market --
    def market_is_open(self) -> bool:
        return self.trading.get_clock().is_open

    def clock_info(self) -> dict:
        clock = self.trading.get_clock()
        return {
            "is_open": clock.is_open,
            "next_open": clock.next_open.astimezone(NY).strftime("%a %H:%M"),
            "next_close": clock.next_close.astimezone(NY).strftime("%a %H:%M"),
        }

    def scan(self) -> list[str]:
        actives = self.screener.get_most_actives(
            MostActivesRequest(by="volume", top=self.s.scanner_top_n)
        )
        symbols = [a.symbol for a in actives.most_actives]
        if not symbols:
            return []

        now = datetime.now(NY)
        start = now - timedelta(days=self.s.adv_lookback_days * 2)
        daily = self.hist.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                feed=DataFeed.IEX,
            )
        ).df

        # Fraction of the trading session elapsed, to compare volume *pace*
        # intraday instead of penalising the morning hours.
        session_minutes = 390
        elapsed = min(
            max((now - now.replace(hour=9, minute=30, second=0)).total_seconds() / 60, 1),
            session_minutes,
        )
        pace = elapsed / session_minutes

        picks: list[tuple[str, float]] = []
        for sym in symbols:
            try:
                d = daily.loc[sym]
            except KeyError:
                continue
            if len(d) < 2:
                continue
            adv = d["volume"].iloc[:-1].tail(self.s.adv_lookback_days).mean()
            today_vol = d["volume"].iloc[-1]
            if adv <= 0 or adv < self.s.adv_min_shares:  # rule 10: ADV > 20M
                continue
            rel = today_vol / (adv * pace)
            if rel > self.s.relative_volume_min:
                picks.append((sym, rel))

        picks.sort(key=lambda p: p[1], reverse=True)
        chosen = [sym for sym, _ in picks[: self.s.max_watchlist]]
        log.info("scanner: %d/%d symbols above %.0f%% relative volume: %s",
                 len(picks), len(symbols), (self.s.relative_volume_min - 1) * 100, chosen)
        return chosen

    def hourly_bars(self, symbol: str) -> pd.DataFrame:
        start = datetime.now(NY) - timedelta(days=self.s.intraday_history_days)
        raw = self.hist.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start,
                feed=DataFeed.IEX,
            )
        ).df
        if raw.empty:
            return pd.DataFrame()
        df = raw.reset_index(level=0, drop=True) if isinstance(raw.index, pd.MultiIndex) else raw
        df = df.tz_convert(NY)
        return aggregate_hourly(df, anchor=self.s.bar_anchor)

    def quote_sizes(self, symbol: str) -> tuple[float, float] | None:
        # IEX feed quote sizes are round lots — convert to shares.
        q = self.hist.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )[symbol]
        return float(q.bid_size) * 100, float(q.ask_size) * 100

    # ------------------------------------------------------------ account --
    def account(self) -> dict:
        acct = self.trading.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "day_pl": float(acct.equity) - float(acct.last_equity),
        }

    def positions(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            }
            for p in self.trading.get_all_positions()
        ]

    # ------------------------------------------------------------- orders --
    def buy_fraction(self, symbol: str, fraction: float, price: float, reason: str) -> None:
        qty = int(self.s.allocation() * fraction / price)
        if qty < 1:
            log.warning("%s: buy skipped, allocation fraction too small at $%.2f", symbol, price)
            return
        log.info("BUY %s x%d (~%.0f%% of allocation) — %s", symbol, qty, fraction * 100, reason)
        self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
            )
        )

    def sell_fraction(self, symbol: str, fraction: float, reason: str) -> None:
        held = self.position_qty(symbol)
        if held <= 0:
            return
        if fraction >= 1.0:
            log.info("SELL %s ALL (%d) — %s", symbol, int(held), reason)
            self.trading.close_position(symbol)
            return
        qty = max(int(held * fraction), 1)
        log.info("SELL %s x%d (%.0f%% of position) — %s", symbol, qty, fraction * 100, reason)
        self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
            )
        )

    def flatten_all(self) -> None:
        log.info("closing all open paper positions")
        self.trading.close_all_positions(cancel_orders=True)
