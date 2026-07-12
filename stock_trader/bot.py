"""Session loop: scan, evaluate, execute — during the NY session window only.

The loop is stoppable via `stop_event` so the web server can run it in a
background thread, and it keeps a bounded `activity` feed for the dashboard.
"""

import logging
import threading
from collections import deque
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .indicators import add_indicators
from .providers import get_provider
from .strategy import SymbolState, evaluate

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class Bot:
    def __init__(self, settings: Settings):
        settings.require_keys()
        self.s = settings
        self.provider = get_provider(settings)
        # the sim clock runs at sim_speed×, so poll fast enough to catch
        # candle closes (2 real seconds ≈ 2 sim minutes at 60×); don't mutate
        # the shared settings — the provider can be switched at runtime
        self.poll_seconds = (
            min(settings.poll_seconds, 2) if settings.provider == "sim"
            else settings.poll_seconds
        )
        self.states: dict[str, SymbolState] = {}
        self.watchlist: list[str] = []
        self.activity: deque[dict] = deque(maxlen=200)
        self.stop_event = threading.Event()
        self.on_event = None  # optional push hook (Telegram bridge)
        self._last_scan: datetime | None = None
        self._flattened_on: date | None = None
        self._day_start_equity: float | None = None
        self._equity_day: date | None = None
        if settings.live_trading and not settings.live_active():
            log.warning("LIVE_TRADING is set but provider %r has no live path "
                        "(only IBKR does) — orders stay paper/demo/simulated",
                        settings.provider)

    def _emit(self, text: str) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(text)
        except Exception:
            log.debug("event hook failed", exc_info=True)

    def record(self, symbol: str, side: str, detail: str) -> None:
        entry = {
            "time": self.provider.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "detail": detail,
        }
        self.activity.appendleft(entry)
        emoji = "🟢" if side == "buy" else "🔴"
        live = "⚠️ LIVE " if self.s.live_active() else ""
        self._emit(f"{live}{emoji} {side.upper()} {symbol} — {detail} [{entry['time']}]")

    # ----------------------------------------------------------------------
    def in_session(self, now: datetime) -> bool:
        if not self.provider.market_is_open():
            return False
        return self.s.session_open <= now.time() < self.s.session_close

    def maybe_rescan(self, now: datetime) -> None:
        due = (
            self._last_scan is None
            or (now - self._last_scan).total_seconds() >= self.s.rescan_minutes * 60
        )
        if not due:
            return
        self._last_scan = now
        try:
            fresh = self.provider.scan()
        except Exception:
            log.exception("scanner failed; keeping previous watchlist")
            return
        # Keep symbols we still hold even if they drop off the scanner.
        held = {p["symbol"] for p in self.provider.positions()}
        self.watchlist = list(dict.fromkeys(fresh + sorted(held)))

    # ----------------------------------------------------------------------
    def step(self, now: datetime) -> None:
        self.maybe_rescan(now)
        for symbol in self.watchlist:
            try:
                self.process_symbol(symbol, now)
            except Exception:
                log.exception("error processing %s", symbol)

    def process_symbol(self, symbol: str, now: datetime) -> None:
        state = self.states.setdefault(symbol, SymbolState())
        has_position = self.provider.position_qty(symbol) > 0

        # Hard safety stop, independent of the indicator rules.
        if has_position:
            plpc = self.provider.unrealized_plpc(symbol)
            if plpc is not None and plpc <= -self.s.hard_stop_pct:
                state.done_for_day = True
                reason = f"hard stop: {plpc:.1%} unrealized"
                self.provider.sell_fraction(symbol, 1.0, reason)
                self.record(symbol, "sell", reason)
                return

        bars = self.provider.hourly_bars(symbol)
        if bars.empty:
            return
        bars = add_indicators(bars, self.s.vmar_period)

        try:
            quote = self.provider.quote_sizes(symbol)
        except Exception:
            log.debug("quote sizes unavailable for %s", symbol)
            quote = None

        for action in evaluate(bars, state, self.s, now, has_position, quote):
            if action.side == "buy":
                if not self._buy_allowed(symbol, action.fraction):
                    continue
                notional = self.s.allocation() * action.fraction
                if self.s.live_active() and notional > self.s.live_order_cap_usd:
                    log.warning("%s: LIVE buy blocked — $%.0f exceeds the "
                                "per-order cap of $%.0f", symbol, notional,
                                self.s.live_order_cap_usd)
                    continue
                price = float(bars["close"].iloc[-1])
                self.provider.buy_fraction(symbol, action.fraction, price, action.reason)
                has_position = True
            else:
                self.provider.sell_fraction(symbol, action.fraction, action.reason)
            self.record(symbol, action.side, action.reason)

    def _buy_allowed(self, symbol: str, fraction: float) -> bool:
        """Rule 11: buys come from free cash only, spread over at most
        max_positions symbols."""
        held = {p["symbol"] for p in self.provider.positions()}
        if symbol not in held and len(held) >= self.s.max_positions:
            log.info("%s: buy skipped — already holding %d positions (max %d)",
                     symbol, len(held), self.s.max_positions)
            return False
        cash = self.provider.account()["cash"]
        needed = self.s.allocation() * fraction
        if cash < needed:
            log.info("%s: buy skipped — free cash %.0f below %.0f (rule 11: "
                     "no margin, free cash only)", symbol, cash, needed)
            return False
        return True

    def daily_loss_exceeded(self, now: datetime) -> bool:
        """Live kill switch: equity dropped live_max_daily_loss_usd below the
        day's starting equity. The first in-session reading of the day sets
        the baseline."""
        try:
            equity = float(self.provider.account()["equity"])
        except Exception:
            log.exception("daily-loss check: account unavailable")
            return False
        if self._equity_day != now.date() or self._day_start_equity is None:
            self._equity_day = now.date()
            self._day_start_equity = equity
            return False
        return equity - self._day_start_equity <= -self.s.live_max_daily_loss_usd

    # ----------------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        log.info(
            "%s bot started (%s) — session %s-%s New York time",
            self.provider.name, self.provider.mode_label,
            self.s.session_open, self.s.session_close,
        )
        mode = "⚠️ LIVE — REAL MONEY" if self.s.live_active() else self.provider.mode_label
        self._emit(f"▶️ {self.provider.name} bot started ({mode}) — session "
                   f"{self.s.session_open:%H:%M}–{self.s.session_close:%H:%M} NY")
        self.stop_event.clear()
        while not self.stop_event.is_set():
            now = self.provider.now()
            try:
                if self.in_session(now):
                    if self.s.live_active() and self.daily_loss_exceeded(now):
                        reason = (f"daily loss limit hit (-${self.s.live_max_daily_loss_usd:.0f}"
                                  f" from day start) — flattening and halting")
                        log.error("LIVE kill switch: %s", reason)
                        self.provider.flatten_all()
                        self.record("*", "sell", f"🛑 {reason}")
                        self.stop_event.set()
                        break
                    if now.time() >= self.s.flatten_at:
                        if self._flattened_on != now.date():
                            self._flattened_on = now.date()
                            self.provider.flatten_all()
                            self.record("*", "sell", "end-of-session flatten")
                    else:
                        self.step(now)
                else:
                    log.info("outside session hours (%s NY) — idle", now.strftime("%H:%M"))
            except Exception:
                log.exception("bot step failed; retrying next poll")
            if once:
                return
            self.stop_event.wait(self.poll_seconds)
        log.info("bot stopped")
        self._emit("⏹ bot stopped")
