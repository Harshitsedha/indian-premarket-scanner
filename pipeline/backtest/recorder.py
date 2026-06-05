"""
Trade recorder: accumulate ClosedTrade objects and write a CSV.

Output columns (matches user spec exactly):
    symbol, date, entry_time, entry_price, exit_time, exit_price,
    side, gap_pct, bars_held, pnl_abs, pnl_pct, mfe_pct, mae_pct, exit_reason
"""
from __future__ import annotations

import csv
from pathlib import Path

from loguru import logger

# All engine imports go through the explicit path so this module is importable
# even when the working directory is not pipeline/.
from backtest.engine import ClosedTrade

_RESULTS_DIR = Path(__file__).parent / "results"

_COLUMNS = [
    "symbol", "date", "entry_time", "entry_price",
    "exit_time", "exit_price", "side", "gap_pct",
    "bars_held", "pnl_abs", "pnl_pct", "mfe_pct", "mae_pct", "exit_reason",
]


def write_csv(trades: list[ClosedTrade], path: Path | None = None) -> Path:
    """
    Write trades to CSV.

    Args:
        trades: list of ClosedTrade from engine.run()
        path:   explicit output path; if None, auto-generates under results/

    Returns:
        Path to the written file.
    """
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if path is None:
        raise ValueError("path must be provided (use run.py to auto-generate the name)")

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        for t in trades:
            writer.writerow({
                "symbol":      t.symbol,
                "date":        t.date,
                "entry_time":  t.entry_time,
                "entry_price": t.entry_price,
                "exit_time":   t.exit_time,
                "exit_price":  t.exit_price,
                "side":        t.side,
                "gap_pct":     t.gap_pct,
                "bars_held":   t.bars_held,
                "pnl_abs":     t.pnl_abs,
                "pnl_pct":     t.pnl_pct,
                "mfe_pct":     t.mfe_pct,
                "mae_pct":     t.mae_pct,
                "exit_reason": t.exit_reason,
            })

    logger.info(f"Wrote {len(trades)} trades → {path}")
    return path
