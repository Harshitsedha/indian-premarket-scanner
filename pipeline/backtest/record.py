"""
Candidate recorder — thin wrapper over the frozen engine.

Identifies qualifying setup-days (gap condition met), snapshots feature values
at the entry-window decision point, and attaches the engine's outcome.

The engine is the sole authority on trade outcomes; this module never
re-derives pnl, MFE/MAE, or exit_reason.

Public API:
    trades, candidates = run_and_record(candles, strategy, symbol, resolved_features)
    write_candidates(candidates, resolved_features, path)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from loguru import logger

# Frozen — never modify these imports' targets
from backtest.engine import ClosedTrade, run as engine_run
from backtest.features import ResolvedFeature
from backtest.recorder import _RESULTS_DIR
from backtest.strategy import BarContext, GapAndGo


@dataclass
class CandidateRow:
    symbol:         str
    date:           str    # "YYYY-MM-DD" of the setup day
    decision_time:  str    # timestamp of bar entry_window_min (e.g. "2026-05-14 10:15:00")
    taken:          bool   # True if a breakout/fill happened this day
    feature_values: dict   # col_name -> float | None
    pnl_pct:        float  # 0.0 for flat (no trade)
    mfe_pct:        float  # 0.0 for flat
    mae_pct:        float  # 0.0 for flat
    exit_reason:    str    # "flat" when no trade; engine reason otherwise
    label:          str    # "win" | "loss" | "flat"


def run_and_record(
    candles:           pd.DataFrame,
    strategy:          GapAndGo,
    symbol:            str,
    resolved_features: list[ResolvedFeature],
) -> tuple[list[ClosedTrade], list[CandidateRow]]:
    """
    Run the frozen engine, then overlay qualifying-day detection.

    Qualifying-day detection mirrors GapAndGo's first-bar logic but is done
    independently from the candle series so the engine's replay is untouched.

    Decision point: bar `strategy.entry_window_min` (0-indexed within the day).
    Features are computed from candles.iloc[:decision_bar_global_idx + 1].
    Outcomes come from the engine's ClosedTrade list, matched by date string.
    """
    trades = engine_run(candles, strategy, symbol)
    trade_by_date: dict[str, ClosedTrade] = {t.date: t for t in trades}

    candidates: list[CandidateRow] = []
    dates = sorted(candles["date"].unique())

    for d in dates:
        # -- Find this day's rows in the full candle array --------------------
        day_mask   = candles["date"] == d
        day_bars   = candles[day_mask]
        day_global = day_bars.index.tolist()   # absolute positions in candles

        # -- Prev-close (mirrors BarContext.prev_close) -----------------------
        prev_bars = candles[candles["date"] < d]
        if prev_bars.empty:
            continue   # first day in range: no prior close → cannot compute gap
        prev_close_val = float(prev_bars.iloc[-1]["close"])
        if prev_close_val <= 0:
            continue

        # -- Gap check (mirrors GapAndGo._is_first_bar) ----------------------
        first_open = float(day_bars.iloc[0]["open"])
        gap        = (first_open - prev_close_val) / prev_close_val * 100
        if gap < strategy.min_gap_pct:
            continue   # day does not qualify

        # -- Decision-point bar: entry_window_min bars into the session -------
        dp_local = strategy.entry_window_min  # 0-indexed within the day
        if len(day_global) <= dp_local:
            # Session ended before the window closed (early halt etc.) — skip
            continue

        dp_global = day_global[dp_local]       # absolute index in candles
        dp_ts     = pd.Timestamp(candles.iloc[dp_global]["timestamp"])
        decision_time = dp_ts.strftime("%Y-%m-%d %H:%M:%S")

        # -- Feature context: visible slice up to and including the dp bar ---
        # position=None because features read only ctx.bars (not ctx.position)
        ctx = BarContext(bars=candles.iloc[: dp_global + 1], position=None)

        feature_values: dict = {}
        for rf in resolved_features:
            try:
                v = rf.spec.compute(ctx, rf.params)
            except Exception as exc:
                logger.warning(f"{symbol} {d}: feature {rf.col_name} error: {exc}")
                v = None
            feature_values[rf.col_name] = v

        # -- Outcome from the engine's trade record --------------------------
        date_str = dp_ts.strftime("%Y-%m-%d")
        trade    = trade_by_date.get(date_str)

        if trade is not None:
            taken       = True
            pnl_pct     = trade.pnl_pct
            mfe_pct     = trade.mfe_pct
            mae_pct     = trade.mae_pct
            exit_reason = trade.exit_reason
            label       = "win" if trade.pnl_abs > 0 else "loss"
        else:
            taken       = False
            pnl_pct     = 0.0
            mfe_pct     = 0.0
            mae_pct     = 0.0
            exit_reason = "flat"
            label       = "flat"

        candidates.append(CandidateRow(
            symbol        = symbol,
            date          = date_str,
            decision_time = decision_time,
            taken         = taken,
            feature_values= feature_values,
            pnl_pct       = pnl_pct,
            mfe_pct       = mfe_pct,
            mae_pct       = mae_pct,
            exit_reason   = exit_reason,
            label         = label,
        ))

    return trades, candidates


def write_candidates(
    candidates:        list[CandidateRow],
    resolved_features: list[ResolvedFeature],
    path:              Path,
) -> Path:
    """Write candidate rows to CSV. Follows the same conventions as recorder.write_csv."""
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    fieldnames = (
        ["symbol", "date", "decision_time", "taken"]
        + [rf.col_name for rf in resolved_features]
        + ["pnl_pct", "exit_reason", "mfe_pct", "mae_pct", "label"]
    )

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in candidates:
            r: dict = {
                "symbol":        row.symbol,
                "date":          row.date,
                "decision_time": row.decision_time,
                "taken":         row.taken,
            }
            r.update(row.feature_values)
            r.update({
                "pnl_pct":     row.pnl_pct,
                "exit_reason": row.exit_reason,
                "mfe_pct":     row.mfe_pct,
                "mae_pct":     row.mae_pct,
                "label":       row.label,
            })
            writer.writerow(r)

    logger.info(f"Wrote {len(candidates)} candidates -> {path}")
    return path
