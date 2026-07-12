"""The tiered entry / exit state machine, evaluated per symbol.

Rule map (from the strategy spec, local times converted to New York time):
  Gate    - 2nd hourly candle must be green with volume above candle 1.
  Tier A  - 30%: MACD histogram green & rising, uptrend, small-but-rising volume.
  Tier B  - 50%: candle 2 closes full-body green, histogram rising, volume up.
  Tier C  - 20%: start of candle 3 with volume up and histogram green & rising.
  Exit 70% - green histogram plateaus, then prints a lower bar.
  Exit 30% - trend flips down, volume drops, histogram bar lower than previous.
  Rule 10  - bid/ask quote sizes must each be <= quote_size_max shares.
  Rule 12  - a volume dip on candle 3 or 5 with the histogram still green is
             treated as a fake-out: the reversal stop is suppressed there.
Every buy must additionally pass the VMAR and EMA 9/20 validation filters.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from .config import Settings
from .indicators import (
    hist_green_and_rising,
    hist_plateau_then_drop,
    is_full_body,
    is_green,
    trend_down,
    trend_up,
)

log = logging.getLogger(__name__)


@dataclass
class Action:
    side: str          # "buy" | "sell"
    fraction: float    # of allocation (buy) or of current position (sell)
    reason: str


@dataclass
class SymbolState:
    gate_passed: bool = False
    tiers_filled: set[str] = field(default_factory=set)
    partial_exit_done: bool = False
    done_for_day: bool = False
    day: date | None = None

    def reset_if_new_day(self, today: date) -> None:
        if self.day != today:
            self.__init__(day=today)


def evaluate(
    bars: pd.DataFrame,
    state: SymbolState,
    settings: Settings,
    now: datetime,
    has_position: bool,
    quote_sizes: tuple[float, float] | None = None,
) -> list[Action]:
    """Return the actions the strategy calls for right now.

    `bars` is the multi-day hourly frame with indicator columns; the last row
    may be a still-forming candle. All tier/exit rules are evaluated on closed
    candles only.
    """
    today = now.date()
    state.reset_if_new_day(today)
    if state.done_for_day or bars.empty:
        return []

    closed = bars[bars.index + timedelta(hours=1) <= now]
    if closed.empty:
        return []
    i = len(closed) - 1
    last = closed.iloc[i]

    today_closed = closed[pd.Index(closed.index.date) == today]
    candles = {int(r["candle_no"]): r for _, r in today_closed.iterrows()}

    actions: list[Action] = []

    # ------------------------------------------------------------ exits ----
    if has_position:
        if now.time() >= settings.flatten_at:
            state.done_for_day = True
            return [Action("sell", 1.0, "end of session flatten")]

        if not state.partial_exit_done and hist_plateau_then_drop(
            closed, i, settings.plateau_tolerance
        ):
            state.partial_exit_done = True
            actions.append(
                Action("sell", settings.partial_exit_pct,
                       "MACD histogram plateaued then dropped")
            )

        vol_dropping = i >= 1 and last["volume"] < closed["volume"].iloc[i - 1]
        hist_lower = i >= 1 and last["macd_hist"] < closed["macd_hist"].iloc[i - 1]
        if trend_down(closed, i) and vol_dropping and hist_lower:
            # Rule 12: volume dips on candles 3 and 5 are often fake-outs —
            # while the MACD histogram is still green, hold instead of stopping.
            if int(last["candle_no"]) in (3, 5) and last["macd_hist"] > 0:
                log.info("volume dip on candle %d with green histogram — "
                         "treated as fake-out, reversal stop suppressed",
                         int(last["candle_no"]))
            else:
                state.done_for_day = True
                actions.append(Action("sell", 1.0, "trend reversal stop"))
                return actions

    # ------------------------------------------------------------ entry ----
    if last["close"] <= 0 or now.time() >= settings.flatten_at:
        return actions

    c1, c2 = candles.get(1), candles.get(2)

    if not state.gate_passed and c1 is not None and c2 is not None:
        if is_green(c2) and c2["volume"] > c1["volume"]:
            state.gate_passed = True
            log.info("entry gate passed: candle 2 green with volume above candle 1")

    if not state.gate_passed:
        return actions

    # Final validation applied to every buy: VMAR + EMA 9/20 (spec) and the
    # rule-10 quote-size cap (skipped when the venue can't provide quotes).
    quote_ok = quote_sizes is None or (
        quote_sizes[0] <= settings.quote_size_max
        and quote_sizes[1] <= settings.quote_size_max
    )
    if not quote_ok:
        log.info("rule 10: bid/ask size %s exceeds %.0f — buys blocked",
                 quote_sizes, settings.quote_size_max)
    validated = quote_ok and trend_up(closed, i) and last["vmar"] >= settings.vmar_min

    def try_buy(tier: str, fraction: float, condition: bool, reason: str) -> None:
        if tier in state.tiers_filled or not condition:
            return
        if not validated:
            log.info("tier %s signal fired but failed VMAR/EMA validation", tier)
            return
        state.tiers_filled.add(tier)
        actions.append(Action("buy", fraction, reason))

    hist_ok = hist_green_and_rising(closed, i)
    vol_rising = i >= 1 and last["volume"] > closed["volume"].iloc[i - 1]

    # Tier A's "sales volume <= 20k" is the rule-10 quote-size cap, which is
    # already part of `validated` above.
    try_buy(
        "A", settings.tier_a_pct,
        hist_ok and trend_up(closed, i) and vol_rising,
        "tier A: histogram rising in uptrend with volume momentum",
    )

    try_buy(
        "B", settings.tier_b_pct,
        c2 is not None
        and is_green(c2) and is_full_body(c2, settings.full_body_min_ratio)
        and hist_ok and c2["volume"] > (c1["volume"] if c1 is not None else 0),
        "tier B: candle 2 closed full-body green with rising histogram/volume",
    )

    # Tier C fires once candle 3 has started forming (it appears as the
    # still-open last row of `bars`, or is already closed).
    candle3_started = bool(
        (bars[pd.Index(bars.index.date) == today]["candle_no"] >= 3).any()
    )
    try_buy(
        "C", settings.tier_c_pct,
        candle3_started and hist_ok and vol_rising,
        "tier C: candle 3 opening with volume up and histogram rising",
    )

    return actions
