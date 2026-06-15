"""
Tests for the Phase 1 forward-return labeler (processing/event_labeler.py):

- Direction multiplier: up→+1, down→−1, None→+1 (raw); signed forward return
- Nearest-frame matching: within tolerance computes; a gap (no frame) writes NULL
- MAE/MFE over the window in the signed convention
- EOD floor: last frame before 15:15 IST → eod NULL; at/after floor → computed
- Maturity gating: T+N matures past tolerance edge; eod matures past 15:30 IST close
- Day-boundary expression is byte-identical between the migration and the labeler (#4)
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import processing.event_labeler as L
from processing.event_labeler import (
    _direction_mult, _trade_day, _signed_return,
    _minute_matured, _eod_matured,
    _label_minute_horizon, _label_eod,
    IST, _IST_DAY_EXPR,
)

UTC = timezone.utc


class FakeCursor:
    """Scripts fetchone()/fetchall() results in call order; ignores SQL text."""
    def __init__(self, ones=None, alls=None):
        self._ones = list(ones or [])
        self._alls = list(alls or [])

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return self._ones.pop(0) if self._ones else None

    def fetchall(self):
        return self._alls.pop(0) if self._alls else []


def _event(symbol="AAA", ts=None):
    return {"symbol": symbol, "ts": ts or datetime(2026, 6, 10, 4, 0, 0, tzinfo=UTC)}


# ── direction multiplier ──────────────────────────────────────────────────────────

class TestDirectionMult:
    def test_up(self):   assert _direction_mult("up") == 1
    def test_down(self): assert _direction_mult("down") == -1
    def test_none_is_raw_positive(self): assert _direction_mult(None) == 1


# ── signed return ──────────────────────────────────────────────────────────────────

class TestSignedReturn:
    def test_up_gain_positive(self):
        assert _signed_return(100.0, 102.0, 1) == 2.0

    def test_down_call_correct_is_positive(self):
        """A down event where price fell is a CORRECT call → positive forward return."""
        assert _signed_return(100.0, 98.0, -1) == 2.0

    def test_down_call_wrong_is_negative(self):
        assert _signed_return(100.0, 103.0, -1) == -3.0


# ── trade-day bucketing matches the migration expression ───────────────────────────

class TestDayBoundary:
    def test_python_trade_day_is_ist(self):
        """18:30Z == 00:00 IST next day → trade day rolls with IST, not UTC."""
        ts = datetime(2026, 6, 10, 18, 30, 0, tzinfo=UTC)   # 2026-06-11 00:00 IST
        assert _trade_day(ts).isoformat() == "2026-06-11"

    def test_expression_byte_identical_to_migration(self):
        """
        Refinement #4: the labeler's day expression MUST match migration 014's unique
        index byte-for-byte, or a midnight-UTC event could dedup under one day and label
        under another. Assert the exact string appears in the migration.
        """
        mig = (Path(__file__).resolve().parents[1]
               / "storage" / "migrations" / "014_up.sql").read_text(encoding="utf-8")
        assert _IST_DAY_EXPR == "(ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date"
        # Normalise whitespace/newlines the migration may wrap the expression across.
        mig_flat = re.sub(r"\s+", " ", mig)
        assert "ts AT TIME ZONE INTERVAL '5 hours 30 minutes' )::date" in mig_flat \
            or "ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date" in mig_flat


# ── maturity gating ────────────────────────────────────────────────────────────────

class TestMaturity:
    def test_minute_not_matured_before_tolerance_edge(self):
        target = datetime(2026, 6, 10, 4, 5, 0, tzinfo=UTC)
        now    = target + timedelta(seconds=40)      # inside tolerance, not past edge
        assert _minute_matured(target, now) is False

    def test_minute_matured_past_tolerance_edge(self):
        target = datetime(2026, 6, 10, 4, 5, 0, tzinfo=UTC)
        now    = target + timedelta(seconds=60)
        assert _minute_matured(target, now) is True

    def test_eod_not_matured_before_close(self):
        day = _trade_day(datetime(2026, 6, 10, 4, 0, 0, tzinfo=UTC))   # 2026-06-10
        now = datetime(2026, 6, 10, 9, 0, 0, tzinfo=UTC)               # 14:30 IST < close
        assert _eod_matured(day, now) is False

    def test_eod_matured_after_close(self):
        day = _trade_day(datetime(2026, 6, 10, 4, 0, 0, tzinfo=UTC))
        now = datetime(2026, 6, 10, 10, 30, 0, tzinfo=UTC)            # 16:00 IST > close
        assert _eod_matured(day, now) is True


# ── minute horizon labeling ─────────────────────────────────────────────────────────

class TestMinuteHorizon:
    def test_match_computes_signed_return_and_excursions(self):
        event   = _event()
        target  = event["ts"] + timedelta(minutes=5)
        matched = target + timedelta(seconds=10)
        cur = FakeCursor(
            ones=[(matched, 102.0)],                       # _nearest_frame
            alls=[[(101.0,), (103.0,), (102.0,)]],         # _excursions path prices
        )
        fr, mae, mfe, m_ts, off = _label_minute_horizon(cur, event, 100.0, 1, target)
        assert fr == 2.0
        assert mae == 1.0 and mfe == 3.0
        assert m_ts == matched
        assert off == 10.0

    def test_gap_writes_null(self):
        """No frame within tolerance → NULL metrics, never a stretch match."""
        event  = _event()
        target = event["ts"] + timedelta(minutes=5)
        cur = FakeCursor(ones=[None])                      # _nearest_frame finds nothing
        assert _label_minute_horizon(cur, event, 100.0, 1, target) == (None, None, None, None, None)

    def test_down_direction_signs_return(self):
        event   = _event()
        target  = event["ts"] + timedelta(minutes=5)
        matched = target
        cur = FakeCursor(ones=[(matched, 98.0)], alls=[[]])
        fr, *_ = _label_minute_horizon(cur, event, 100.0, -1, target)
        assert fr == 2.0                                   # down + price fell = correct = +


# ── eod horizon + floor ──────────────────────────────────────────────────────────────

class TestEodFloor:
    def test_last_frame_after_floor_computes(self):
        event   = _event()
        last_ts = datetime(2026, 6, 10, 10, 0, 0, tzinfo=UTC)   # 15:30 IST ≥ 15:15 floor
        cur = FakeCursor(ones=[(last_ts, 105.0)], alls=[[(104.0,), (106.0,)]])
        fr, mae, mfe, m_ts, off = _label_eod(cur, event, 100.0, 1)
        assert fr == 5.0
        assert m_ts == last_ts
        assert off is None

    def test_last_frame_before_floor_writes_null(self):
        """Mid-day last print (< 15:15 IST) must NOT be passed off as the close."""
        event   = _event()
        last_ts = datetime(2026, 6, 10, 9, 0, 0, tzinfo=UTC)    # 14:30 IST < floor
        cur = FakeCursor(ones=[(last_ts, 105.0)])
        assert _label_eod(cur, event, 100.0, 1) == (None, None, None, None, None)

    def test_no_frames_writes_null(self):
        cur = FakeCursor(ones=[None])
        assert _label_eod(cur, _event(), 100.0, 1) == (None, None, None, None, None)
