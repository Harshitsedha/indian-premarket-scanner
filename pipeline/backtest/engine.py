"""
Bar-by-bar replay engine.

Correctness guarantees:
  - Look-ahead prevention: strategy.on_bar() receives candles.iloc[:i+1] only.
    The full candles array is never exposed to the strategy in any form.
  - Fills happen at the NEXT bar's open after a signal (not the signal bar).
  - Pending entries are cancelled at every day boundary (no cross-day fills).
  - The engine (not the strategy) owns: position state, stop/target checks,
    EOD exit, MFE/MAE tracking, and P&L computation.

Processing order per bar i:
  1. Day-boundary reset (cancel pending, call strategy.reset_day())
  2. Fill pending entry at bar.open (if any)
  3. Update MFE/MAE while in position
  4. Check stop → target → EOD exit in priority order
  5. Call strategy.on_bar(ctx) with visible = candles.iloc[:i+1]
  6. Queue any ENTER_LONG signal as pending; execute EXIT signals immediately
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from loguru import logger

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from backtest.strategy import Action, BarContext, Signal, Strategy


# ── Trade record (engine output) ─────────────────────────────────────────────

@dataclass
class ClosedTrade:
    symbol:       str
    entry_date:   str    # "2026-06-03"
    entry_time:   str    # "09:45:00"
    entry_price:  float
    exit_date:    str    # "2026-06-03" (may differ from entry_date for cross-day holds)
    exit_time:    str    # "15:28:00"
    exit_price:   float
    side:         str    # "LONG"
    gap_pct:      float
    bars_held:    int
    pnl_abs:      float
    pnl_pct:      float
    mfe_pct:      float  # max favourable excursion from entry, %
    mae_pct:      float  # max adverse excursion from entry, % (negative = worse)
    exit_reason:  str
    # Decision-time indicator snapshot, copied verbatim from the entry Signal.
    # Strategy-defined keys; empty for strategies that record nothing.
    context:      dict = field(default_factory=dict)


# ── Internal position state ───────────────────────────────────────────────────

@dataclass
class _OpenPosition:
    entry_price:   float
    entry_time:    str
    entry_bar_idx: int
    stop_price:    float
    target_price:  float
    gap_pct:       float
    side:          str = "LONG"
    context:       dict = field(default_factory=dict)


# ── Engine ────────────────────────────────────────────────────────────────────

def run(
    candles:  pd.DataFrame,
    strategy: Strategy,
    symbol:   str,
) -> list[ClosedTrade]:
    """
    Replay candles through strategy, return list of closed trades.

    Args:
        candles:  chronologically sorted DataFrame from data.get_candles()
        strategy: any object satisfying the Strategy protocol
        symbol:   used for trade records only (no API calls here)
    """
    if candles.empty:
        logger.warning(f"engine.run: empty candle set for {symbol}, no trades possible")
        return []

    trades:   list[ClosedTrade] = []
    pending:  Signal | None     = None
    position: _OpenPosition | None = None
    mfe_pct:  float = 0.0
    mae_pct:  float = 0.0
    eod_exit: bool  = getattr(strategy, "eod_exit", True)

    def _close(bar: pd.Series, exit_price: float, reason: str, bar_idx: int) -> None:
        nonlocal position, mfe_pct, mae_pct
        assert position is not None
        ep      = position.entry_price
        pnl_abs = exit_price - ep
        pnl_pct = pnl_abs / ep * 100
        ts      = pd.Timestamp(bar["timestamp"])
        # Split stored full-datetime entry_time into date + clock
        entry_dt   = pd.Timestamp(position.entry_time)
        trades.append(ClosedTrade(
            symbol      = symbol,
            entry_date  = entry_dt.strftime("%Y-%m-%d"),
            entry_time  = entry_dt.strftime("%H:%M:%S"),
            entry_price = round(ep, 4),
            exit_date   = ts.strftime("%Y-%m-%d"),
            exit_time   = ts.strftime("%H:%M:%S"),
            exit_price  = round(exit_price, 4),
            side        = position.side,
            gap_pct     = round(position.gap_pct, 4),
            bars_held   = bar_idx - position.entry_bar_idx,
            pnl_abs     = round(pnl_abs, 4),
            pnl_pct     = round(pnl_pct, 4),
            mfe_pct     = round(mfe_pct, 4),
            mae_pct     = round(mae_pct, 4),
            exit_reason = reason,
            context     = dict(position.context),
        ))
        logger.debug(
            f"{symbol} CLOSE {reason}: entry={ep:.2f} exit={exit_price:.2f} "
            f"pnl={pnl_pct:+.2f}%"
        )
        position = None
        mfe_pct  = 0.0
        mae_pct  = 0.0

    for i in range(len(candles)):
        bar = candles.iloc[i]

        # ── Step 1: Day boundary ───────────────────────────────────────────────
        is_new_day = (i == 0) or (bar["date"] != candles.iloc[i - 1]["date"])
        if is_new_day:
            # A pending entry from bar i-1 (last bar of yesterday) must never
            # fill at today's open — GapAndGo is a same-session strategy.
            pending = None
            strategy.reset_day()

        # ── Step 2: Fill pending entry ─────────────────────────────────────────
        if pending is not None and position is None:
            fill = float(bar["open"])
            stop = float(pending.stop_price) if pending.stop_price is not None else 0.0
            if fill > stop:
                risk   = fill - stop
                target = fill + risk * (pending.target_r or 2.0)
                ts_str = pd.Timestamp(bar["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                position = _OpenPosition(
                    entry_price   = fill,
                    entry_time    = ts_str,
                    entry_bar_idx = i,
                    stop_price    = stop,
                    target_price  = target,
                    gap_pct       = pending.gap_pct,
                    # Snapshot from the decision bar, frozen at fill. Copied so
                    # later strategy state changes can't mutate it.
                    context       = dict(pending.context),
                )
                logger.debug(
                    f"{symbol} FILL {fill:.2f} stop={stop:.2f} "
                    f"target={target:.2f} gap={pending.gap_pct:.2f}%"
                )
            else:
                logger.debug(
                    f"{symbol} Entry cancelled: open {fill:.2f} <= stop {stop:.2f}"
                )
            pending = None

        # ── Step 3: MFE / MAE ─────────────────────────────────────────────────
        if position is not None:
            ep      = position.entry_price
            mfe_pct = max(mfe_pct, (float(bar["high"]) - ep) / ep * 100)
            mae_pct = min(mae_pct, (float(bar["low"])  - ep) / ep * 100)

        # ── Step 4: Stop / target / EOD ───────────────────────────────────────
        if position is not None:
            # Look one bar ahead ONLY to detect last-of-day; this info is
            # never passed to the strategy — it stays inside the engine.
            is_last_of_day = (
                (i + 1 >= len(candles))
                or (candles.iloc[i + 1]["date"] != bar["date"])
            )
            if float(bar["low"]) <= position.stop_price:
                _close(bar, position.stop_price, "stop_hit", i)
            elif float(bar["high"]) >= position.target_price:
                _close(bar, position.target_price, "target_hit", i)
            elif is_last_of_day and eod_exit:
                _close(bar, float(bar["close"]), "eod_exit", i)

        # ── Step 5: Strategy ──────────────────────────────────────────────────
        # CRITICAL: pass only candles.iloc[:i+1] — never i+2 or beyond.
        visible = candles.iloc[: i + 1]
        ctx     = BarContext(bars=visible, position=position)
        signal  = strategy.on_bar(ctx)

        if signal is None:
            continue
        if signal.action == Action.ENTER_LONG and position is None and pending is None:
            pending = signal
        elif signal.action == Action.EXIT and position is not None:
            _close(bar, float(bar["close"]), signal.reason or "strategy_exit", i)

    return trades
