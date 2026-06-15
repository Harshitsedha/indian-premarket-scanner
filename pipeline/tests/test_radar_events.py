"""
Tests for Phase 1 event emission (realtime/radar_events.py):

- Direction convention frozen at emission: ORB up/down, gap sign, range_expansion None
- First-crossing dedup: one event per (symbol, event_type, trade_date), survives re-cycles
- trigger JSONB payload: metric values at crossing + direction, no snapshot join needed
- event_type == rule name; events fire independently of the alert cap/throttle
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import realtime.radar_events as radar_events
from realtime.radar_events import process_events, _resolve_direction

TRADE_DATE = "2026-06-10"
FRAME_TS   = "2026-06-10T09:30:00+05:30"

_RULES = [
    {"name": "orb_break_volume", "conditions": {"or_status": ["broke_up", "broke_down"], "rvol_min": 2.0}},
    {"name": "gap_momentum",     "conditions": {"abs_gap_pct_min": 2.0, "rvol_min": 2.5}},
    {"name": "range_expansion",  "conditions": {"atr_multiple_min": 2.0, "rvol_min": 2.0}},
]


class FakeEventCache:
    """setnx_ex dedup keyspace + capture of xadd_event payloads."""
    def __init__(self):
        self.kv: dict = {}
        self.emitted: list = []

    def setnx_ex(self, key, ttl):
        if key in self.kv:
            return False
        self.kv[key] = 1
        return True

    def xadd_event(self, stream, event):
        self.emitted.append((stream, event))
        return True


def _row(symbol="AAA", **kw):
    base = {
        "symbol": symbol, "or_status": "inside", "rvol": None, "gap_pct": 0.0,
        "atr_multiple": 0.0, "ltp": 100.0, "or_break_atr": None,
    }
    base.update(kw)
    return base


# ── direction convention ────────────────────────────────────────────────────────

class TestDirectionConvention:
    def test_orb_break_up_is_up(self):
        assert _resolve_direction("orb_break_volume", _row(or_status="broke_up")) == "up"

    def test_orb_break_down_is_down(self):
        assert _resolve_direction("orb_break_volume", _row(or_status="broke_down")) == "down"

    def test_gap_up_is_up(self):
        assert _resolve_direction("gap_momentum", _row(gap_pct=2.5)) == "up"

    def test_gap_down_is_down(self):
        assert _resolve_direction("gap_momentum", _row(gap_pct=-2.5)) == "down"

    def test_range_expansion_is_none(self):
        """No inherent direction — must be None (labeled unsigned), never guessed."""
        assert _resolve_direction("range_expansion", _row(atr_multiple=3.0)) is None


# ── emission + dedup ──────────────────────────────────────────────────────────────

class TestEmission:
    def test_orb_event_emitted_once_with_trigger(self):
        cache = FakeEventCache()
        row = _row(or_status="broke_up", rvol=3.0, ltp=512.4, or_break_atr=1.3,
                   gap_pct=0.5, atr_multiple=1.0)
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS)
        assert n == 1
        stream, ev = cache.emitted[0]
        assert stream == "radar:events"
        assert ev["event_type"] == "orb_break_volume"
        assert ev["symbol"] == "AAA"
        assert ev["ts"] == FRAME_TS
        assert ev["regime_id"] is None
        assert ev["event_id"]                       # a UUID string is present
        t = ev["trigger"]
        assert t["direction"] == "up"
        assert t["price"] == 512.4
        assert t["rvol"] == 3.0
        assert t["or_status"] == "broke_up"
        assert t["or_break_atr"] == 1.3

    def test_first_crossing_only(self):
        """Re-evaluating the same latched row on later cycles must not re-emit."""
        cache = FakeEventCache()
        row = _row(or_status="broke_up", rvol=3.0)
        with patch.object(radar_events, "_cache", cache):
            first  = process_events([row], _RULES, TRADE_DATE, FRAME_TS)
            second = process_events([row], _RULES, TRADE_DATE, FRAME_TS)
        assert (first, second) == (1, 0)
        assert len(cache.emitted) == 1

    def test_distinct_event_types_and_symbols_independent(self):
        cache = FakeEventCache()
        rows = [
            _row("AAA", or_status="broke_up", rvol=3.0),       # orb
            _row("BBB", gap_pct=-3.0, rvol=3.0),               # gap down
            _row("CCC", atr_multiple=2.5, rvol=2.2),           # range expansion
        ]
        with patch.object(radar_events, "_cache", cache):
            n = process_events(rows, _RULES, TRADE_DATE, FRAME_TS)
        assert n == 3
        by_type = {ev["event_type"]: ev for _, ev in cache.emitted}
        assert by_type["gap_momentum"]["trigger"]["direction"] == "down"
        assert by_type["range_expansion"]["trigger"]["direction"] is None

    def test_no_match_no_emit(self):
        cache = FakeEventCache()
        row = _row(or_status="inside", rvol=1.0, gap_pct=0.1, atr_multiple=0.5)
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS)
        assert n == 0
        assert cache.emitted == []

    def test_dedup_key_namespace_is_event_specific(self):
        """Dedup keys live in their own keyspace, never colliding with alert dedup."""
        cache = FakeEventCache()
        row = _row(or_status="broke_up", rvol=3.0)
        with patch.object(radar_events, "_cache", cache):
            process_events([row], _RULES, TRADE_DATE, FRAME_TS)
        assert any(k.startswith(f"radar:event_emitted:{TRADE_DATE}:orb_break_volume:")
                   for k in cache.kv)
