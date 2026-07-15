# Current bot behavior — rule-by-rule reference

The canonical map from the original 12-rule spec to what the code does today.
When changing strategy behavior, update this file in the same commit.

Original spec times are local (UTC+2/+3): the 16:00–23:00 working window is
the NY 9:30–16:00 regular session; "the 16:30 candle" is candle 1 (the
9:30–10:00 partial), "the 17:00 candle" is candle 2 (10:00–11:00 ET).

All thresholds are fields on `Settings` in `stock_trader/config.py` —
change numbers there, not in the logic.

## Rule 1 — working hours (NY 16:00–23:00 local = 9:30–16:00 ET)

- The loop only evaluates while the provider says the market is open **and**
  the ET time is inside `session_open`–`session_close` (09:30–16:00);
  otherwise it idles. `Bot.in_session`, `stock_trader/bot.py`.
- All positions are force-closed at `flatten_at` (15:55 ET), once per day.
- Crypto (Bitget) is open 24/7 but still anchors to the same NY window,
  every day including weekends.
- Poll cadence: `poll_seconds` (60s; the sim provider polls every 2s because
  its clock runs at `sim_speed`× ).

## Rule 2 — watchlist: volume > 8% above the day average

- Every `rescan_minutes` (15) the scanner rebuilds the watchlist
  (`Bot.maybe_rescan`; per-provider `scan()` in `stock_trader/providers/`).
- Alpaca: pulls the `scanner_top_n` (50) most-active US shares, computes
  today's volume **pace** vs the `adv_lookback_days` (20)-day average daily
  volume, keeps those above `relative_volume_min` (1.08 = +8%), capped at
  `max_watchlist` (10). Bitget applies the same ratio over UTC daily candles.
- Symbols still held stay on the watchlist even if they drop off the scan.

## Rule 3 — entry gate (2nd hourly candle)

- Candles are clock-hour anchored (`bar_anchor = "hour"`): candle 1 is the
  9:30–10:00 partial, candle 2 is 10:00–11:00 — matching the spec's
  16:30/17:00 charts. (Open-anchored candles make the gate nearly
  impossible; see "Interpretation notes" in README.md.)
- Gate: candle 2 closed green **and** candle 2 volume > candle 1 volume.
  Until it passes (per symbol, per day) no buys happen. `strategy.evaluate`.
- All tier/exit rules run on **closed** candles only (the still-forming bar
  is ignored, except for detecting that candle 3 has started).

## Rule 4 — Tier A buy: 30%

- Conditions: MACD histogram green **and** rising vs the previous bar
  (`hist_green_and_rising`), uptrend (`trend_up`: EMA9 > EMA20 and
  close > EMA9), volume above the previous bar.
- The "sales volume no more than 20k" clause is implemented as the rule-10
  bid/ask quote-size cap (part of every buy's validation, see rule 10).
- Buys `tier_a_pct` (30%) of the per-symbol allocation. Each tier fires at
  most once per symbol per day (`SymbolState.tiers_filled`).

## Rule 5 — Tier B buy: 50%

- Conditions: candle 2 closed green with a full body (body ≥
  `full_body_min_ratio` (70%) of its high–low range), histogram green &
  rising, candle 2 volume > candle 1 volume.
- Buys `tier_b_pct` (50%) of the allocation.

## Rule 6 — Tier C buy: 20%

- Conditions: candle 3 has started forming (open or closed), volume above
  the previous closed bar, histogram green & rising.
- Buys `tier_c_pct` (20%) of the allocation.

## Rule 7 — exit 70% on histogram plateau-then-drop

- `hist_plateau_then_drop`: the two previous histogram bars are green and
  within `plateau_tolerance` (10%) of each other (a plateau) and the latest
  closed bar is lower. Sells `partial_exit_pct` (70%) of the position, once
  per day (`SymbolState.partial_exit_done`).

## Rule 8 — exit the rest on trend reversal

- `trend_down` (EMA9 < EMA20 **or** close < EMA20) + volume below the
  previous bar + histogram bar lower than the previous → sell 100% of what
  remains and mark the symbol done for the day.
- Subject to the rule-12 fake-out suppression below.

## Rule 9 — VMAR and EMA 9/20 on every final decision

- Every buy (all tiers) additionally requires: `trend_up` (EMA9 > EMA20 and
  price above EMA9) and VMAR ≥ `vmar_min` (1.0).
- VMAR = bar volume ÷ SMA(volume, `vmar_period` (20)). EMA/MACD warm up on
  `intraday_history_days` (15) of hourly history.
- EMA 9/20 also drives the rule-8 exit via `trend_down`.

## Rule 10 — liquidity limits

- Scanner side: 20-day average daily volume must exceed `adv_min_shares`
  (20,000,000 shares).
- Order side: resting bid and ask sizes must each be ≤ `quote_size_max`
  (20,000 shares) or all buys are blocked. Alpaca IEX round lots are
  converted to shares; IBKR delayed data has no meaningful sizes so the
  check is skipped there; for crypto the threshold is in coins.
- (Reading of the ambiguous "20k per min" wording: order-book quote sizes —
  see README "Interpretation notes" #2.)

## Rule 11 — free cash only, spread across the best movers

- A buy is skipped unless free cash covers the full order (no margin).
- At most `max_positions` (3) symbols held at once; buys in already-held
  symbols are always allowed to complete their tiers. `Bot._buy_allowed`.
- Tier fractions size against `allocation()` per symbol: `allocation_usd`
  ($10,000) on paper, `live_allocation_usd` ($500) when live.

## Rule 12 — fake volume dips on candles 3 and 5

- If the rule-8 reversal fires on candle 3 or 5 while the MACD histogram is
  still green, it is treated as a fake-out: the stop is suppressed and the
  position held (logged). The plateau exit and hard stop are NOT suppressed.

## Safety behavior beyond the spec

| Behavior | Trigger | Where |
|---|---|---|
| Hard stop | position ≤ −`hard_stop_pct` (3%) unrealized → sell 100%, done for day; independent of indicators | `Bot.process_symbol` |
| End-of-session flatten | 15:55 ET (`flatten_at`) → close everything, once per day | `Bot.run` / `strategy.evaluate` |
| Daily reset | all per-symbol state (gate, tiers, exits) resets on date change | `SymbolState.reset_if_new_day` |
| Live kill switch | live only: equity −`live_max_daily_loss_usd` ($150) below day-start → flatten all, halt bot | `Bot.daily_loss_exceeded` |
| Live order cap | live only: any single order > `live_order_cap_usd` ($600) is refused | `Bot.process_symbol` |
| Live gating | real money needs: `LIVE_TRADING=1` **and** provider=ibkr **and** live-account gateway login **and** typed `LIVE` at every bot start; Alpaca/Bitget/sim can never go live | `Settings.live_active`, README |
| Error isolation | one symbol's failure never stops the loop; scanner failure keeps the previous watchlist | `Bot.step` / `maybe_rescan` |

## Execution details

- Orders are **market** orders: buys sized by notional (fraction ×
  allocation ÷ latest close), sells by fraction of the held quantity.
- Activity feed (last 200 events) drives the dashboard and Telegram pushes;
  live orders are tagged `⚠️ LIVE`.
- Providers: `alpaca` (paper, real-time IEX), `ibkr` (via IB Gateway;
  15-min-delayed data without subscriptions; 4-min bars cache for pacing),
  `bitget` (demo futures), `sim` (replay of the last trading day, or the
  synthetic `demo` day where every stage fires).
