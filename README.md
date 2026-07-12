# stock_trader — tiered MACD/volume paper-trading bot

Implements the "New York session" tiered entry/exit strategy against a
simulated account on your choice of provider:

- **Alpaca** (default) — US shares, paper-trading account
- **Bitget** — crypto, Bitget's demo-trading environment (simulated USDT-M
  futures; universe auto-detected — UTA demo lists regular pairs like
  `BTC/USDT:USDT`, classic demo lists S-prefixed pairs)
- **IBKR** — US shares via Interactive Brokers, through a locally running
  IB Gateway/TWS logged into a paper account

It monitors high-relative-volume symbols during NY session hours (9:30–16:00
New York time — 16:30–23:00 if you're in a UTC+2/+3 timezone), evaluates
hourly candles with MACD, volume, VMAR and EMA 9/20, and places simulated
market orders. Crypto trades 24/7, but the strategy stays anchored to the NY
session window — with Bitget it simply runs every day, weekends included.

> **Paper/demo by default.** The Alpaca client is hardcoded to the paper
> endpoint, the Bitget client always sends the demo-trading flag, and IBKR
> refuses live logins unless you explicitly opt in. A live-trading mode
> exists for **IBKR only**, gated behind `LIVE_TRADING=1` in `.env` plus a
> typed confirmation — see [Live trading](#live-trading-ibkr-only--real-money)
> before even thinking about it. This is an experiment, not financial
> advice — validate the strategy on paper before risking anything real.

## Easy setup on Windows (no technical knowledge needed)

1. **Get the project onto the computer.** On this repo's GitHub page click the
   green **Code** button → **Download ZIP**, then right-click the downloaded
   file → **Extract All…** and put the folder somewhere easy, like Documents.
2. **Double-click `start_bot.bat`** inside that folder.
   - If Python isn't installed yet, a download page opens automatically —
     click *Download Python*, run the installer, and **tick
     "Add python.exe to PATH"** on its first screen. Then double-click
     `start_bot.bat` again.
   - The first start downloads what it needs (a few minutes). After that,
     starting takes seconds.
3. Your browser opens the dashboard at `http://127.0.0.1:8000`. Click
   **⚙ Settings**, pick a provider (choose **Simulation** to just watch it
   work, no accounts needed), and press **Start bot**.
4. Keep the black window open while using it — closing it stops the bot.

Everything runs only on that computer; nothing is exposed to the internet.

**Updates**: when new code lands on GitHub, an **⬆ Update** button appears in
the dashboard header — one click downloads it, reinstalls anything needed and
restarts the app by itself. (While this repo is private, paste a read-only
GitHub token under ⚙ Settings → Updates so the updater can reach it.) Git
checkouts update via `git pull --ff-only`; ZIP installs download the latest
zipball and never touch `.env` or the environment.

## Setup (Mac/Linux, command line)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional — ⚙ Settings in the dashboard writes it too
```

- Alpaca paper keys: <https://app.alpaca.markets/paper/dashboard/overview>
- Bitget API keys (key + secret + passphrase, read/trade scope): switch
  bitget.com to **Demo Trading** mode first, then create the key *inside*
  the demo environment — set `PROVIDER=bitget` (and `BITGET_UTA=1` for
  Unified Account mode)
- IBKR: no API keys. Pick the IBKR provider in ⚙ Settings — the panel shows
  the gateway state with **Install IB Gateway** (downloads IBKR's official
  installer and, on Windows, installs it silently) and **Start IB Gateway**
  buttons. Whenever the server or bot starts with IBKR selected, the gateway
  is launched automatically if it isn't already running. **Logging in stays
  manual** — IBKR requires it (plus two-factor on live accounts): in the
  gateway window pick *IB API* (not FIX CTCI), log in with your **paper**
  account (username starts with `DU`), then under Configure → Settings →
  API → Settings enable "ActiveX and Socket Clients" and untick "Read-Only
  API". The gateway restarts itself daily (Lock and Exit → set Auto restart
  to stay logged in through the week). The bot checks the connected account
  code and refuses live logins unless live mode is explicitly enabled.
  Without paid market-data subscriptions IBKR serves 15-min-delayed data,
  which shifts every signal — Alpaca's free feed is real-time. IBKR request
  pacing is respected via a 4-minute bars cache per symbol.

## Run

### Web dashboard (recommended)

```bash
python main.py serve         # http://127.0.0.1:8000
```

The dashboard shows account equity / day P&L tiles, the live watchlist with
per-symbol signal state (gate + tiers A/B/C), open positions, the trade
activity feed, server logs, and an hourly chart (candles, EMA 9/20, volume,
MACD histogram) for any symbol — click a watchlist row or type a ticker.
Start/stop the bot loop from the header. `?symbol=NVDA` deep-links a chart.

**⚙ Settings** in the header configures everything without touching `.env` by
hand: pick the provider (sim / Alpaca / IBKR / Bitget), paste credentials, and
set the Telegram token — changes are written back to `.env` and applied
immediately (the bot is rebuilt on the new provider and restarted if it was
running). Secrets are shown masked; leaving a secret blank keeps the saved
value, entering `-` clears it.

The server binds to localhost and has **no authentication** — don't expose it
(`--host/--port` flags exist for LAN use at your own risk).

### Telegram remote control (optional)

Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), put the
token in `.env` as `TELEGRAM_BOT_TOKEN`, and restart the server. Then open a
chat with your bot and send `/start` — that chat gets bound and receives a
push notification for every trade (tier buys, exits, hard stops, the
end-of-session flatten) plus bot start/stop. Commands:

| Command | Effect |
|---|---|
| `/status` | bot state, provider clock, watchlist with tier progress, account |
| `/account` | equity, free cash, day P&L |
| `/positions` | open positions with live unrealized P&L |
| `/watchlist` | scanner picks + per-symbol gate/tier chips |
| `/run` / `/stop` | start / stop the trading loop |
| `/log [n]` | last *n* server log lines (default 15) |

The server logs the numeric chat id when you bind; set it as
`TELEGRAM_CHAT_ID` in `.env` so restarts reconnect automatically and **every
other chat is ignored** — without it, whoever messages the bot first after a
restart gets control. The bridge long-polls `api.telegram.org` (no webhook,
no inbound port) and works for `serve` and the headless `run` mode alike
(in headless mode `/run` can't restart a stopped loop — rerun the CLI).

### Headless CLI

```bash
python main.py scan          # show today's high-relative-volume watchlist
python main.py bars AAPL     # inspect hourly candles + indicators
python main.py step          # one evaluation pass (no loop)
python main.py run           # full session loop — polls every 60s
```

The bot idles outside market hours and flattens all positions at 15:55 ET.

## Live trading (IBKR only) — REAL MONEY

Live mode is off by default. It can be enabled in ⚙ Settings (a red warning
panel plus a typed `LIVE` confirmation, enforced server-side) or by setting
`LIVE_TRADING=1` in `.env`; either way it persists to `.env`. Telegram can
never toggle it.

To go live you must clear **four** independent gates:

1. The live-trading switch: tick it in ⚙ Settings and type `LIVE` when
   asked, or set `LIVE_TRADING=1` in `.env` and restart.
2. IB Gateway logged into your **live** account (Gateway live port 4001,
   TWS live 7496 — update `IBKR_PORT`). With the flag off, a live login is
   refused outright; with it on, a paper login still trades paper.
3. Typing `LIVE` **again** when starting the bot — every start, not just
   the first (API: `POST /api/bot/start?confirm=LIVE`; Telegram: `/run live`).
4. The pre-flight account check — the provider re-verifies the connected
   account codes on every connect.

While live, the following guardrails are always on:

| Guardrail | Default | Env var |
|---|---|---|
| Per-symbol allocation (replaces the paper $10k) | $500 | `LIVE_ALLOCATION_USD` |
| Daily-loss kill switch — flattens everything and halts the bot for the day | −$150 from day-start equity | `LIVE_MAX_DAILY_LOSS_USD` |
| Per-order notional cap | $600 | `LIVE_ORDER_CAP_USD` |
| −3% per-position hard stop, 15:55 ET flatten | always | (same as paper) |

Every live order is pushed to Telegram tagged `⚠️ LIVE`, the dashboard badge
turns red, and Alpaca/Bitget remain paper/demo-only regardless of the flag.

**Warning, stated plainly:** this strategy has not been validated on live
market data — in replay simulation it produced zero entries on a normal day,
and the demo day that shows trades is synthetic. It trades unattended with
market orders. Run Alpaca paper for a meaningful period first, and only ever
allocate money you can afford to lose.

## Strategy implementation

All thresholds live in `stock_trader/config.py`.

| Spec rule | Implementation |
|---|---|
| Volume > 8% above daily average | Alpaca: most-actives universe filtered on today's volume *pace* vs 20-day ADV > 1.08 (`relative_volume_min`). Bitget: same rule over the demo pairs' UTC daily candles |
| Entry gate (2nd hourly candle) | Candle 2 green **and** volume > candle 1 volume |
| Tier A — 30% | MACD histogram green & rising, EMA9 > EMA20 uptrend, volume rising (the "≤ 20k" clause is the rule-10 quote-size cap below) |
| Tier B — 50% | Candle 2 closes full-body green (body ≥ 70% of range), histogram rising, volume up |
| Tier C — 20% | Candle 3 has started, volume rising, histogram green & rising |
| Final validation | Every buy also requires EMA9 > EMA20 with price above EMA9, and VMAR ≥ 1.0 |
| Exit 70% | Green histogram plateaus (two bars within 10%) then prints a lower bar |
| Exit 30% (rest) | Trend flips down (EMA9 < EMA20 or close < EMA20) + volume dropping + histogram lower |
| Rule 10 — liquidity | Scanner requires 20-day avg daily volume > 20M shares (`adv_min_shares`); every buy requires bid & ask quote sizes ≤ 20k shares (`quote_size_max`) |
| Rule 11 — cash only | Buys only when free cash covers the order (no margin), across at most 3 symbols at once (`max_positions`) |
| Rule 12 — fake dips | A volume dip on candle 3 or 5 with the histogram still green suppresses the reversal stop (logged as a fake-out) |
| Extra safety | Hard stop at −3% unrealized on any position; full flatten at 15:55 ET |

Tiers size against `allocation_usd` (default $10,000) per symbol.

## Interpretation notes — please review

The original spec had a few ambiguities; here's what was assumed:

1. **Candle anchoring.** Hourly candles are anchored on the clock hour by
   default (candle 1 = the 9:30–10:00 partial, candle 2 = 10:00–11:00),
   matching the "16:00 / 17:00 candle" charts the spec describes. This
   matters: with open-anchored candles (`bar_anchor = "open"`, candle 1 =
   9:30–10:30) the first candle contains the opening volume spike and the
   entry gate (candle 2 volume > candle 1) almost never passes — replay
   simulation confirmed zero entries on a normal day.
2. **"Volume sales shares ask/bid ≤ 20k" (rule 10 / Tier A).** Read as the
   **order-book quote sizes**: shares resting on the bid and ask must each be
   ≤ 20,000. The alternative reading — *traded* volume ≤ 20k/minute — is
   internally contradictory: a stock with ADV > 20M shares averages ~51k
   shares/min, so it could never also be >8% above its average pace. Alpaca
   IEX quote sizes arrive in round lots and are converted to shares; IBKR
   can't provide meaningful delayed quote sizes, so the check is skipped
   there; for crypto the threshold is in coins (tune `quote_size_max`).
3. **"Sales volume"** throughout is treated as total traded volume — Alpaca
   (like most feeds) doesn't split buy-side vs sell-side volume on bars.
4. **VMAR** is implemented as bar volume ÷ 20-bar volume SMA (Volume Moving
   Average Ratio), requiring ≥ 1.0 to validate a buy.

## Tests

```bash
python -m pytest tests/ -q
```

Synthetic-data tests cover the indicator math, hourly aggregation, and the
full tier/exit state machine — no API keys needed.
