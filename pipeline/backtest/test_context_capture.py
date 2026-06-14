"""
pytest tests for arbitrary decision-time context capture.

Covers:
  - context flows Signal -> ClosedTrade -> report CSV
  - strategies that record no context still work (empty + off-by-default report)
  - two strategies with different keys produce a unioned column set
  - HARD CONSTRAINT: context is a pure pre-entry snapshot — a value derived from
    any bar at/after the entry timestamp makes the test FAIL.

Run from pipeline/:
    pytest backtest/test_context_capture.py -v
"""
from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from backtest.engine import run as engine_run
from backtest.recorder import TRADE_COLUMNS, write_csv
from backtest.strategy import Action, BarContext, Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _single_day_candles(highs: list[float] | None = None, n: int = 6) -> pd.DataFrame:
    """One trading day, n one-minute bars. open/low/close flat; high overridable."""
    day = pd.Timestamp(date(2026, 1, 5))
    rows = []
    for m in range(n):
        ts = pd.Timestamp(datetime(2026, 1, 5, 9, 15 + m))
        rows.append({
            "timestamp": ts, "date": day,
            "open": 100.0,
            "high": (highs[m] if highs is not None else 101.0),
            "low": 99.0, "close": 100.0, "volume": 1000, "oi": 0.0,
        })
    return pd.DataFrame(rows)


class _EnterOnBarN:
    """
    Enters LONG when the day reaches bar `bar_n`, attaching context_fn(ctx) to
    the Signal. stop is well below price so the fill always succeeds.
    """
    eod_exit = True

    def __init__(self, bar_n: int, context_fn) -> None:
        self.bar_n = bar_n
        self.context_fn = context_fn
        self._entered = False

    def reset_day(self) -> None:
        self._entered = False

    def on_bar(self, ctx: BarContext) -> Signal | None:
        if self._entered or ctx.position is not None:
            return None
        today = ctx.current["date"]
        n = int((ctx.bars["date"] == today).sum())
        if n == self.bar_n:
            self._entered = True
            return Signal(
                action=Action.ENTER_LONG,
                stop_price=float(ctx.current["low"]) - 5.0,
                target_r=2.0,
                context=self.context_fn(ctx),
            )
        return None


# ── 1. Flow: Signal -> ClosedTrade -> report ───────────────────────────────────

def test_context_flows_signal_to_trade_to_report(tmp_path) -> None:
    candles = _single_day_candles()
    strat = _EnterOnBarN(bar_n=3, context_fn=lambda ctx: {"foo": 1.5, "bar": 2.5})
    trades = engine_run(candles, strat, "TEST")

    assert len(trades) == 1
    assert trades[0].context == {"foo": 1.5, "bar": 2.5}

    out = tmp_path / "trades.csv"
    write_csv(trades, out, context_cols=True)

    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["foo"] == "1.5"
    assert rows[0]["bar"] == "2.5"
    # Canonical fields untouched alongside context columns
    assert rows[0]["symbol"] == "TEST"


# ── 2. No-context strategy still works ──────────────────────────────────────────

def test_no_context_strategy_is_clean(tmp_path) -> None:
    candles = _single_day_candles()
    strat = _EnterOnBarN(bar_n=3, context_fn=lambda ctx: {})
    trades = engine_run(candles, strat, "TEST")

    assert len(trades) == 1
    assert trades[0].context == {}

    # Default report: exactly the canonical columns.
    default_path = tmp_path / "default.csv"
    write_csv(trades, default_path)
    with open(default_path, newline="", encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == TRADE_COLUMNS

    # Even with context_cols=True, no keys -> no extra columns.
    ctx_path = tmp_path / "ctx.csv"
    write_csv(trades, ctx_path, context_cols=True)
    with open(ctx_path, newline="", encoding="utf-8") as fh:
        assert next(csv.reader(fh)) == TRADE_COLUMNS


# ── 3. Two strategies, different keys -> unioned columns ────────────────────────

def test_differing_context_keys_union_into_columns(tmp_path) -> None:
    trades_a = engine_run(
        _single_day_candles(),
        _EnterOnBarN(bar_n=3, context_fn=lambda ctx: {"a1": 10.0, "a2": 20.0}),
        "AAA",
    )
    trades_b = engine_run(
        _single_day_candles(),
        _EnterOnBarN(bar_n=3, context_fn=lambda ctx: {"b1": 99.0}),
        "BBB",
    )
    assert len(trades_a) == 1 and len(trades_b) == 1

    out = tmp_path / "combined.csv"
    write_csv(trades_a + trades_b, out, context_cols=True)

    with open(out, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        rows = list(reader)

    assert header == TRADE_COLUMNS + ["a1", "a2", "b1"]
    row_a = next(r for r in rows if r["symbol"] == "AAA")
    row_b = next(r for r in rows if r["symbol"] == "BBB")
    assert row_a["a1"] == "10.0" and row_a["a2"] == "20.0" and row_a["b1"] == ""
    assert row_b["b1"] == "99.0" and row_b["a1"] == "" and row_b["a2"] == ""


# ── 4. HARD CONSTRAINT: context is a pre-entry snapshot, no look-ahead ──────────

def test_context_has_no_lookahead_past_entry() -> None:
    """
    Decision is at bar idx 2 (09:17); fill is at idx 3 (09:18). Bars from idx 3
    onward carry high=99999. A context value that captured any bar at/after the
    entry timestamp — or an engine that recomputed context from the full series —
    would surface 99999 (or a later timestamp), failing the assertions below.
    """
    candles = _single_day_candles(highs=[101, 101, 101, 99999, 99999, 99999])

    def snapshot(ctx: BarContext) -> dict:
        return {
            "max_high_so_far": round(float(ctx.bars["high"].max()), 4),
            "bars_seen": float(len(ctx.bars)),
            "decision_ts": str(pd.Timestamp(ctx.current["timestamp"])),
        }

    strat = _EnterOnBarN(bar_n=3, context_fn=snapshot)
    trades = engine_run(candles, strat, "TEST")

    assert len(trades) == 1
    ctx = trades[0].context

    # Captured at the decision bar only — the post-entry 99999 spike is invisible.
    assert ctx["max_high_so_far"] == 101.0, (
        f"look-ahead: post-entry high leaked into context ({ctx['max_high_so_far']})"
    )
    assert ctx["max_high_so_far"] != 99999.0
    assert ctx["bars_seen"] == 3.0

    # The snapshot timestamp must be strictly before the entry (fill) timestamp.
    entry_ts = pd.Timestamp(f"{trades[0].entry_date} {trades[0].entry_time}")
    assert pd.Timestamp(ctx["decision_ts"]) < entry_ts, (
        "context timestamp is at/after entry — not a pre-entry snapshot"
    )
