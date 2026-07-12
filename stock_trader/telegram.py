"""Telegram bridge: remote control + trade notifications for the bot.

Setup: talk to @BotFather on Telegram, `/newbot`, and put the token in .env
as TELEGRAM_BOT_TOKEN. Start the server, then send /start to your bot — the
first chat that does so is bound for the session. Pin TELEGRAM_CHAT_ID in
.env to survive restarts and lock everyone else out (recommended).

The bridge long-polls api.telegram.org over plain HTTPS (stdlib urllib — no
webhook, no public endpoint, works from behind NAT) and it can only ever
drive the same paper/demo providers the dashboard drives.
"""

import html
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

_API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "<b>stock_trader</b> — paper/demo trading bot\n"
    "/status — bot, clock and watchlist state\n"
    "/account — equity, cash, day P&amp;L\n"
    "/positions — open positions\n"
    "/watchlist — scanner picks + tier progress\n"
    "/run — start the trading loop\n"
    "/stop — stop the trading loop\n"
    "/log [n] — last n log lines (default 15)\n"
    "/help — this message"
)


def _usd(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


class _LogRing(logging.Handler):
    """Small independent log buffer so /log works in every run mode."""

    def __init__(self, maxlen: int = 200):
        super().__init__(level=logging.INFO)
        self.lines: deque[str] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        t = datetime.fromtimestamp(record.created, NY).strftime("%H:%M:%S")
        self.lines.appendleft(f"{t} {record.levelname:<7} {record.getMessage()}")


class HeadlessRunner:
    """Adapts a foreground `python main.py run` Bot to the runner interface."""

    def __init__(self, bot):
        self.bot = bot

    @property
    def running(self) -> bool:
        return not self.bot.stop_event.is_set()

    def start(self) -> None:
        raise RuntimeError(
            "the bot runs in the foreground in this mode — "
            "restart `python main.py run` to resume"
        )

    def stop(self) -> None:
        self.bot.stop_event.set()


class TelegramBridge:
    """Long-polling command handler + outbound notifier.

    `runner` is anything with `.bot`, `.running`, `.start()`, `.stop()`
    (the web server's BotRunner, or HeadlessRunner for the CLI loop).
    """

    def __init__(self, settings: Settings, runner):
        self.s = settings
        self.runner = runner
        self.chat_id: str | None = settings.telegram_chat_id.strip() or None
        self._pinned = self.chat_id is not None
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ring = _LogRing()
        logging.getLogger().addHandler(self._ring)

    # ------------------------------------------------------ transport --
    def _api(self, method: str, **params):
        """One Telegram Bot API call. Patched out in tests."""
        req = urllib.request.Request(
            _API.format(token=self.s.telegram_bot_token, method=method),
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        # long polls ask Telegram to hold for up to 50s; leave headroom
        with urllib.request.urlopen(req, timeout=70) as resp:
            payload = json.load(resp)
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {payload}")
        return payload["result"]

    def _reply(self, chat_id: str, text_html: str) -> None:
        self._api("sendMessage", chat_id=chat_id, text=text_html,
                  parse_mode="HTML", disable_web_page_preview=True)

    # ---------------------------------------------------- outbound push --
    def send_event(self, text: str) -> None:
        """Notify the bound chat. Never raises — called from the bot loop."""
        if not self.chat_id:
            return
        try:
            self._reply(self.chat_id, html.escape(text))
        except Exception:
            log.warning("telegram: notification failed", exc_info=True)

    # ------------------------------------------------------- lifecycle --
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        logging.getLogger().removeHandler(self._ring)

    def _poll_loop(self) -> None:
        try:
            me = self._api("getMe")
            log.info("telegram: connected as @%s — send /start to it%s",
                     me.get("username"),
                     "" if self._pinned else " to bind this chat")
        except Exception:
            log.warning("telegram: getMe failed — check TELEGRAM_BOT_TOKEN "
                        "(will keep retrying)", exc_info=True)
        while not self._stop.is_set():
            try:
                updates = self._api("getUpdates", offset=self._offset,
                                    timeout=50, allowed_updates=["message"])
            except (urllib.error.URLError, TimeoutError, RuntimeError, OSError):
                log.warning("telegram: getUpdates failed — retrying in 5s")
                self._stop.wait(5)
                continue
            for u in updates:
                self._offset = max(self._offset, u["update_id"] + 1)
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id") or "")
                text = (msg.get("text") or "").strip()
                if not chat or not text:
                    continue
                try:
                    self._handle(chat, text)
                except Exception as e:
                    log.exception("telegram: command failed: %s", text)
                    try:
                        self._reply(chat, f"⚠️ {html.escape(str(e))}")
                    except Exception:
                        pass

    # -------------------------------------------------------- commands --
    def _handle(self, chat: str, text: str) -> None:
        cmd = text.split()[0].lower().split("@")[0]
        args = text.split()[1:]

        if self.chat_id is None:
            if cmd == "/start":
                self.chat_id = chat
                log.info("telegram: bound to chat %s — set TELEGRAM_CHAT_ID=%s "
                         "in .env to pin it across restarts", chat, chat)
                self._reply(chat, "🔗 chat bound — you'll get trade "
                                  "notifications here.\n\n" + HELP)
            # anything else from an unbound chat is ignored silently
            return
        if chat != self.chat_id:
            log.warning("telegram: ignoring message from unauthorized chat %s", chat)
            return

        if cmd in ("/start", "/help"):
            self._reply(chat, HELP)
        elif cmd == "/status":
            self._reply(chat, self._status_text())
        elif cmd == "/account":
            a = self._bot().provider.account()
            self._reply(chat, f"💰 equity {_usd(a.get('equity'))} · "
                              f"cash {_usd(a.get('cash'))} · "
                              f"day P&amp;L {_usd(a.get('day_pl'))}")
        elif cmd == "/positions":
            self._reply(chat, self._positions_text())
        elif cmd == "/watchlist":
            self._reply(chat, self._watchlist_text())
        elif cmd == "/run":
            if self.runner.running:
                self._reply(chat, "already running ▶️")
            elif self.s.live_active() and args != ["live"]:
                self._reply(chat, "⚠️ <b>LIVE trading is enabled — real money.</b>\n"
                                  "Send <code>/run live</code> to confirm.")
            else:
                self.runner.start()
                self._reply(chat, "▶️ bot started"
                            + (" — ⚠️ LIVE, real money" if self.s.live_active() else ""))
        elif cmd == "/stop":
            if not self.runner.running:
                self._reply(chat, "already stopped ⏸")
            else:
                self.runner.stop()
                self._reply(chat, "⏸ bot stopped")
        elif cmd == "/log":
            n = int(args[0]) if args and args[0].isdigit() else 15
            lines = list(self._ring.lines)[:n]
            body = html.escape("\n".join(reversed(lines))) or "log is empty"
            self._reply(chat, f"<pre>{body}</pre>")
        else:
            self._reply(chat, "unknown command — /help")

    # --------------------------------------------------------- helpers --
    def _bot(self):
        ensure = getattr(self.runner, "ensure_bot", None)
        bot = ensure() if ensure else self.runner.bot
        if bot is None:
            raise RuntimeError("bot not initialized yet — /run to start it")
        return bot

    def _status_text(self) -> str:
        state = "running ▶️" if self.runner.running else "stopped ⏸"
        lines = [f"🤖 <b>{html.escape(self.s.provider)}</b> — bot {state}"]
        if self.s.live_active():
            lines.insert(0, "⚠️ <b>LIVE TRADING — REAL MONEY</b>")
        bot = self.runner.bot
        if bot is not None:
            try:
                now = bot.provider.now()
                market = "open" if bot.provider.market_is_open() else "closed"
                lines.append(f"🕒 {now:%H:%M:%S} NY · market {market} "
                             f"({bot.provider.mode_label})")
            except Exception as e:
                lines.append(f"🕒 clock unavailable: {html.escape(str(e))}")
            lines.append(self._watchlist_text())
            try:
                a = bot.provider.account()
                lines.append(f"💰 equity {_usd(a.get('equity'))} · "
                             f"cash {_usd(a.get('cash'))} · "
                             f"day P&amp;L {_usd(a.get('day_pl'))}")
            except Exception as e:
                lines.append(f"💰 account unavailable: {html.escape(str(e))}")
        else:
            lines.append("bot not initialized yet — /run to start it")
        return "\n".join(lines)

    def _watchlist_text(self) -> str:
        bot = self.runner.bot
        if bot is None or not bot.watchlist:
            return "👀 watchlist: empty"
        rows = []
        for sym in bot.watchlist:
            st = bot.states.get(sym)
            if st is None:
                rows.append(html.escape(sym))
                continue
            chips = []
            if st.gate_passed:
                chips.append("gate✓")
            chips += [f"{t}✓" for t in sorted(st.tiers_filled)]
            if st.partial_exit_done:
                chips.append("70%out")
            if st.done_for_day:
                chips.append("done")
            rows.append(html.escape(f"{sym} {' '.join(chips)}".strip()))
        return "👀 watchlist: " + ", ".join(rows)

    def _positions_text(self) -> str:
        pos = self._bot().provider.positions()
        if not pos:
            return "📭 no open positions"
        rows = []
        for p in pos:
            rows.append(html.escape(
                f"{p['symbol']} ×{p['qty']:g} @ {p['avg_entry']:.2f} → "
                f"{p['price']:.2f} ({p['unrealized_plpc']:+.1%}, "
                f"{_usd(p['unrealized_pl'])})"
            ))
        return "📈 " + "\n".join(rows)


def maybe_start_bridge(settings: Settings, runner) -> TelegramBridge | None:
    """Create + start the bridge when a token is configured, else None."""
    if not settings.telegram_bot_token:
        return None
    bridge = TelegramBridge(settings, runner)
    bridge.start()
    return bridge
