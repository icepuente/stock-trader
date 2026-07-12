"""Strategy and runtime configuration.

All thresholds from the strategy spec live here so they can be tuned without
touching the logic. Times are New York (exchange) time; your local
16:30-23:00 window maps to the 9:30-16:00 ET regular session automatically.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENV_PATH = ".env"


def _env_list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


@dataclass
class Settings:
    # --- provider: "alpaca" (US shares, paper) or "bitget" (crypto, demo) ---
    provider: str = field(default_factory=lambda: os.environ.get("PROVIDER", "alpaca").lower())

    # --- credentials (paper/demo only; live trading is deliberately unsupported) ---
    api_key: str = field(default_factory=lambda: os.environ.get("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("ALPACA_SECRET_KEY", ""))
    bitget_api_key: str = field(default_factory=lambda: os.environ.get("BITGET_API_KEY", ""))
    bitget_secret_key: str = field(default_factory=lambda: os.environ.get("BITGET_SECRET_KEY", ""))
    bitget_passphrase: str = field(default_factory=lambda: os.environ.get("BITGET_PASSPHRASE", ""))

    # Demo pairs to trade. Empty = every USDT-margined future the demo
    # environment lists (classic demo has S-prefixed pairs, UTA demo has the
    # regular pairs). Set BITGET_SYMBOLS to restrict the universe.
    bitget_symbols: list[str] = field(default_factory=lambda: _env_list("BITGET_SYMBOLS", ""))
    # Set BITGET_UTA=1 if your Bitget account is in Unified Account mode.
    bitget_uta: bool = field(
        default_factory=lambda: os.environ.get("BITGET_UTA", "") in ("1", "true", "yes")
    )

    # --- LIVE trading (opt-in, IBKR only) -----------------------------------
    # Real money. Settable in .env or from the dashboard settings panel —
    # the dashboard path requires a typed "LIVE" confirmation, enforced
    # server-side. On top of the switch, the gateway must be logged into a
    # live account and starting the bot requires a second typed confirmation.
    # Telegram can never toggle it.
    live_trading: bool = field(
        default_factory=lambda: os.environ.get("LIVE_TRADING", "") in ("1", "true", "yes")
    )
    # Live per-symbol allocation — intentionally far below the paper $10k.
    live_allocation_usd: float = field(
        default_factory=lambda: float(os.environ.get("LIVE_ALLOCATION_USD", "500"))
    )
    # Kill switch: flatten everything and stop the bot for the day once
    # equity drops this much below the day's starting equity.
    live_max_daily_loss_usd: float = field(
        default_factory=lambda: float(os.environ.get("LIVE_MAX_DAILY_LOSS_USD", "150"))
    )
    # Belt-and-braces: refuse any single live order above this notional.
    live_order_cap_usd: float = field(
        default_factory=lambda: float(os.environ.get("LIVE_ORDER_CAP_USD", "600"))
    )

    # --- self-update from GitHub ---
    update_repo: str = field(
        default_factory=lambda: os.environ.get("UPDATE_REPO", "icepuente/stock-trader")
    )
    # Only needed while the repo is private: a fine-grained token with
    # read-only Contents access lets the updater check and download code.
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))

    # --- Telegram remote control + notifications (optional) ---
    # Token from @BotFather. Chat id optional: the first chat sending /start
    # binds itself; set TELEGRAM_CHAT_ID to pin it and lock out other chats.
    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "")
    )

    # --- IBKR (PROVIDER=ibkr): connects to a locally running IB Gateway/TWS
    # logged into a PAPER account. Gateway paper port 4002, TWS paper 7497.
    ibkr_host: str = field(default_factory=lambda: os.environ.get("IBKR_HOST", "127.0.0.1"))
    ibkr_port: int = field(default_factory=lambda: int(os.environ.get("IBKR_PORT", "4002")))
    ibkr_client_id: int = field(default_factory=lambda: int(os.environ.get("IBKR_CLIENT_ID", "17")))
    ibkr_bars_cache_seconds: int = 240   # respect IBKR historical-data pacing limits

    # --- simulation (PROVIDER=sim): replay the last trading day at speed ---
    sim_symbols: list[str] = field(default_factory=lambda: _env_list(
        "SIM_SYMBOLS", "AAPL,MSFT,NVDA,TSLA,AMD,META,AMZN,GOOGL,PLTR,COIN"
    ))
    sim_speed: float = field(default_factory=lambda: float(os.environ.get("SIM_SPEED", "60")))
    # "replay" = real data for the last trading day; "demo" = synthetic day
    # crafted so every strategy stage fires (gate, tiers A/B/C, exits)
    sim_scenario: str = field(default_factory=lambda: os.environ.get("SIM_SCENARIO", "replay"))

    # --- session (New York time) ---
    session_open: time = time(9, 30)
    session_close: time = time(16, 0)
    flatten_at: time = time(15, 55)      # close all positions before the bell
    poll_seconds: int = 60
    rescan_minutes: int = 15             # how often to refresh the volume scanner

    # --- scanner ---
    scanner_top_n: int = 50              # most-active universe pulled from Alpaca
    relative_volume_min: float = 1.08    # volume pace > 8% above 20-day average
    adv_lookback_days: int = 20
    adv_min_shares: float = 20_000_000   # rule 10: avg daily volume > 20M shares
    max_watchlist: int = 10

    # --- bars ---
    # "hour"  -> candles anchored on the clock hour (candle 1 = 9:30-10:00
    #            partial), matching the 16:00/16:30/17:00 chart the strategy
    #            spec describes. Default: with "open" anchoring candle 1
    #            swallows the full opening hour — the day's biggest volume —
    #            and the entry gate (candle 2 volume > candle 1) almost never
    #            passes (verified in replay simulation).
    # "open"  -> 1h candles anchored at 9:30 ET (candle 1 = 9:30-10:30)
    bar_anchor: str = "hour"
    intraday_history_days: int = 15      # history fed to EMA/MACD so they warm up

    # --- entry gate (2nd hourly candle) ---
    full_body_min_ratio: float = 0.7     # body / range to count as "full-body"

    # --- tier sizing (fractions of allocation_usd) ---
    allocation_usd: float = 10_000.0
    tier_a_pct: float = 0.30
    tier_b_pct: float = 0.50
    tier_c_pct: float = 0.20

    # --- rule 10: bid/ask quote sizes must each be <= 20k shares ---
    quote_size_max: float = 20_000.0

    # --- rule 11: buys come from free cash only, split across the strongest
    # movers — at most this many symbols held at once ---
    max_positions: int = 3

    # --- final validation filters ---
    vmar_period: int = 20                # VMAR = bar volume / SMA(volume, period)
    vmar_min: float = 1.0
    # EMA trend filter: require ema9 > ema20 to buy, and it feeds the exit logic

    # --- exits ---
    partial_exit_pct: float = 0.70       # take-profit tranche
    plateau_tolerance: float = 0.10      # |hist[t-1]-hist[t-2]| within 10% counts as a plateau
    hard_stop_pct: float = 0.03          # safety net: exit fully at -3% from avg entry

    def live_active(self) -> bool:
        """True only when orders can reach a real-money venue: the env switch
        is set AND the provider has a live path (IBKR only — Alpaca stays
        hardcoded to the paper endpoint, Bitget to demo, sim is virtual)."""
        return self.live_trading and self.provider == "ibkr"

    def allocation(self) -> float:
        """Per-symbol allocation actually in force ($ live vs paper)."""
        return self.live_allocation_usd if self.live_active() else self.allocation_usd

    def has_keys(self) -> bool:
        if self.provider == "bitget":
            return bool(self.bitget_api_key and self.bitget_secret_key and self.bitget_passphrase)
        if self.provider == "ibkr":
            return True  # no API keys — authentication happens in IB Gateway
        if self.provider == "sim":
            return True  # public data, virtual portfolio — nothing to configure
        return bool(self.api_key and self.secret_key)

    def require_keys(self) -> None:
        if self.has_keys():
            return
        if self.provider == "bitget":
            raise SystemExit(
                "Missing BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE. "
                "Copy .env.example to .env and fill in your Bitget API credentials "
                "(demo trading is used — no real funds are touched)."
            )
        raise SystemExit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. "
            "Copy .env.example to .env and fill in your *paper* keys."
        )


# --------------------------------------------------------------------------
# Dashboard-editable configuration: form key -> (attribute, env var, kind).
# kind: "secret" (masked in GET, blank keeps / "-" clears), "str", "int",
# "float", "bool", "csv", or "choice:a,b,c".
CONFIG_FIELDS: dict[str, tuple[str, str, str]] = {
    "provider":           ("provider", "PROVIDER", "choice:alpaca,bitget,ibkr,sim"),
    "alpaca_api_key":     ("api_key", "ALPACA_API_KEY", "secret"),
    "alpaca_secret_key":  ("secret_key", "ALPACA_SECRET_KEY", "secret"),
    "bitget_api_key":     ("bitget_api_key", "BITGET_API_KEY", "secret"),
    "bitget_secret_key":  ("bitget_secret_key", "BITGET_SECRET_KEY", "secret"),
    "bitget_passphrase":  ("bitget_passphrase", "BITGET_PASSPHRASE", "secret"),
    "bitget_uta":         ("bitget_uta", "BITGET_UTA", "bool"),
    "bitget_symbols":     ("bitget_symbols", "BITGET_SYMBOLS", "csv"),
    "ibkr_host":          ("ibkr_host", "IBKR_HOST", "str"),
    "ibkr_port":          ("ibkr_port", "IBKR_PORT", "int"),
    "ibkr_client_id":     ("ibkr_client_id", "IBKR_CLIENT_ID", "int"),
    "sim_scenario":       ("sim_scenario", "SIM_SCENARIO", "choice:replay,demo"),
    "sim_speed":          ("sim_speed", "SIM_SPEED", "float"),
    "sim_symbols":        ("sim_symbols", "SIM_SYMBOLS", "csv"),
    "telegram_bot_token": ("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "secret"),
    "telegram_chat_id":   ("telegram_chat_id", "TELEGRAM_CHAT_ID", "str"),
    "update_repo":        ("update_repo", "UPDATE_REPO", "str"),
    "github_token":       ("github_token", "GITHUB_TOKEN", "secret"),
    # REAL MONEY: enabling live_trading through the API additionally requires
    # live_confirm == "LIVE" — enforced in the /api/config endpoint.
    "live_trading":       ("live_trading", "LIVE_TRADING", "bool"),
    "live_allocation_usd":     ("live_allocation_usd", "LIVE_ALLOCATION_USD", "float"),
    "live_max_daily_loss_usd": ("live_max_daily_loss_usd", "LIVE_MAX_DAILY_LOSS_USD", "float"),
    "live_order_cap_usd":      ("live_order_cap_usd", "LIVE_ORDER_CAP_USD", "float"),
}


def _mask(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def public_config(s: Settings) -> dict:
    """Settings as shown to the dashboard — secrets masked, lists as CSV."""
    out = {}
    for key, (attr, _env, kind) in CONFIG_FIELDS.items():
        v = getattr(s, attr)
        if kind == "secret":
            out[key] = _mask(v)
        elif kind == "csv":
            out[key] = ",".join(v)
        else:
            out[key] = v
    return out


def apply_config(s: Settings, updates: dict) -> dict[str, str]:
    """Validate `updates`, mutate `s` in place, and return the env-var values
    to persist. Secrets: blank or masked input keeps the stored value, a
    single "-" clears it."""
    env_updates: dict[str, str] = {}
    for key, raw in updates.items():
        if key not in CONFIG_FIELDS:
            raise ValueError(f"unknown setting {key!r}")
        attr, env, kind = CONFIG_FIELDS[key]
        if kind == "secret":
            v = str(raw or "").strip()
            if not v or "•" in v:
                continue  # unchanged
            if v == "-":
                v = ""
            setattr(s, attr, v)
            env_updates[env] = v
        elif kind == "bool":
            b = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
            setattr(s, attr, b)
            env_updates[env] = "1" if b else "0"
        elif kind in ("int", "float"):
            try:
                v = int(raw) if kind == "int" else float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number, got {raw!r}") from None
            if kind == "float" and v <= 0:
                raise ValueError(f"{key} must be positive")
            setattr(s, attr, v)
            env_updates[env] = str(v)
        elif kind == "csv":
            lst = [t.strip() for t in str(raw or "").split(",") if t.strip()]
            setattr(s, attr, lst)
            env_updates[env] = ",".join(lst)
        elif kind.startswith("choice:"):
            options = kind.split(":", 1)[1].split(",")
            v = str(raw or "").strip().lower()
            if v not in options:
                raise ValueError(f"{key} must be one of {options}, got {raw!r}")
            setattr(s, attr, v)
            env_updates[env] = v
        else:
            v = str(raw or "").strip()
            setattr(s, attr, v)
            env_updates[env] = v
    return env_updates


def _env_quote(value: str) -> str:
    if value and not re.search(r"""[\s#"']""", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_env(updates: dict[str, str], path: str | os.PathLike | None = None) -> None:
    """Persist values into the .env file, preserving unrelated lines.

    An active `KEY=` line is rewritten in place; otherwise the first
    commented `#KEY=` placeholder is uncommented; otherwise the key is
    appended at the end.
    """
    p = Path(path or ENV_PATH)
    lines = p.read_text().splitlines() if p.exists() else []
    pending = dict(updates)

    def match_key(line: str) -> tuple[str | None, bool]:
        m = re.match(r"\s*(#?)\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        return (m.group(2), bool(m.group(1))) if m else (None, False)

    out = []
    for line in lines:  # pass 1: active assignments
        key, commented = match_key(line)
        if key in pending and not commented:
            out.append(f"{key}={_env_quote(pending.pop(key))}")
        else:
            out.append(line)
    if pending:         # pass 2: commented placeholders
        for i, line in enumerate(out):
            key, commented = match_key(line)
            if key in pending and commented:
                out[i] = f"{key}={_env_quote(pending.pop(key))}"
    if pending:         # pass 3: append what's left
        out.extend(f"{key}={_env_quote(v)}" for key, v in pending.items())
    p.write_text("\n".join(out) + "\n")
