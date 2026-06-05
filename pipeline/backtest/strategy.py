"""
Strategy interface and built-in implementations for the bar-by-bar backtester.

Protocol:
    class MyStrategy:
        eod_exit: bool = True          # engine reads this to force EOD close
        def reset_day(self) -> None: ...
        def on_bar(self, ctx: BarContext) -> Signal | None: ...

BarContext exposes only bars[0..i] (no look-ahead) and the current open position.
The engine, not the strategy, owns fills, MFE/MAE, and P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import pandas as pd


# ── Signal types ──────────────────────────────────────────────────────────────

class Action(Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT       = "EXIT"


@dataclass
class Signal:
    action:      Action
    stop_price:  float | None = None   # absolute price; engine computes target from this
    target_r:    float        = 2.0    # risk multiple for target (engine applies after fill)
    gap_pct:     float        = 0.0    # passed through to trade record
    reason:      str          = ""


# ── Context ───────────────────────────────────────────────────────────────────

class BarContext:
    """
    Immutable view of the world at bar i.
    bars = candles.iloc[:i+1]  — strictly no future rows.
    """

    def __init__(self, bars: pd.DataFrame, position: object | None) -> None:
        self.bars     = bars
        self.position = position   # _OpenPosition | None; opaque to the strategy

    @property
    def current(self) -> pd.Series:
        return self.bars.iloc[-1]

    def prev_close(self) -> float | None:
        """Last close of the trading session before today. None on the first day."""
        today    = self.current["date"]
        prev     = self.bars[self.bars["date"] < today]
        if prev.empty:
            return None
        return float(prev.iloc[-1]["close"])

    def opening_range_high(self, n_min: int) -> float | None:
        """
        Max high of the first n_min bars of today.
        Returns None if fewer than n_min bars of today are visible yet.
        """
        today_bars = self.bars[self.bars["date"] == self.current["date"]]
        if len(today_bars) < n_min:
            return None
        return float(today_bars.head(n_min)["high"].max())

    def opening_range_low(self, n_min: int) -> float | None:
        """
        Min low of the first n_min bars of today.
        Returns None if fewer than n_min bars of today are visible yet.
        """
        today_bars = self.bars[self.bars["date"] == self.current["date"]]
        if len(today_bars) < n_min:
            return None
        return float(today_bars.head(n_min)["low"].min())


# ── Protocol ──────────────────────────────────────────────────────────────────

class Strategy(Protocol):
    eod_exit: bool

    def reset_day(self) -> None:
        """Called by the engine at every day boundary before the first bar."""
        ...

    def on_bar(self, ctx: BarContext) -> Signal | None:
        """
        Called once per bar, strictly in chronological order.
        ctx.bars contains only bars up to and including the current index.
        """
        ...


# ── GapAndGo ──────────────────────────────────────────────────────────────────

class GapAndGo:
    """
    Gap-and-go momentum strategy for NSE equities.

    Entry logic:
      1. First bar of day: compute gap = (open - prev_close) / prev_close * 100
      2. If gap >= min_gap_pct: start tracking the opening range
      3. After opening_range_min bars complete: OR is locked (high/low captured)
      4. If the OR high is broken within entry_window_min bars of the session open
         (measured from the day's first bar, not from OR close): signal ENTER_LONG
         - stop  = OR low
         - target = fill + (fill - stop) * target_r   [computed by engine after fill]
         Once entry_window_min bars have elapsed with no breakout, the day is
         cancelled — no entry even if the OR high breaks later.

    Exit logic (engine enforced):
      - Stop hit:   bar.low  <= stop_price
      - Target hit: bar.high >= target_price
      - EOD:        last bar of trading day if eod_exit=True
    """

    def __init__(
        self,
        min_gap_pct:        float = 1.0,
        opening_range_min:  int   = 15,
        entry_window_min:   int   = 60,   # cancel watch if no breakout within this many bars of open
        stop_pct:           float = 1.0,  # kept for API symmetry; stop is OR low in practice
        target_r:           float = 2.0,
        eod_exit:           bool  = True,
    ) -> None:
        self.min_gap_pct       = min_gap_pct
        self.opening_range_min = opening_range_min
        self.entry_window_min  = entry_window_min
        self.stop_pct          = stop_pct
        self.target_r          = target_r
        self.eod_exit          = eod_exit

        # Mutable per-day state; reset by engine via reset_day()
        self._tracking:     bool  = False
        self._entered:      bool  = False
        self._gap_pct:      float = 0.0
        self._is_first_bar: bool  = False

    def reset_day(self) -> None:
        self._tracking     = False
        self._entered      = False
        self._gap_pct      = 0.0
        self._is_first_bar = True

    def on_bar(self, ctx: BarContext) -> Signal | None:
        bar = ctx.current

        # ── First bar of the day ───────────────────────────────────────────────
        if self._is_first_bar:
            self._is_first_bar = False
            prev_close = ctx.prev_close()
            if prev_close and prev_close > 0:
                gap = (float(bar["open"]) - prev_close) / prev_close * 100
                if gap >= self.min_gap_pct:
                    self._tracking = True
                    self._gap_pct  = round(gap, 4)
            return None  # never enter on the open of day 1's first bar

        # ── Gap insufficient or already entered today ──────────────────────────
        if not self._tracking or self._entered:
            return None

        # Engine owns the open position; strategy just waits when in a trade.
        if ctx.position is not None:
            return None

        # ── Opening-range not yet complete ─────────────────────────────────────
        or_high = ctx.opening_range_high(self.opening_range_min)
        or_low  = ctx.opening_range_low(self.opening_range_min)
        if or_high is None or or_low is None:
            return None

        # ── Entry window: measured from session open (bar 0), not from OR close.
        # len(today_bars) == i+1 where i is 0-indexed bar number within today.
        # When len > entry_window_min the window has closed; cancel the day.
        today_bar_count = int((ctx.bars["date"] == ctx.current["date"]).sum())
        if today_bar_count > self.entry_window_min:
            self._tracking = False   # stop checking for the rest of the session
            return None

        # ── Check for breakout ────────────────────────────────────────────────
        if float(bar["high"]) > or_high:
            self._entered = True
            return Signal(
                action     = Action.ENTER_LONG,
                stop_price = or_low,
                target_r   = self.target_r,
                gap_pct    = self._gap_pct,
                reason     = (
                    f"gap={self._gap_pct:.2f}% OR[{self.opening_range_min}] "
                    f"break {float(bar['high']):.2f}>{or_high:.2f}"
                ),
            )

        return None
