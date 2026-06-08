"""
Load a backtest result CSV and compute the full statistics dict for the AI report.

All arithmetic happens here — Claude only receives the finished numbers.

Public API:
    stats = load_and_compute(csv_path)  -> dict (JSON-serialisable)

Reuses backtest.metrics.compute_summary for the core metrics (win rate,
expectancy, profit factor) and adds:
  - total_pnl_abs, payoff_ratio
  - exit_breakdown: count + pnl contribution per reason
  - avg MFE on winners, avg MAE on losers (absolute), give_back count
  - bars_held avg, median, quick (<=15) vs long/eod (>15) split
  - max consecutive wins and losses
  - gap_pct performance buckets
"""
from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path
from types import SimpleNamespace

from backtest.metrics import compute_summary

_QUICK_BARS          = 15    # <= 15 bars (15 min on 1-min candles) = quick scalp
_GIVEBACK_THRESHOLD  = 0.5   # MFE >= 0.5% then closed <=0 = give-back trade


def _safe_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


def load_and_compute(csv_path: str | Path) -> dict:
    """
    Load a trade result CSV (schema: symbol, entry_date, entry_time,
    entry_price, exit_date, exit_time, exit_price, side, gap_pct,
    bars_held, pnl_abs, pnl_pct, mfe_pct, mae_pct, exit_reason)
    and return a JSON-serialisable stats dict.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Result CSV not found: {path}")

    trades = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            trades.append(SimpleNamespace(
                pnl_abs    = _safe_float(row.get("pnl_abs")),
                pnl_pct    = _safe_float(row.get("pnl_pct")),
                exit_reason= (row.get("exit_reason") or "unknown").strip(),
                mfe_pct    = _safe_float(row.get("mfe_pct")),
                mae_pct    = _safe_float(row.get("mae_pct")),
                bars_held  = _safe_int(row.get("bars_held")),
                gap_pct    = _safe_float(row.get("gap_pct")),
            ))

    if not trades:
        return {"total_trades": 0, "note": "No trades in result CSV."}

    # ── Core metrics (reuse existing compute_summary) ─────────────────────────
    base = compute_summary(trades)

    # Sanitise math.inf (not JSON-serialisable)
    pf = base.get("profit_factor")
    if pf is not None and math.isinf(pf):
        base["profit_factor"]      = None
        base["profit_factor_note"] = "infinite (no losing trades)"

    # ── Additional metrics ────────────────────────────────────────────────────
    n       = len(trades)
    winners = [t for t in trades if t.pnl_abs > 0]
    losers  = [t for t in trades if t.pnl_abs <= 0]

    total_pnl_abs = round(sum(t.pnl_abs for t in trades), 4)

    avg_win  = base.get("avg_win_pct")
    avg_loss = base.get("avg_loss_pct")
    payoff_ratio = (
        round(avg_win / abs(avg_loss), 4)
        if avg_win is not None and avg_loss is not None and avg_loss != 0
        else None
    )

    # Exit reason breakdown: count + pnl contribution
    _exit: dict[str, dict] = {}
    for t in trades:
        r = t.exit_reason
        if r not in _exit:
            _exit[r] = {"count": 0, "total_pnl_abs": 0.0, "pnl_pct_sum": 0.0, "wins": 0}
        _exit[r]["count"]       += 1
        _exit[r]["total_pnl_abs"] += t.pnl_abs
        _exit[r]["pnl_pct_sum"] += t.pnl_pct
        if t.pnl_abs > 0:
            _exit[r]["wins"] += 1

    exit_breakdown = {}
    for r, d in _exit.items():
        c = d["count"]
        exit_breakdown[r] = {
            "count":        c,
            "pct_of_total": round(c / n * 100, 1),
            "wins":         d["wins"],
            "win_rate":     round(d["wins"] / c * 100, 1) if c else None,
            "total_pnl_abs":round(d["total_pnl_abs"], 4),
            "avg_pnl_pct":  round(d["pnl_pct_sum"] / c, 4) if c else None,
        }

    # MFE / MAE analysis
    avg_mfe_winners = (
        round(sum(t.mfe_pct for t in winners) / len(winners), 4) if winners else None
    )
    avg_mae_losers = (
        round(sum(t.mae_pct for t in losers) / len(losers), 4) if losers else None
    )
    give_back_count = sum(
        1 for t in trades if t.mfe_pct >= _GIVEBACK_THRESHOLD and t.pnl_abs <= 0
    )

    # Bars held
    bars        = [t.bars_held for t in trades]
    avg_bars    = round(sum(bars) / n, 2)
    med_bars    = round(statistics.median(bars), 1)
    quick_count = sum(1 for b in bars if b <= _QUICK_BARS)
    long_count  = n - quick_count

    # Consecutive wins / losses (preserving trade order from CSV)
    max_cw = max_cl = cur_w = cur_l = 0
    for t in trades:
        if t.pnl_abs > 0:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_cw = max(max_cw, cur_w)
        max_cl = max(max_cl, cur_l)

    # Gap-pct performance buckets
    gap_buckets: dict[str, dict] = {
        "<1pct":  {"count": 0, "wins": 0},
        "1_3pct": {"count": 0, "wins": 0},
        "3_5pct": {"count": 0, "wins": 0},
        ">5pct":  {"count": 0, "wins": 0},
    }
    for t in trades:
        g = t.gap_pct
        key = "<1pct" if g < 1 else "1_3pct" if g < 3 else "3_5pct" if g < 5 else ">5pct"
        gap_buckets[key]["count"] += 1
        if t.pnl_abs > 0:
            gap_buckets[key]["wins"] += 1
    for bkt in gap_buckets.values():
        c = bkt["count"]
        bkt["win_rate_pct"] = round(bkt["wins"] / c * 100, 1) if c else None

    # ── Assemble final dict ───────────────────────────────────────────────────
    result = {
        # Core (from compute_summary, with inf sanitised)
        "total_trades":    base["total_trades"],
        "wins":            base["wins"],
        "losses":          base["losses"],
        "win_rate_pct":    round(base["win_rate"] * 100, 2),
        "avg_win_pct":     base["avg_win_pct"],
        "avg_loss_pct":    base["avg_loss_pct"],
        "avg_trade_pct":   base["avg_trade_pct"],
        "expectancy_pct":  base["expectancy_pct"],
        "profit_factor":   base["profit_factor"],
        "max_drawdown_pct": base["max_drawdown_pct"],
        # Additional
        "total_pnl_abs":          total_pnl_abs,
        "payoff_ratio":           payoff_ratio,
        "exit_breakdown":         exit_breakdown,
        "avg_mfe_winners_pct":    avg_mfe_winners,
        "avg_mae_losers_pct":     avg_mae_losers,
        "give_back_count":        give_back_count,
        "give_back_threshold_pct": _GIVEBACK_THRESHOLD,
        "bars_held_avg":          avg_bars,
        "bars_held_median":       med_bars,
        "quick_trades_lte15bars": quick_count,
        "long_trades_gt15bars":   long_count,
        "max_consecutive_wins":   max_cw,
        "max_consecutive_losses": max_cl,
        "gap_pct_buckets":        gap_buckets,
    }
    if "profit_factor_note" in base:
        result["profit_factor_note"] = base["profit_factor_note"]

    return result
