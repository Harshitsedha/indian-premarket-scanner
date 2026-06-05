"""
pytest tests for the bar-by-bar backtester.

Tests:
  - Look-ahead prevention (3 tests): strategy never sees future bars
  - prev_close() contract (2 tests): None on first day, correct value on day 2
  - Entry-window guard (2 tests): no fill after window expires; fill within window
  - CA guard (2 tests): flags split, clean data returns empty
  - Feature no-look-ahead (1 test): every feature returns the same value
      at a decision point whether future bars exist in the underlying slice

Run from pipeline/ directory:
    pytest backtest/test_no_look_ahead.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from backtest.data import scan_ca_jumps
from backtest.engine import run as engine_run
from backtest.strategy import Action, BarContext, GapAndGo, Signal


# ── Synthetic candle factory ──────────────────────────────────────────────────

def _make_candles(n_days: int = 3, bars_per_day: int = 5) -> pd.DataFrame:
    """
    Build a minimal synthetic candle DataFrame: n_days × bars_per_day rows.
    Prices are flat (100/101/99/100) so no strategy logic fires accidentally.
    """
    rows = []
    for d in range(n_days):
        day = date(2026, 1, 2 + d)          # 2026-01-02, 03, 04 ...
        day_ts = pd.Timestamp(day)
        for m in range(bars_per_day):
            ts = pd.Timestamp(datetime(day.year, day.month, day.day, 9, 15 + m))
            rows.append({
                "timestamp": ts,
                "date":      day_ts,
                "open":      100.0,
                "high":      101.0,
                "low":        99.0,
                "close":     100.0,
                "volume":    1000,
                "oi":          0.0,
            })
    return pd.DataFrame(rows)


# ── Spy strategy ──────────────────────────────────────────────────────────────

class _SpyStrategy:
    """Records the number of visible bars at each on_bar call. Never signals."""

    eod_exit = False

    def __init__(self) -> None:
        self.bar_counts: list[int]             = []
        self.last_timestamps: list[pd.Timestamp] = []

    def reset_day(self) -> None:
        pass

    def on_bar(self, ctx: BarContext) -> Signal | None:
        self.bar_counts.append(len(ctx.bars))
        self.last_timestamps.append(ctx.bars.iloc[-1]["timestamp"])
        return None


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_strategy_sees_exactly_i_plus_one_bars() -> None:
    """At engine step i, strategy must see exactly i+1 bars (indices 0..i)."""
    candles = _make_candles(n_days=3, bars_per_day=5)
    spy     = _SpyStrategy()
    engine_run(candles, spy, "TEST")

    assert len(spy.bar_counts) == len(candles), (
        f"Expected {len(candles)} on_bar calls, got {len(spy.bar_counts)}"
    )
    for i, n in enumerate(spy.bar_counts):
        assert n == i + 1, (
            f"Step {i}: strategy saw {n} bars (expected {i + 1}) — look-ahead detected"
        )


def test_strategy_latest_bar_matches_current_index() -> None:
    """
    At step i, ctx.bars.iloc[-1]['timestamp'] must equal candles.iloc[i]['timestamp'].
    This is the direct look-ahead check: if the engine ever slices candles[:i+2]
    (or more) and passes it to the strategy, this test catches it.
    """
    candles = _make_candles(n_days=2, bars_per_day=10)
    spy     = _SpyStrategy()
    engine_run(candles, spy, "TEST")

    for i, ts in enumerate(spy.last_timestamps):
        expected = candles.iloc[i]["timestamp"]
        assert ts == expected, (
            f"Step {i}: strategy's latest bar is {ts}, "
            f"but current bar should be {expected} — look-ahead detected"
        )


def test_no_cross_day_fill() -> None:
    """
    An ENTER_LONG signal on the last bar of a day must NOT fill the next day.
    Pending entries must be cancelled at every day boundary.
    """
    candles = _make_candles(n_days=2, bars_per_day=5)

    class _LastBarSignalStrategy:
        """Fires ENTER_LONG on the very last bar of day 1, then stays silent."""
        eod_exit  = False
        _signalled = False

        def reset_day(self) -> None:
            pass

        def on_bar(self, ctx: BarContext) -> Signal | None:
            # Signal on bar index 4 (last of day 1, bars_per_day=5)
            if len(ctx.bars) == 5 and not self._signalled:
                self._signalled = True
                return Signal(
                    action=Action.ENTER_LONG,
                    stop_price=95.0,
                    target_r=2.0,
                )
            return None

    trades = engine_run(candles, _LastBarSignalStrategy(), "TEST")
    assert len(trades) == 0, (
        f"Expected 0 trades (cross-day fill must be prevented), got {len(trades)}"
    )


# ── prev_close() contract tests ───────────────────────────────────────────────

def test_prev_close_none_on_first_day() -> None:
    """
    At the very first bar of the loaded range there is no prior session in the
    candles DataFrame.  prev_close() must return None — the day must be skipped,
    not given a fabricated gap.
    """
    candles = _make_candles(n_days=2, bars_per_day=5)

    seen: list = []

    class _PrevCloseSpy:
        eod_exit = False

        def reset_day(self) -> None:
            pass

        def on_bar(self, ctx: BarContext) -> Signal | None:
            if len(ctx.bars) == 1:   # first bar of the entire range
                seen.append(ctx.prev_close())
            return None

    engine_run(candles, _PrevCloseSpy(), "TEST")

    assert len(seen) == 1, "Expected exactly one first-bar call"
    assert seen[0] is None, (
        f"prev_close() on first day must be None (no prior session), got {seen[0]!r}"
    )


def test_prev_close_uses_last_close_of_prior_session() -> None:
    """
    At the first bar of day 2, prev_close() must equal the LAST close of day 1
    — i.e. bar (bars_per_day - 1) of day 1 — never bars.iloc[0].
    """
    bars_per_day = 5
    candles = _make_candles(n_days=2, bars_per_day=bars_per_day)
    # _make_candles uses close=100 for all bars, so the expected value is 100.0

    seen: list = []

    class _PrevCloseSpy2:
        eod_exit = False

        def reset_day(self) -> None:
            pass

        def on_bar(self, ctx: BarContext) -> Signal | None:
            # First bar of day 2 is when len(ctx.bars) == bars_per_day + 1
            if len(ctx.bars) == bars_per_day + 1:
                seen.append(ctx.prev_close())
            return None

    engine_run(candles, _PrevCloseSpy2(), "TEST")

    assert len(seen) == 1
    assert seen[0] == 100.0, (
        f"Expected prev_close()=100.0 (last close of day 1), got {seen[0]!r}"
    )


# ── Entry-window guard tests ──────────────────────────────────────────────────

def _make_entry_window_candles() -> pd.DataFrame:
    """
    Two-day candle set for testing the entry-window parameter.

    Day 1 (2026-01-02): 1 bar, close=100 — provides a prev_close for day 2.

    Day 2 (2026-01-05): 80 bars.
      Bars  0-14: open=102 (bar 0 only), high=101, low=100.5
            → gap = 2% (≥ 1% threshold), OR_high = 101, OR_low = 100.5
      Bars 15-59: high=100.8, low=100.5   — below OR_high, within any window ≥ 15
      Bars 60-79: high=102.0, low=100.5   — break OR_high, but past bar 60

    With entry_window_min=60:  bar 60 has len(today_bars)=61 > 60 → window expired
    With entry_window_min=70:  bar 60 has len(today_bars)=61 ≤ 70 → entry fires
                                fill at bar 61 open=101, target=102 hit at bar 61
    """
    rows = []
    d1, d2 = date(2026, 1, 2), date(2026, 1, 5)
    d1ts, d2ts = pd.Timestamp(d1), pd.Timestamp(d2)

    # Day 1: single closing bar
    rows.append({
        "timestamp": pd.Timestamp(datetime(2026, 1, 2, 15, 29)),
        "date": d1ts, "open": 100.0, "high": 100.5,
        "low": 99.5, "close": 100.0, "volume": 1000, "oi": 0.0,
    })

    # Day 2: 80 bars — use timedelta to avoid minute-overflow past :59
    from datetime import timedelta as _td
    _open = datetime(2026, 1, 5, 9, 15)
    for m in range(80):
        ts = pd.Timestamp(_open + _td(minutes=m))
        o  = 102.0 if m == 0 else 101.0
        if m < 15:       # OR window  (OR_high=101, OR_low=100.5)
            h, l = 101.0, 100.5
        elif m < 60:     # within possible entry window, no breakout
            h, l = 100.8, 100.5
        else:            # post-window breakout; low above stop so target fires first
            h, l = 102.0, 101.0
        rows.append({
            "timestamp": ts, "date": d2ts,
            "open": o, "high": h, "low": l, "close": 101.0,
            "volume": 1000, "oi": 0.0,
        })

    return pd.DataFrame(rows)


def test_entry_window_expires_no_fill() -> None:
    """
    Breakout that occurs after entry_window_min bars must produce no trade.
    Day 2 breakout is at bar 60; with entry_window_min=60 that is bar 61 of the
    session (len(today_bars)=61 > 60), so the window has already closed.
    """
    candles  = _make_entry_window_candles()
    strategy = GapAndGo(min_gap_pct=1.0, opening_range_min=15, entry_window_min=60)
    trades   = engine_run(candles, strategy, "TEST")
    assert len(trades) == 0, (
        f"Expected 0 trades (breakout at bar 60 exceeds window=60), got {len(trades)}"
    )


def test_entry_window_within_window_produces_fill() -> None:
    """
    Same candles with a wider window (entry_window_min=70) must produce exactly
    1 trade: breakout bar 60 is within window, fill at bar 61 open=101,
    or_low=100.5 → risk=0.5 → target=102, which is hit at bar 61 high=102.
    """
    candles  = _make_entry_window_candles()
    strategy = GapAndGo(min_gap_pct=1.0, opening_range_min=15, entry_window_min=70)
    trades   = engine_run(candles, strategy, "TEST")
    assert len(trades) == 1, (
        f"Expected 1 trade (breakout at bar 60 within window=70), got {len(trades)}"
    )
    assert trades[0].exit_reason == "target_hit", (
        f"Expected target_hit exit, got {trades[0].exit_reason!r}"
    )


# ── Corporate-action guard tests ──────────────────────────────────────────────

def _make_ca_candles(split_pct: float = -50.0) -> pd.DataFrame:
    """
    Two-day candle set.  Day 2 opens with a large jump simulating a corporate
    action (e.g. split_pct=-50 for a 2:1 stock split).
    """
    rows = []
    d1, d2 = date(2026, 1, 2), date(2026, 1, 5)
    d1ts, d2ts = pd.Timestamp(d1), pd.Timestamp(d2)

    prev_close = 3000.0
    post_open  = prev_close * (1 + split_pct / 100)   # e.g. 1500 for a 2:1 split

    rows.append({
        "timestamp": pd.Timestamp(datetime(2026, 1, 2, 15, 29)),
        "date": d1ts, "open": prev_close, "high": prev_close,
        "low": prev_close * 0.99, "close": prev_close, "volume": 1000, "oi": 0.0,
    })
    rows.append({
        "timestamp": pd.Timestamp(datetime(2026, 1, 5, 9, 15)),
        "date": d2ts, "open": post_open, "high": post_open,
        "low": post_open * 0.99, "close": post_open, "volume": 1000, "oi": 0.0,
    })
    return pd.DataFrame(rows)


def test_ca_guard_flags_split() -> None:
    """scan_ca_jumps must flag a 50% close-to-open drop (2:1 split signature)."""
    candles = _make_ca_candles(split_pct=-50.0)
    flagged = scan_ca_jumps(candles, symbol="TESTSYM", ca_jump_pct=20.0)
    assert len(flagged) == 1, f"Expected 1 flagged transition, got {len(flagged)}"
    assert abs(flagged[0]["jump_pct"]) == pytest.approx(50.0, abs=0.1)


def test_ca_guard_clean_data_returns_empty() -> None:
    """Normal candles (no large jumps) must return an empty list."""
    candles = _make_candles(n_days=3, bars_per_day=5)
    flagged = scan_ca_jumps(candles, symbol="TEST", ca_jump_pct=20.0)
    assert flagged == [], f"Expected no CA events on flat data, got {flagged}"


# ── Feature no-look-ahead test ────────────────────────────────────────────────

class _FixedCurrentCtx(BarContext):
    """
    Test helper: forces ctx.current to a specific iloc position.

    With the real BarContext, ctx.current = bars.iloc[-1], so adding future bars
    to ctx.bars changes the 'current' bar.  This subclass pins 'current' to a
    fixed position so we can test: does the feature give the SAME value when
    ctx.bars contains extra future rows vs. a clean slice?

    If a feature erroneously indexes ctx.bars past the pinned position
    (e.g., ctx.bars.iloc[-1] or ctx.bars.iloc[dp+1]), the values will differ
    between ctx_correct and ctx_extended, and the test will catch it.
    """
    def __init__(self, bars: pd.DataFrame, position, fixed_iloc: int) -> None:
        super().__init__(bars, position)
        self._fixed_iloc = fixed_iloc

    @property
    def current(self) -> "pd.Series":
        return self.bars.iloc[self._fixed_iloc]


def test_feature_no_look_ahead() -> None:
    """
    Every registered feature must return the same value at a given decision bar
    regardless of whether future bars exist in ctx.bars.

    Structure:
      Day 0 (5 bars):   price=100, vol=1000  — provides prev_close
      Day 1 (20 bars):  price=102, vol=1200  — the 'today' of the decision point
      Future (5 bars):  price=9999, vol=9M   — extreme values must NOT appear

    Two BarContexts share the same 'current' bar (day-1 bar 19, index 24):
      ctx_correct  = bars 0..24 only              (no future)
      ctx_extended = ALL 30 bars, current pinned to index 24  (future rows present)

    A feature that accidentally reads past its decision bar — e.g., uses
    ctx.bars.iloc[-1] on ctx_extended — would see price=9999 and return a
    different value, causing the assertion to fail.
    """
    from backtest.features import FEATURES

    from datetime import timedelta as _td

    d0 = date(2026, 1, 2)
    d1 = date(2026, 1, 5)
    df = date(2026, 1, 6)   # "future"

    d0ts = pd.Timestamp(d0)
    d1ts = pd.Timestamp(d1)
    dfts = pd.Timestamp(df)

    rows_d0: list = []
    for m in range(5):
        ts = pd.Timestamp(datetime(2026, 1, 2, 9, 15 + m))
        rows_d0.append({
            "timestamp": ts, "date": d0ts,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1000, "oi": 0.0,
        })

    rows_d1: list = []
    _open1 = datetime(2026, 1, 5, 9, 15)
    for m in range(20):
        ts = pd.Timestamp(_open1 + _td(minutes=m))
        rows_d1.append({
            "timestamp": ts, "date": d1ts,
            "open": 102.0 if m == 0 else 101.5,
            "high": 103.0, "low": 101.0, "close": 102.0,
            "volume": 1200, "oi": 0.0,
        })

    # Future bars — extreme values that must NOT influence any feature
    rows_future: list = []
    _openf = datetime(2026, 1, 6, 9, 15)
    for m in range(5):
        ts = pd.Timestamp(_openf + _td(minutes=m))
        rows_future.append({
            "timestamp": ts, "date": dfts,
            "open": 9999.0, "high": 9999.0, "low": 9999.0, "close": 9999.0,
            "volume": 9_999_999, "oi": 0.0,
        })

    candles_correct  = pd.DataFrame(rows_d0 + rows_d1)
    candles_extended = pd.DataFrame(rows_d0 + rows_d1 + rows_future)

    # Decision point = last bar of day 1 = global index 24
    dp = len(candles_correct) - 1   # 24

    for name, spec in FEATURES.items():
        params = {p.name: p.default for p in spec.params}

        # Correct: ctx.bars stops at dp; ctx.current = bars.iloc[-1] = bar dp
        ctx_correct  = BarContext(
            bars     = candles_correct.iloc[: dp + 1],
            position = None,
        )
        v_correct = spec.compute(ctx_correct, params)

        # Extended: ctx.bars includes 5 future rows with extreme values,
        # but current is pinned to bar dp so the feature 'sees' the same today.
        ctx_extended = _FixedCurrentCtx(
            bars       = candles_extended,
            position   = None,
            fixed_iloc = dp,
        )
        v_extended = spec.compute(ctx_extended, params)

        match = (
            (v_correct is None and v_extended is None)
            or (
                v_correct is not None
                and v_extended is not None
                and abs(v_correct - v_extended) < 1e-9
            )
        )
        assert match, (
            f"Feature {name!r}: at decision bar {dp}, "
            f"ctx_correct={v_correct!r} vs ctx_extended={v_extended!r}. "
            "A difference indicates look-ahead (future bars with price=9999 "
            "or vol=9M leaked into the computation)."
        )
