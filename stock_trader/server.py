"""FastAPI web server: dashboard UI + JSON API over the paper/demo trading bot.

The bot loop runs in a background thread, started/stopped from the UI.
Binds to localhost by default — there is no auth layer, don't expose it.
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .bot import Bot
from .config import Settings, apply_config, first_run, public_config, save_env
from .gateway import GatewayManager
from .indicators import add_indicators
from .telegram import maybe_start_bridge
from .updater import Updater

NY = ZoneInfo("America/New_York")
STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


class RingBufferHandler(logging.Handler):
    """Keeps the last N log lines in memory for the dashboard."""

    def __init__(self, maxlen: int = 300):
        super().__init__(level=logging.INFO)
        self.lines: deque[dict] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.appendleft({
            "time": datetime.fromtimestamp(record.created, NY).strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
        })


class BotRunner:
    """Owns the Bot instance and its background thread."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bot: Bot | None = None
        self.thread: threading.Thread | None = None
        self.notifier = None  # set by the Telegram bridge when configured
        self._clock_cache: tuple[float, dict] | None = None

    @property
    def keys_configured(self) -> bool:
        return self.settings.has_keys()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def ensure_bot(self) -> Bot:
        if not self.keys_configured:
            raise HTTPException(
                status_code=409,
                detail=f"{self.settings.provider} keys not configured — open "
                       "⚙ Settings in the dashboard (or edit .env) to add them.",
            )
        if self.bot is None:
            self.bot = Bot(self.settings)
            if self.notifier:
                self.bot.on_event = self.notifier
        return self.bot

    def start(self) -> None:
        if self.running:
            return
        bot = self.ensure_bot()
        self.thread = threading.Thread(target=bot.run, name="bot-loop", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.bot:
            self.bot.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            self.thread = None

    def reset(self) -> None:
        """Discard the bot so the next start builds a fresh provider."""
        self.stop()
        self.bot = None
        self._clock_cache = None

    def market_clock(self) -> dict:
        """Provider market clock, cached briefly so UI polling stays cheap."""
        if self._clock_cache and time.monotonic() - self._clock_cache[0] < 15:
            return self._clock_cache[1]
        info = self.ensure_bot().provider.clock_info()
        self._clock_cache = (time.monotonic(), info)
        return info


def _clean(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 4)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runner = BotRunner(settings)
    ring = RingBufferHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(ring)

    bridge: dict = {"b": maybe_start_bridge(settings, runner)}
    if bridge["b"]:
        runner.notifier = bridge["b"].send_event
    gateway = GatewayManager(settings)
    gateway.ensure()  # IBKR selected + gateway installed but not running -> launch it
    updater = Updater(settings)

    app = FastAPI(title="stock_trader", docs_url="/docs")
    app.state.runner = runner

    @app.exception_handler(Exception)
    async def surface_provider_errors(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    # ------------------------------------------------------------- pages --
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # --------------------------------------------------------------- api --
    @app.get("/api/status")
    def status() -> dict:
        clock = runner.bot.provider.now() if runner.bot else datetime.now(NY)
        out: dict = {
            "first_run": first_run(),
            "keys_configured": runner.keys_configured,
            "running": runner.running,
            "ny_time": clock.strftime("%H:%M:%S"),
            "session": f"{settings.session_open:%H:%M}–{settings.session_close:%H:%M} ET",
            "provider": settings.provider,
            "mode": ("LIVE" if settings.live_active()
                     else {"bitget": "DEMO", "sim": "SIM"}.get(settings.provider, "PAPER")),
            "live": settings.live_active(),
            "telegram": bridge["b"] is not None,
            "gateway": gateway.status() if settings.provider == "ibkr" else None,
            "watchlist": [],
            "states": {},
            "market": None,
        }
        if not runner.keys_configured:
            return out
        try:
            out["market"] = runner.market_clock()
        except Exception as e:  # bad keys, network down — surface, don't 500
            out["error"] = str(e)
            return out
        if runner.bot:
            out["watchlist"] = runner.bot.watchlist
            out["states"] = {
                sym: {
                    "gate_passed": st.gate_passed,
                    "tiers_filled": sorted(st.tiers_filled),
                    "partial_exit_done": st.partial_exit_done,
                    "done_for_day": st.done_for_day,
                }
                for sym, st in runner.bot.states.items()
            }
        return out

    @app.post("/api/bot/start")
    def bot_start(confirm: str = "") -> dict:
        if settings.live_active() and confirm != "LIVE":
            raise HTTPException(
                status_code=428,
                detail="LIVE trading is enabled — real money. Repeat the "
                       "request with ?confirm=LIVE to start the bot.",
            )
        if settings.provider == "ibkr":
            gateway.ensure()
        runner.start()
        return {"running": runner.running}

    @app.post("/api/bot/stop")
    def bot_stop() -> dict:
        runner.stop()
        return {"running": runner.running}

    @app.get("/api/config")
    def get_config() -> dict:
        return public_config(settings)

    @app.post("/api/config")
    async def set_config(request: Request) -> dict:
        updates = await request.json()
        was_running = runner.running

        # REAL-MONEY gate: turning live trading ON requires a typed "LIVE"
        # confirmation, checked here so no client can skip it.
        live_confirm = str(updates.pop("live_confirm", "") or "")
        wants_live = updates.get("live_trading") in (True, "1", "true", "yes", "on")
        if wants_live and not settings.live_trading and live_confirm != "LIVE":
            raise HTTPException(
                status_code=428,
                detail="Enabling LIVE trading uses real money and requires the "
                       'typed confirmation — resend with "live_confirm": "LIVE".',
            )
        was_live = settings.live_trading

        try:
            env_updates = apply_config(settings, updates)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        save_env(env_updates)

        if settings.live_trading != was_live:
            msg = ("⚠️ LIVE trading ENABLED from the dashboard — real money "
                   "once IB Gateway is on a live account and the bot is started."
                   if settings.live_trading else
                   "✅ Live trading disabled — back to paper/demo only.")
            log.warning(msg)
            if runner.notifier:
                runner.notifier(msg)
        runner.reset()  # next start builds the provider from the new settings

        # restart the Telegram bridge only when its settings actually changed
        if any(k in env_updates for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")):
            old = bridge["b"]
            bound = old.chat_id if old else None
            if old:
                old.stop()
            bridge["b"] = maybe_start_bridge(settings, runner)
            # keep an auto-bound chat across saves unless a pin was set
            if bridge["b"] and not settings.telegram_chat_id and bound:
                bridge["b"].chat_id = bound
            runner.notifier = bridge["b"].send_event if bridge["b"] else None

        if settings.provider == "ibkr":
            gateway.ensure()  # switching to IBKR: bring the gateway up too

        restarted = False
        if was_running and settings.has_keys():
            runner.start()
            restarted = True
        log.info("configuration updated: %s%s",
                 ", ".join(sorted(env_updates)) or "nothing changed",
                 " — bot restarted" if restarted else "")
        return {"config": public_config(settings), "bot_restarted": restarted,
                "saved": sorted(env_updates)}

    @app.get("/api/update")
    def update_check() -> dict:
        return updater.check()

    @app.post("/api/update")
    def update_apply() -> dict:
        return updater.update()

    @app.get("/api/gateway")
    def gateway_status() -> dict:
        return gateway.status()

    @app.post("/api/gateway/start")
    def gateway_start() -> dict:
        return gateway.start()

    @app.post("/api/gateway/install")
    def gateway_install() -> dict:
        return gateway.install()

    @app.get("/api/account")
    def account() -> dict:
        return runner.ensure_bot().provider.account()

    @app.get("/api/positions")
    def positions() -> list[dict]:
        return runner.ensure_bot().provider.positions()

    @app.post("/api/scan")
    def scan() -> dict:
        bot = runner.ensure_bot()
        symbols = bot.provider.scan()
        bot.watchlist = list(dict.fromkeys(symbols + bot.watchlist))
        return {"watchlist": bot.watchlist}

    # {symbol:path} so crypto pairs like SBTC/SUSDT:SUSDT survive the router
    @app.get("/api/bars/{symbol:path}")
    def bars(symbol: str, limit: int = 60) -> dict:
        provider = runner.ensure_bot().provider
        sym = symbol.upper()
        data = provider.hourly_bars(sym)
        if data.empty:
            raise HTTPException(status_code=404, detail=f"no data for {sym!r}")
        df = add_indicators(data, settings.vmar_period).tail(limit)
        return {
            "symbol": sym,
            "bars": [
                {
                    "t": ts.strftime("%m-%d %H:%M"),
                    "day": ts.strftime("%m-%d"),
                    "o": _clean(r["open"]), "h": _clean(r["high"]),
                    "l": _clean(r["low"]), "c": _clean(r["close"]),
                    "v": int(r["volume"]),
                    "ema9": _clean(r["ema9"]), "ema20": _clean(r["ema20"]),
                    "hist": _clean(r["macd_hist"]), "vmar": _clean(r["vmar"]),
                    "n": int(r["candle_no"]),
                }
                for ts, r in df.iterrows()
            ],
        }

    @app.get("/api/activity")
    def activity() -> list[dict]:
        return list(runner.bot.activity) if runner.bot else []

    @app.get("/api/logs")
    def logs(limit: int = 100) -> list[dict]:
        return list(ring.lines)[:limit]

    return app


app = create_app()
