"""
Tests for event emission (realtime/radar_events.py):

- Direction convention frozen at emission: ORB up/down, gap sign, range_expansion None
- Phase 3 per-range ORB: one orb_break_volume event per OR window that broke; a symbol
  breaking two windows emits two events with distinct range_labels; range_id stamped
  from range_defs (provenance); the canonical slice key is range_label (the window).
- First-crossing dedup is now per (symbol, event_type, range, trade_date)
- gap_momentum / range_expansion stay on the row-level path with range_label=None
- trigger JSONB payload: metric values at crossing + direction + range tag
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import realtime.radar_events as radar_events
from realtime.radar_events import process_events, _resolve_direction

TRADE_DATE = "2026-06-10"
FRAME_TS   = "2026-06-10T09:30:00+05:30"

_DEFAULT_LABEL = "09:15-09:30"
_CUSTOM_LABEL  = "09:45-10:30"

_RULES = [
    {"name": "orb_break_volume", "conditions": {"or_status": ["broke_up", "broke_down"], "rvol_min": 2.0}},
    {"name": "gap_momentum",     "conditions": {"abs_gap_pct_min": 2.0, "rvol_min": 2.5}},
    {"name": "range_expansion",  "conditions": {"atr_multiple_min": 2.0, "rvol_min": 2.0}},
]

# tracker.defs() shape the poller passes in — label -> {id, is_default}
_RANGE_DEFS = [
    {"id": 1, "label": _DEFAULT_LABEL, "is_default": True},
    {"id": 2, "label": _CUSTOM_LABEL,  "is_default": False},
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


def _row(symbol="AAA", ranges=None, **kw):
    base = {
        "symbol": symbol, "or_status": "inside", "rvol": None, "gap_pct": 0.0,
        "atr_multiple": 0.0, "ltp": 100.0, "or_break_atr": None,
        "ranges": ranges if ranges is not None else {},
    }
    base.update(kw)
    return base


def _cell(status, break_atr=None):
    return {"status": status, "break_atr": break_atr}


# ── direction convention ────────────────────────────────────────────────────────

class TestDirectionConvention:
    def test_orb_break_up_is_up(self):
        assert _resolve_direction("orb_break_volume", {"or_status": "broke_up"}) == "up"

    def test_orb_break_down_is_down(self):
        assert _resolve_direction("orb_break_volume", {"or_status": "broke_down"}) == "down"

    def test_gap_up_is_up(self):
        assert _resolve_direction("gap_momentum", _row(gap_pct=2.5)) == "up"

    def test_gap_down_is_down(self):
        assert _resolve_direction("gap_momentum", _row(gap_pct=-2.5)) == "down"

    def test_range_expansion_is_none(self):
        """No inherent direction — must be None (labeled unsigned), never guessed."""
        assert _resolve_direction("range_expansion", _row(atr_multiple=3.0)) is None


# ── per-range ORB emission ──────────────────────────────────────────────────────

class TestOrbRangeEmission:
    def test_default_range_event_with_range_tag(self):
        cache = FakeEventCache()
        row = _row(rvol=3.0, ltp=512.4, gap_pct=0.5, atr_multiple=1.0,
                   ranges={_DEFAULT_LABEL: _cell("broke_up", 1.3)})
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 1
        stream, ev = cache.emitted[0]
        assert stream == "radar:events"
        assert ev["event_type"] == "orb_break_volume"
        assert ev["symbol"] == "AAA"
        t = ev["trigger"]
        assert t["direction"] == "up"
        assert t["price"] == 512.4
        assert t["rvol"] == 3.0
        assert t["or_status"] == "broke_up"      # the broken range's status
        assert t["or_break_atr"] == 1.3          # the broken range's break distance
        assert t["range_label"] == _DEFAULT_LABEL
        assert t["range_id"] == 1

    def test_symbol_breaking_two_ranges_emits_two_events(self):
        """The core Phase 3 behaviour: default + custom both break → two distinct events."""
        cache = FakeEventCache()
        row = _row(rvol=3.0, ranges={
            _DEFAULT_LABEL: _cell("broke_up", 1.1),
            _CUSTOM_LABEL:  _cell("broke_down", 0.7),
        })
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 2
        by_label = {ev["trigger"]["range_label"]: ev["trigger"] for _, ev in cache.emitted}
        assert set(by_label) == {_DEFAULT_LABEL, _CUSTOM_LABEL}
        assert by_label[_DEFAULT_LABEL]["direction"] == "up"
        assert by_label[_CUSTOM_LABEL]["direction"] == "down"
        assert by_label[_DEFAULT_LABEL]["range_id"] == 1
        assert by_label[_CUSTOM_LABEL]["range_id"] == 2

    def test_custom_range_breaks_even_when_default_inside(self):
        """A custom-window break must emit even though the legacy default field is 'inside'."""
        cache = FakeEventCache()
        row = _row(rvol=3.0, or_status="inside",
                   ranges={_DEFAULT_LABEL: _cell("inside"),
                           _CUSTOM_LABEL:  _cell("broke_up", 0.9)})
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 1
        assert cache.emitted[0][1]["trigger"]["range_label"] == _CUSTOM_LABEL

    def test_rvol_below_min_blocks_all_ranges(self):
        cache = FakeEventCache()
        row = _row(rvol=1.0, ranges={_DEFAULT_LABEL: _cell("broke_up", 1.0)})
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 0

    def test_per_range_first_crossing_only(self):
        """Re-evaluating the same latched ranges must not re-emit (per-window dedup)."""
        cache = FakeEventCache()
        row = _row(rvol=3.0, ranges={
            _DEFAULT_LABEL: _cell("broke_up", 1.1),
            _CUSTOM_LABEL:  _cell("broke_up", 0.7),
        })
        with patch.object(radar_events, "_cache", cache):
            first  = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
            second = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert (first, second) == (2, 0)
        assert len(cache.emitted) == 2

    def test_dedup_key_includes_range_label(self):
        cache = FakeEventCache()
        row = _row(rvol=3.0, ranges={_CUSTOM_LABEL: _cell("broke_up", 0.9)})
        with patch.object(radar_events, "_cache", cache):
            process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert f"radar:event_emitted:{TRADE_DATE}:orb_break_volume:AAA:{_CUSTOM_LABEL}" in cache.kv

    def test_missing_range_def_still_emits_with_null_range_id(self):
        """If range_defs lacks the label, the event still emits (range_id None)."""
        cache = FakeEventCache()
        row = _row(rvol=3.0, ranges={_CUSTOM_LABEL: _cell("broke_up", 0.9)})
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, range_defs=[])
        assert n == 1
        assert cache.emitted[0][1]["trigger"]["range_id"] is None
        assert cache.emitted[0][1]["trigger"]["range_label"] == _CUSTOM_LABEL


# ── non-OR events (row-level path, range_label None) ──────────────────────────────

class TestNonOrEvents:
    def test_gap_and_range_expansion_have_null_range_label(self):
        cache = FakeEventCache()
        rows = [
            _row("BBB", gap_pct=-3.0, rvol=3.0),       # gap down
            _row("CCC", atr_multiple=2.5, rvol=2.2),   # range expansion
        ]
        with patch.object(radar_events, "_cache", cache):
            n = process_events(rows, _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 2
        by_type = {ev["event_type"]: ev["trigger"] for _, ev in cache.emitted}
        assert by_type["gap_momentum"]["direction"] == "down"
        assert by_type["gap_momentum"]["range_label"] is None
        assert by_type["range_expansion"]["direction"] is None
        assert by_type["range_expansion"]["range_label"] is None

    def test_gap_dedup_key_uses_null_token(self):
        """Non-OR events dedup one-per-(symbol,type,day): key carries the '-' range token."""
        cache = FakeEventCache()
        row = _row("BBB", gap_pct=-3.0, rvol=3.0)
        with patch.object(radar_events, "_cache", cache):
            first  = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
            second = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert (first, second) == (1, 0)
        assert f"radar:event_emitted:{TRADE_DATE}:gap_momentum:BBB:-" in cache.kv

    def test_no_match_no_emit(self):
        cache = FakeEventCache()
        row = _row(rvol=1.0, gap_pct=0.1, atr_multiple=0.5,
                   ranges={_DEFAULT_LABEL: _cell("inside")})
        with patch.object(radar_events, "_cache", cache):
            n = process_events([row], _RULES, TRADE_DATE, FRAME_TS, _RANGE_DEFS)
        assert n == 0
        assert cache.emitted == []
