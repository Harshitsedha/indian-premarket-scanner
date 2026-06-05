"""
Trade expectancy and performance metrics for the backtester.

Public API:
    summary = compute_summary(trades)   -> dict
    print_summary(summary, label="")
    write_summary(summary, path, label="")

All division is guarded:
  - No trades  -> every numeric field is 0 or None, no crash
  - No losses  -> profit_factor = math.inf, labelled "infinite (no losses)"
  - No wins    -> avg_win is None, expectancy uses 0 for the win term
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtest.engine import ClosedTrade


def compute_summary(trades: list) -> dict:
    """
    Compute expectancy statistics from a list of ClosedTrade objects.

    Returns a dict with keys:
        total_trades      int
        wins              int
        losses            int
        win_rate          float  0..1
        avg_win_pct       float | None   (None when no winning trades)
        avg_loss_pct      float | None   (None when no losing trades)
        avg_trade_pct     float
        expectancy_pct    float  win_rate * avg_win + loss_rate * avg_loss
        profit_factor     float | math.inf | None
        max_drawdown_pct  float  (most negative single-trade pnl_pct; 0 if no trades)
        exit_counts       dict[str, int]
    """
    if not trades:
        return {
            "total_trades":    0,
            "wins":            0,
            "losses":          0,
            "win_rate":        0.0,
            "avg_win_pct":     None,
            "avg_loss_pct":    None,
            "avg_trade_pct":   0.0,
            "expectancy_pct":  0.0,
            "profit_factor":   None,
            "max_drawdown_pct": 0.0,
            "exit_counts":     {},
        }

    n       = len(trades)
    wins    = [t for t in trades if t.pnl_abs > 0]
    losses  = [t for t in trades if t.pnl_abs <= 0]

    win_rate  = len(wins) / n          # fraction 0..1
    loss_rate = 1.0 - win_rate

    # Average per-leg returns (None when the bucket is empty)
    avg_win_pct  = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else None
    avg_loss_pct = sum(t.pnl_pct for t in losses)  / len(losses) if losses else None

    # Arithmetic mean across all trades
    avg_trade_pct = sum(t.pnl_pct for t in trades) / n

    # Expectancy = E[P&L per trade] = win_rate*avg_win + loss_rate*avg_loss
    # Uses 0 when a bucket is missing so the formula remains well-defined.
    expectancy_pct = (
        win_rate  * (avg_win_pct  or 0.0)
        + loss_rate * (avg_loss_pct or 0.0)
    )

    # Profit factor = gross_profit / gross_loss  (monetary, not %)
    # math.inf when there are no losing trades.
    gross_profit = sum(t.pnl_abs for t in wins)
    gross_loss   = abs(sum(t.pnl_abs for t in losses))
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif wins:
        profit_factor = math.inf     # all winners, no losses
    else:
        profit_factor = None         # no trades at all (shouldn't reach here)

    # Worst single-trade P&L (0 when all trades are profitable)
    max_drawdown_pct = min((t.pnl_pct for t in trades), default=0.0)

    # Count exit reasons
    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    return {
        "total_trades":    n,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        win_rate,
        "avg_win_pct":     avg_win_pct,
        "avg_loss_pct":    avg_loss_pct,
        "avg_trade_pct":   avg_trade_pct,
        "expectancy_pct":  expectancy_pct,
        "profit_factor":   profit_factor,
        "max_drawdown_pct": max_drawdown_pct,
        "exit_counts":     exit_counts,
    }


def _format_summary(summary: dict, label: str = "") -> str:
    n = summary["total_trades"]
    if n == 0:
        return ("No trades." if not label else f"No trades — {label}")

    wins   = summary["wins"]
    losses = summary["losses"]
    wr     = summary["win_rate"] * 100

    avg_win  = summary["avg_win_pct"]
    avg_loss = summary["avg_loss_pct"]

    pf = summary["profit_factor"]
    if pf is math.inf or pf == math.inf:
        pf_str = "infinite (no losses)"
    elif pf is None:
        pf_str = "n/a"
    else:
        pf_str = f"{pf:.2f}"

    exit_counts = summary["exit_counts"]
    # Sort by count descending so the dominant exit reason comes first
    exit_lines = "\n".join(
        f"    {reason:14s}: {count:4d}  ({count / n * 100:.1f}%)"
        for reason, count in sorted(exit_counts.items(), key=lambda x: -x[1])
    )

    sep = "=" * 52
    dash = "-" * 52
    lines = [sep]
    if label:
        lines.append(f"  {label}")
        lines.append(sep)
    lines += [
        f"  Trades         : {n:4d}",
        f"  Win rate       : {wr:5.1f}%  ({wins} wins, {losses} losses)",
        dash,
        (f"  Avg win        : {avg_win:+.2f}%"  if avg_win  is not None
         else "  Avg win        : n/a"),
        (f"  Avg loss       : {avg_loss:+.2f}%" if avg_loss is not None
         else "  Avg loss       : n/a"),
        f"  Avg trade      : {summary['avg_trade_pct']:+.2f}%",
        dash,
        f"  Expectancy     : {summary['expectancy_pct']:+.2f}%  per trade",
        f"  Profit factor  : {pf_str}",
        f"  Max drawdown   : {summary['max_drawdown_pct']:+.2f}%  (single trade)",
        dash,
        "  Exit reasons   :",
        exit_lines,
        sep,
    ]
    return "\n".join(lines)


def print_summary(summary: dict, label: str = "") -> None:
    """Print expectancy summary to stdout."""
    print(_format_summary(summary, label))


def write_summary(summary: dict, path: Path, label: str = "") -> None:
    """Write expectancy summary to a text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_summary(summary, label) + "\n", encoding="utf-8")
