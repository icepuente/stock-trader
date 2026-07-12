"""Interactive Brokers provider: US shares via a locally running IB Gateway
or TWS logged into a PAPER account.

Paper-only enforcement: after connecting, the managed account codes are
checked — IBKR paper accounts start with "D" (DU/DF). If a live account is
detected the provider disconnects and refuses to operate. There is no
override.

ib_async is asyncio-based while the bot loop and API endpoints call from
several threads, so all IB traffic is funnelled onto one dedicated event-loop
thread via run_coroutine_threadsafe.

Pacing: IBKR allows roughly 60 historical-data requests per 10 minutes, so
hourly bars are cached per symbol (default 240s) and the scanner universe is
capped — do not poll harder than the defaults without checking those limits.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, MarketOrder, ScannerSubscription, Stock, util

from ..config import Settings
from ..data import aggregate_hourly
from .base import Provider

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

_GATEWAY_HINT = (
    "Could not reach IB Gateway/TWS at {host}:{port}. Start IB Gateway, log in "
    "with your PAPER account (username starts with DU), and enable "
    "'ActiveX and Socket Clients' in Configuration → API → Settings "
    "(paper ports: 4002 Gateway, 7497 TWS)."
)

_SCANNER_CAP = 25  # daily-bar lookups per rescan; keeps well under pacing limits


def verify_accounts(accounts: list[str], allow_live: bool = False) -> str:
    """Classify the gateway's managed accounts and return "PAPER" or "LIVE".

    IBKR paper account codes start with 'D' (DU…/DF…). A live login is
    refused unless LIVE_TRADING=1 was set in the environment — there is no
    other way to reach a live account."""
    if not accounts:
        raise RuntimeError(
            "IB Gateway reported no managed accounts — finish logging in and retry."
        )
    live = [a for a in accounts if a and not a.upper().startswith("D")]
    if live and not allow_live:
        raise RuntimeError(
            f"IB Gateway is logged into a LIVE account ({live}) — live trading "
            "is disabled. Log the gateway into your paper account (username "
            "starts with DU), or explicitly opt in with LIVE_TRADING=1 in .env "
            "(real money!) and restart."
        )
    return "LIVE" if live else "PAPER"


def assert_paper_accounts(accounts: list[str]) -> None:
    """Paper-only guard (kept for callers that never allow live)."""
    verify_accounts(accounts, allow_live=False)


class IBKRProvider(Provider):
    name = "ibkr"
    mode_label = "PAPER"

    def __init__(self, settings: Settings):
        self.s = settings
        self.ib = IB()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="ib-loop", daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._bars_cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float = 90):
        """Run a coroutine on the IB thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self) -> None:
        if self.ib.isConnected():
            return
        try:
            await self.ib.connectAsync(
                self.s.ibkr_host, self.s.ibkr_port, clientId=self.s.ibkr_client_id
            )
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            raise RuntimeError(
                _GATEWAY_HINT.format(host=self.s.ibkr_host, port=self.s.ibkr_port)
            ) from e
        accounts = self.ib.managedAccounts()
        try:
            self.mode_label = verify_accounts(accounts, allow_live=self.s.live_trading)
        except RuntimeError:
            self.ib.disconnect()
            raise
        # Delayed quotes are fine if there's no market-data subscription;
        # historical bars (what the strategy runs on) come through regardless.
        self.ib.reqMarketDataType(3)
        if self.mode_label == "LIVE":
            log.warning("connected to IB Gateway in LIVE mode — REAL MONEY. "
                        "account(s): %s, allocation $%.0f/symbol, daily loss "
                        "halt at $%.0f", accounts, self.s.live_allocation_usd,
                        self.s.live_max_daily_loss_usd)
        else:
            log.info("connected to IB Gateway, paper account(s): %s", accounts)

    def _ensure(self) -> None:
        with self._lock:
            self._call(self._connect())

    # ------------------------------------------------------------- market --
    def market_is_open(self) -> bool:
        # Local NYSE calendar approximation (weekends only, not holidays —
        # on a holiday the scanner just returns nothing and no candles form).
        now = datetime.now(NY)
        return now.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)

    def clock_info(self) -> dict:
        self._ensure()  # so a dead gateway surfaces in the dashboard banner
        now = datetime.now(NY)
        is_open = self.market_is_open()
        nxt = now
        while True:
            nxt = nxt + timedelta(days=1)
            if nxt.weekday() < 5:
                break
        return {
            "is_open": is_open,
            "next_open": (now if now.time() < dtime(9, 30) and now.weekday() < 5
                          else nxt).strftime("%a") + " 09:30",
            "next_close": now.strftime("%a") + " 16:00" if is_open else "—",
        }

    def scan(self) -> list[str]:
        self._ensure()

        async def _scan() -> list[str]:
            sub = ScannerSubscription(
                instrument="STK", locationCode="STK.US.MAJOR", scanCode="MOST_ACTIVE",
                numberOfRows=_SCANNER_CAP,
            )
            data = await self.ib.reqScannerDataAsync(sub)
            return [d.contractDetails.contract.symbol for d in data]

        symbols = self._call(_scan())
        picks: list[tuple[str, float]] = []
        now = datetime.now(NY)
        session_minutes = 390
        elapsed = min(
            max((now - now.replace(hour=9, minute=30, second=0)).total_seconds() / 60, 1),
            session_minutes,
        )
        pace = elapsed / session_minutes

        for sym in symbols:
            try:
                daily = self._daily_bars(sym)
            except Exception:
                log.exception("ibkr: daily bars failed for %s", sym)
                continue
            if len(daily) < 3:
                continue
            # IB reports US stock volume in lots of 100 shares
            vol_shares = daily["volume"] * 100
            adv = vol_shares.iloc[:-1].tail(self.s.adv_lookback_days).mean()
            if adv <= 0 or adv < self.s.adv_min_shares:  # rule 10: ADV > 20M
                continue
            rel = vol_shares.iloc[-1] / (adv * pace)
            if rel > self.s.relative_volume_min:
                picks.append((sym, rel))

        picks.sort(key=lambda p: p[1], reverse=True)
        chosen = [sym for sym, _ in picks[: self.s.max_watchlist]]
        log.info("scanner: %d/%d symbols above %.0f%% relative volume: %s",
                 len(picks), len(symbols), (self.s.relative_volume_min - 1) * 100, chosen)
        return chosen

    def _daily_bars(self, symbol: str) -> pd.DataFrame:
        async def _fetch():
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            return await self.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="30 D",
                barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
            )
        bars = self._call(_fetch())
        return util.df(bars) if bars else pd.DataFrame()

    def hourly_bars(self, symbol: str) -> pd.DataFrame:
        cached = self._bars_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < self.s.ibkr_bars_cache_seconds:
            return cached[1]
        self._ensure()

        async def _fetch():
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            return await self.ib.reqHistoricalDataAsync(
                contract, endDateTime="",
                durationStr=f"{self.s.intraday_history_days} D",
                barSizeSetting="5 mins", whatToShow="TRADES", useRTH=True,
                formatDate=2,
            )

        bars = self._call(_fetch())
        if not bars:
            return pd.DataFrame()
        df = util.df(bars).set_index("date")[["open", "high", "low", "close", "volume"]]
        df.index = pd.DatetimeIndex(df.index).tz_convert(NY)
        out = aggregate_hourly(df, anchor=self.s.bar_anchor)
        self._bars_cache[symbol] = (time.monotonic(), out)
        return out

    # ------------------------------------------------------------ account --
    def account(self) -> dict:
        self._ensure()
        summary = self._call(self.ib.accountSummaryAsync())
        vals = {v.tag: v.value for v in summary if v.currency in ("USD", "BASE", "")}
        def f(tag):
            try:
                return float(vals.get(tag, 0) or 0)
            except ValueError:
                return 0.0
        return {
            "equity": f("NetLiquidation"),
            "cash": f("TotalCashValue"),
            "buying_power": f("BuyingPower"),
            "day_pl": None,
        }

    def positions(self) -> list[dict]:
        self._ensure()
        out = []
        for item in self.ib.portfolio():
            if item.position == 0 or item.contract.secType != "STK":
                continue
            qty = float(item.position)
            avg = float(item.averageCost)
            out.append({
                "symbol": item.contract.symbol,
                "qty": qty,
                "avg_entry": avg,
                "price": float(item.marketPrice),
                "market_value": float(item.marketValue),
                "unrealized_pl": float(item.unrealizedPNL),
                "unrealized_plpc": (float(item.marketPrice) - avg) / avg if avg else 0.0,
            })
        return out

    # ------------------------------------------------------------- orders --
    def _order(self, symbol: str, side: str, qty: int, reason: str) -> None:
        async def _place():
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            self.ib.placeOrder(contract, MarketOrder(side, qty))
        log.info("%s %s x%d — %s", side, symbol, qty, reason)
        self._call(_place())

    def buy_fraction(self, symbol: str, fraction: float, price: float, reason: str) -> None:
        self._ensure()
        qty = int(self.s.allocation() * fraction / price)
        if qty < 1:
            log.warning("%s: buy skipped, allocation fraction too small at $%.2f", symbol, price)
            return
        self._order(symbol, "BUY", qty, f"~{fraction:.0%} of allocation — {reason}")

    def sell_fraction(self, symbol: str, fraction: float, reason: str) -> None:
        self._ensure()
        held = self.position_qty(symbol)
        if held <= 0:
            return
        qty = int(held) if fraction >= 1.0 else max(int(held * fraction), 1)
        self._order(symbol, "SELL", qty, f"{fraction:.0%} of position — {reason}")

    def flatten_all(self) -> None:
        self._ensure()
        log.info("closing all open paper positions")
        for p in self.positions():
            if p["qty"] > 0:
                self._order(p["symbol"], "SELL", int(p["qty"]), "flatten")
