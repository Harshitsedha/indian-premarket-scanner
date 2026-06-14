"""
Trade recorder: accumulate ClosedTrade objects and write a CSV.

Output columns (canonical — defined ONCE in _trade_to_dict):
    symbol, entry_date, entry_time, entry_price,
    exit_date, exit_time, exit_price,
    side, gap_pct, bars_held, pnl_abs, pnl_pct, mfe_pct, mae_pct, exit_reason

All callers that need to serialize a ClosedTrade must go through _trade_to_dict
so the column list and the field mapping can never drift apart.
"""
from __future__ import annotations

import csv
from pathlib import Path

from loguru import logger

# All engine imports go through the explicit path so this module is importable
# even when the working directory is not pipeline/.
from backtest.engine import ClosedTrade

_RESULTS_DIR = Path(__file__).parent / "results"

# Single source of truth for output column order.
TRADE_COLUMNS = [
    "symbol", "entry_date", "entry_time", "entry_price",
    "exit_date", "exit_time", "exit_price", "side", "gap_pct",
    "bars_held", "pnl_abs", "pnl_pct", "mfe_pct", "mae_pct", "exit_reason",
]


def _trade_to_dict(t: ClosedTrade) -> dict:
    """
    Canonical serializer for a ClosedTrade.  write_csv and any future
    DataFrame-based export must use this function — never duplicate the
    field mapping inline.
    """
    return {
        "symbol":      t.symbol,
        "entry_date":  t.entry_date,
        "entry_time":  t.entry_time,
        "entry_price": t.entry_price,
        "exit_date":   t.exit_date,
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
    }


def write_csv(
    trades: list[ClosedTrade],
    path: Path | None = None,
    *,
    context_cols: bool = False,
) -> Path:
    """
    Write trades to CSV.

    Args:
        trades: list of ClosedTrade from engine.run()
        path:   explicit output path; if None, auto-generates under results/
        context_cols: when True, append one column per distinct key found in any
                      trade's .context (first-seen order), unioned across all
                      trades. Missing keys for a given trade are left blank.
                      Keys colliding with canonical TRADE_COLUMNS are ignored so
                      the engine's authoritative fields can never be overwritten.
                      Default False keeps the report at the canonical column set.

    Returns:
        Path to the written file.
    """
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if path is None:
        raise ValueError("path must be provided (use run.py to auto-generate the name)")

    extra_cols: list[str] = []
    if context_cols:
        seen: set[str] = set()
        for t in trades:
            for k in t.context:
                if k not in seen and k not in TRADE_COLUMNS:
                    seen.add(k)
                    extra_cols.append(k)

    fieldnames = TRADE_COLUMNS + extra_cols

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            row = _trade_to_dict(t)
            if extra_cols:
                row.update({k: v for k, v in t.context.items() if k in extra_cols})
            writer.writerow(row)

    logger.info(f"Wrote {len(trades)} trades -> {path}")
    return path
