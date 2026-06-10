"""
Tests for the radar pipeline:

- RVOL math: normal, early-session fraction clamp, zero volume, None avg_volume
- gap_pct: null when today_open is None/zero; correct value when data present
- radar_poller._compute_row: all derived fields
- _poll_cycle: success path, partial-missing symbols, 401 handling
- /api/radar: stale detection (>3 min old snapshot), market_open flag
- universe.refresh_universe: skips unmappable symbols without failing
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime.radar_poller import (
    _compute_row,
    _poll_cycle,
    _session_fraction,
    _SNAPSHOT_KEY,
    _STATUS_KEY,
    IST,
)
from realtime.orb_ranges import RangeTracker, builtin_default_def, compute_break_status


class FakeCache:
    """In-memory stand-in for storage.redis_client (kv + list + hash keyspaces)."""

    def __init__(self):
        self.kv:     dict = {}
        self.lists:  dict = {}
        self.hashes: dict = {}

    def get(self, key):
        return self.kv.get(key)

    def set_ex(self, key, value, ttl=60):
        self.kv[key] = value

    def setnx_ex(self, key, ttl):
        if key in self.kv:
            return False
        self.kv[key] = 1
        return True

    def rpush_json_many(self, items, ttl):
        for k, v in items.items():
            self.lists.setdefault(k, []).append(v)

    def lrange_json(self, key):
        return list(self.lists.get(key, []))

    def hset_json(self, key, mapping, ttl=None):
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall_json(self, key):
        return dict(self.hashes.get(key, {}))

    def delete_pattern(self, pattern):
        import fnmatch
        n = 0
        for store in (self.kv, self.lists, self.hashes):
            for k in [k for k in store if fnmatch.fnmatch(k, pattern)]:
                del store[k]
                n += 1
        return n


def _make_tracker(fake_cache, trade_date="2026-06-10", defs=None):
    """RangeTracker wired to a FakeCache (defaults to the built-in default range)."""
    with patch("realtime.orb_ranges._cache", fake_cache):
        tracker = RangeTracker(trade_date)
        tracker.set_defs(defs if defs is not None else [builtin_default_def()])
    return tracker


# ── helpers ───────────────────────────────────────────────────────────────────

def _baseline(prev_close=100.0, avg_vol=1_000_000, avg_range=0.02, prev_high=102.0, prev_low=98.0):
    return {
        "prev_close":        prev_close,
        "avg_volume_20d":    avg_vol,
        "avg_range_pct_20d": avg_range,
        "prev_high":         prev_high,
        "prev_low":          prev_low,
    }


def _entry(ltp=105.0, open_=102.0, high=106.0, low=101.0, volume=500_000, prev_close=100.0):
    return {
        "last_price": ltp,
        "ohlc": {
            "open":  open_,
            "high":  high,
            "low":   low,
            "close": prev_close,   # Upstox: ohlc.close = prev session close during intraday
        },
        "volume": volume,
    }


# ── RVOL math ─────────────────────────────────────────────────────────────────

class TestRvol:
    def test_mid_session_rvol(self):
        """Normal mid-session RVOL: volume / (avg_vol * fraction)."""
        entry    = _entry(volume=500_000)
        baseline = _baseline(avg_vol=1_000_000)
        fraction = 0.5

        row = _compute_row("TEST", entry, baseline, fraction, "NIFTY100")
        expected = round(500_000 / (1_000_000 * 0.5), 2)
        assert row["rvol"] == pytest.approx(expected)

    def test_early_session_clamp(self):
        """Fraction below 0.05 is clamped to 0.05 so no division-by-zero."""
        # _session_fraction() returns max(0.05, ...) — test the compute_row path
        # by passing fraction=0.01 (below clamp); _compute_row itself does not clamp,
        # the clamp lives in _session_fraction(). Verify the math still works.
        entry    = _entry(volume=100_000)
        baseline = _baseline(avg_vol=1_000_000)
        fraction = 0.01   # unclamped value

        row = _compute_row("TEST", entry, baseline, fraction, "NIFTY100")
        expected = round(100_000 / (1_000_000 * 0.01), 2)
        assert row["rvol"] == pytest.approx(expected)

    def test_zero_volume_returns_none(self):
        entry    = _entry(volume=0)
        baseline = _baseline(avg_vol=1_000_000)
        row = _compute_row("TEST", entry, baseline, 0.5, "NIFTY100")
        assert row["rvol"] is None

    def test_none_avg_volume_returns_none(self):
        entry    = _entry(volume=500_000)
        baseline = _baseline(avg_vol=None)
        row = _compute_row("TEST", entry, baseline, 0.5, "NIFTY100")
        assert row["rvol"] is None

    def test_no_baseline_returns_none(self):
        """Without a baseline, RVOL is None."""
        row = _compute_row("TEST", _entry(), None, 0.5, "NIFTY100")
        assert row["rvol"] is None


# ── session_fraction clamp ────────────────────────────────────────────────────

class TestSessionFraction:
    def _patch_now(self, h, m, s=0):
        """Patch datetime.now to return a fixed IST time."""
        fixed = datetime(2026, 6, 10, h, m, s, tzinfo=IST)
        return patch("realtime.radar_poller.datetime",
                     **{"now.return_value": fixed, "fromisoformat": datetime.fromisoformat})

    def test_at_session_start_clamped_to_min(self):
        with self._patch_now(9, 15):
            f = _session_fraction()
        assert f == pytest.approx(0.05)  # 0 elapsed → clamped to 0.05

    def test_one_minute_in_still_clamped(self):
        with self._patch_now(9, 16):
            f = _session_fraction()
        # 1/375 ≈ 0.00267 — still below 0.05 → clamped
        assert f == pytest.approx(0.05)

    def test_mid_session(self):
        with self._patch_now(12, 0):
            f = _session_fraction()
        # (720 - 555) / 375 = 165/375 ≈ 0.44
        assert f == pytest.approx(165 / 375, abs=1e-3)

    def test_at_session_end_clamped_to_one(self):
        with self._patch_now(15, 30):
            f = _session_fraction()
        assert f == pytest.approx(1.0)


# ── gap_pct null handling ─────────────────────────────────────────────────────

class TestGapPct:
    def test_null_when_open_is_zero(self):
        """Pre-market: open not yet available — gap_pct should be None."""
        entry = _entry(open_=0)
        row   = _compute_row("TEST", entry, _baseline(), 0.5, "NIFTY100")
        assert row["gap_pct"] is None

    def test_null_when_open_is_none_in_ohlc(self):
        entry = {"last_price": 105.0, "ohlc": {"open": None, "high": 106.0, "low": 101.0, "close": 100.0}, "volume": 100}
        row   = _compute_row("TEST", entry, _baseline(), 0.5, "NIFTY100")
        assert row["gap_pct"] is None

    def test_correct_gap_pct(self):
        """gap_pct = (open - prev_close) / prev_close * 100."""
        entry    = _entry(open_=103.0)
        baseline = _baseline(prev_close=100.0)
        row = _compute_row("TEST", entry, baseline, 0.5, "NIFTY100")
        assert row["gap_pct"] == pytest.approx(3.0)

    def test_negative_gap(self):
        entry    = _entry(open_=97.0)
        baseline = _baseline(prev_close=100.0)
        row = _compute_row("TEST", entry, baseline, 0.5, "NIFTY100")
        assert row["gap_pct"] == pytest.approx(-3.0)


# ── poller parsing ────────────────────────────────────────────────────────────

class TestPollCycle:
    def _universe(self):
        return [
            {"symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "index_membership": "NIFTY100"},
            {"symbol": "INFY",     "instrument_key": "NSE_EQ|INE009A01021", "index_membership": "NIFTY100"},
        ]

    def _quote_data(self):
        return {
            "NSE_EQ|INE002A01018": _entry(ltp=2950.0, open_=2920.0, high=2960.0, low=2910.0, volume=800_000, prev_close=2900.0),
            "NSE_EQ|INE009A01021": _entry(ltp=1500.0, open_=1480.0, high=1510.0, low=1475.0, volume=400_000, prev_close=1490.0),
        }

    def _call(self, http, baselines, quote_data, mock_cache, written):
        """Helper: call _poll_cycle with the full new signature."""
        fake = FakeCache()
        with patch("realtime.radar_poller._get_full_quotes", return_value=quote_data), \
             patch("realtime.radar_poller._cache", mock_cache), \
             patch("realtime.orb_ranges._cache", fake), \
             patch("realtime.radar_poller._session_fraction", return_value=0.5), \
             patch("realtime.radar_poller.process_alerts"):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            return _poll_cycle(
                http, self._universe(), baselines,
                tracker=_make_tracker(fake), trade_date="2026-06-10",
                news_cache={}, alert_rules=[],
            )

    def test_success_writes_snapshot(self):
        """Successful poll writes radar:snapshot to Redis."""
        http      = MagicMock()
        mock_cache = MagicMock()
        baselines = {
            "RELIANCE": _baseline(prev_close=2900.0, avg_vol=2_000_000),
            "INFY":     _baseline(prev_close=1490.0, avg_vol=1_000_000),
        }
        written = {}
        result = self._call(http, baselines, self._quote_data(), mock_cache, written)

        assert result is True
        assert _SNAPSHOT_KEY in written
        snap = written[_SNAPSHOT_KEY]
        assert "generated_at" in snap
        assert len(snap["rows"]) == 2
        symbols = {r["symbol"] for r in snap["rows"]}
        assert "RELIANCE" in symbols and "INFY" in symbols

    def test_partial_symbols_missing(self):
        """If some instrument_keys are absent from the quote response, those rows are skipped."""
        partial_data = {"NSE_EQ|INE002A01018": _entry()}   # only RELIANCE, no INFY
        http       = MagicMock()
        mock_cache = MagicMock()
        written    = {}
        result = self._call(http, {"RELIANCE": _baseline()}, partial_data, mock_cache, written)

        assert result is True
        snap = written[_SNAPSHOT_KEY]
        assert len(snap["rows"]) == 1
        assert snap["rows"][0]["symbol"] == "RELIANCE"

    def test_401_sets_status_and_returns_false(self):
        """401 response sets radar:status, sends Telegram alert, returns False."""
        http       = MagicMock()
        mock_cache = MagicMock()
        written    = {}

        fake = FakeCache()
        with patch("realtime.radar_poller._get_full_quotes", return_value={"_401": True}), \
             patch("realtime.radar_poller._cache", mock_cache), \
             patch("realtime.orb_ranges._cache", fake), \
             patch("realtime.radar_poller._send_token_alert") as mock_alert:
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            result = _poll_cycle(
                http, self._universe(), {},
                tracker=_make_tracker(fake), trade_date="2026-06-10",
                news_cache={}, alert_rules=[],
            )

        assert result is False
        mock_alert.assert_called_once()
        assert _STATUS_KEY in written
        assert written[_STATUS_KEY]["state"] == "token_expired"
        assert _SNAPSHOT_KEY not in written

    def test_none_response_does_not_crash(self):
        """None from _get_full_quotes (network error) returns True and writes nothing."""
        http       = MagicMock()
        mock_cache = MagicMock()

        fake = FakeCache()
        with patch("realtime.radar_poller._get_full_quotes", return_value=None), \
             patch("realtime.radar_poller._cache", mock_cache), \
             patch("realtime.orb_ranges._cache", fake):
            result = _poll_cycle(
                http, self._universe(), {},
                tracker=_make_tracker(fake), trade_date="2026-06-10",
                news_cache={}, alert_rules=[],
            )

        assert result is True
        mock_cache.set_ex.assert_not_called()


# ── /api/radar stale + market_open behavior ──────────────────────────────────

class TestRadarApiEndpoint:
    def _make_snapshot(self, age_seconds: int) -> dict:
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return {"generated_at": ts.isoformat(), "rows": []}

    def test_stale_when_snapshot_older_than_3_min(self):
        """snapshot generated >180s ago → stale=True."""
        from api.main import radar as radar_endpoint

        old_snapshot = self._make_snapshot(200)
        with patch("api.main._cache") as mock_cache, \
             patch("api.main._load_active_ranges", return_value=[]):
            mock_cache.get.side_effect = lambda k: old_snapshot if k == "radar:snapshot" else {}
            result = radar_endpoint()
        assert result["stale"] is True

    def test_not_stale_when_snapshot_is_recent(self):
        """snapshot generated <60s ago → stale=False."""
        from api.main import radar as radar_endpoint

        fresh_snapshot = self._make_snapshot(30)
        with patch("api.main._cache") as mock_cache, \
             patch("api.main._load_active_ranges", return_value=[]):
            mock_cache.get.side_effect = lambda k: fresh_snapshot if k == "radar:snapshot" else {}
            result = radar_endpoint()
        assert result["stale"] is False

    def test_no_snapshot_returns_none_not_stale(self):
        """No snapshot in Redis → snapshot=None, stale=False."""
        from api.main import radar as radar_endpoint

        with patch("api.main._cache") as mock_cache, \
             patch("api.main._load_active_ranges", return_value=[]):
            mock_cache.get.return_value = None
            result = radar_endpoint()
        assert result["snapshot"] is None
        assert result["stale"] is False

    def test_market_open_during_session(self):
        """A datetime within 09:15–15:30 IST on a weekday → market_open=True."""
        from api.main import radar as radar_endpoint

        # 11:00 IST = 05:30 UTC on a Tuesday
        tuesday_1100_ist = datetime(2026, 6, 9, 5, 30, 0, tzinfo=timezone.utc)
        with patch("api.main._cache") as mock_cache, \
             patch("api.main._load_active_ranges", return_value=[]), \
             patch("api.main.datetime") as mock_dt:
            mock_cache.get.return_value = None
            mock_dt.now.return_value = tuesday_1100_ist
            mock_dt.fromisoformat = datetime.fromisoformat
            result = radar_endpoint()
        assert result["market_open"] is True

    def test_market_closed_on_weekend(self):
        """Saturday → market_open=False."""
        from api.main import radar as radar_endpoint

        saturday_1100_ist = datetime(2026, 6, 6, 5, 30, 0, tzinfo=timezone.utc)
        with patch("api.main._cache") as mock_cache, \
             patch("api.main._load_active_ranges", return_value=[]), \
             patch("api.main.datetime") as mock_dt:
            mock_cache.get.return_value = None
            mock_dt.now.return_value = saturday_1100_ist
            mock_dt.fromisoformat = datetime.fromisoformat
            result = radar_endpoint()
        assert result["market_open"] is False


# ── universe sourced from tagging_universe.txt ────────────────────────────────

class TestUniverseMapping:
    def test_unmappable_symbols_skipped_not_raised(self):
        """Symbols without an Upstox instrument_key are logged and excluded; no exception."""
        from ingestion.universe import refresh_universe

        fake_symbols = ["RELIANCE", "UNMAPPABLE_XYZ", "TCS"]

        def fake_token(sym):
            return "NSE_EQ|FAKE" if sym in ("RELIANCE", "TCS") else None

        upserted = []
        with patch("ingestion.universe._load_tagging_universe", return_value=fake_symbols), \
             patch("ingestion.universe.get_instrument_token", side_effect=fake_token), \
             patch("ingestion.universe._needs_refresh", return_value=True), \
             patch("ingestion.universe._upsert_universe", side_effect=lambda rows: upserted.extend(rows)):
            refresh_universe(force=True)

        symbols = {r["symbol"] for r in upserted}
        assert "RELIANCE" in symbols
        assert "TCS" in symbols
        assert "UNMAPPABLE_XYZ" not in symbols

    def test_refresh_sources_from_tagging_universe(self):
        """refresh_universe reads from _load_tagging_universe (not NSE downloads)."""
        from ingestion.universe import refresh_universe

        tagging_symbols = ["INFY", "HDFC", "TCS"]
        upserted = []
        with patch("ingestion.universe._load_tagging_universe", return_value=tagging_symbols), \
             patch("ingestion.universe.get_instrument_token", return_value="NSE_EQ|FAKE"), \
             patch("ingestion.universe._needs_refresh", return_value=True), \
             patch("ingestion.universe._upsert_universe", side_effect=lambda rows: upserted.extend(rows)):
            refresh_universe(force=True)

        assert len(upserted) == 3
        assert all(r["index_membership"] == "BACKTESTER" for r in upserted)

    def test_override_symbols_added_with_override_membership(self):
        """universe_override.json symbols are added with membership='OVERRIDE'."""
        from ingestion.universe import refresh_universe

        upserted = []
        with patch("ingestion.universe._load_tagging_universe", return_value=["RELIANCE"]), \
             patch("ingestion.universe._load_override", return_value=["NEWSTOCK"]), \
             patch("ingestion.universe.get_instrument_token", return_value="NSE_EQ|FAKE"), \
             patch("ingestion.universe._needs_refresh", return_value=True), \
             patch("ingestion.universe._upsert_universe", side_effect=lambda rows: upserted.extend(rows)):
            refresh_universe(force=True)

        memberships = {r["symbol"]: r["index_membership"] for r in upserted}
        assert memberships.get("RELIANCE") == "BACKTESTER"
        assert memberships.get("NEWSTOCK") == "OVERRIDE"


# ── baselines idempotency ─────────────────────────────────────────────────────

class TestBaselinesIdempotency:
    def _mock_db_coverage(self, total: int, computed: int):
        """Returns a mock _db() whose cursor returns (total,) then (computed,)."""
        mock_conn = MagicMock()
        mock_cur  = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__  = MagicMock(return_value=False)
        mock_cur.__enter__  = MagicMock(return_value=mock_cur)
        mock_cur.__exit__   = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.side_effect = [(total,), (computed,)]
        return mock_conn

    def test_skip_when_coverage_at_threshold(self):
        """≥90% coverage returns True → caller should skip recomputation."""
        from processing.radar_baselines import baselines_already_computed

        with patch("processing.radar_baselines._db",
                   return_value=self._mock_db_coverage(total=200, computed=185)):
            result = baselines_already_computed("2026-06-10")
        assert result is True   # 185/200 = 92.5% ≥ 90%

    def test_no_skip_when_below_threshold(self):
        """<90% coverage returns False → caller should run baselines."""
        from processing.radar_baselines import baselines_already_computed

        with patch("processing.radar_baselines._db",
                   return_value=self._mock_db_coverage(total=200, computed=100)):
            result = baselines_already_computed("2026-06-10")
        assert result is False   # 100/200 = 50% < 90%

    def test_no_skip_when_universe_empty(self):
        """Empty universe (total=0) returns False — cannot infer coverage."""
        from processing.radar_baselines import baselines_already_computed

        with patch("processing.radar_baselines._db",
                   return_value=self._mock_db_coverage(total=0, computed=0)):
            result = baselines_already_computed("2026-06-10")
        assert result is False

    def test_conservative_false_on_db_error(self):
        """DB error during coverage check returns False (safe to re-run)."""
        from processing.radar_baselines import baselines_already_computed

        with patch("processing.radar_baselines._db", side_effect=Exception("DB down")):
            result = baselines_already_computed("2026-06-10")
        assert result is False


# ── Opening Range tracking ────────────────────────────────────────────────────

def _tracker_row(symbol="RELIANCE", ltp=None, high=None, low=None,
                 prev_close=2900.0, rvol=None, volume=0):
    return {"symbol": symbol, "ltp": ltp, "high": high, "low": low,
            "prev_close": prev_close, "rvol": rvol, "volume": volume}


class TestORTracking:
    """Freeze, latch, and break logic via the orb_ranges range framework."""

    def _now_ist(self, h: int, m: int, s: int = 0):
        return datetime(2026, 6, 10, h, m, s, tzinfo=IST)

    BASELINES = {"RELIANCE": {"avg_range_pct_20d": 0.02}}

    def _frozen_tracker(self, fake, or_high=2960.0, or_low=2910.0):
        """Tracker with the default range pre-materialised for RELIANCE."""
        tracker = _make_tracker(fake)
        label   = builtin_default_def()["label"]
        tracker.ranges[label]["materialized"] = {
            "RELIANCE": {"or_high": or_high, "or_low": or_low,
                         "frozen_at": self._now_ist(9, 30).isoformat()},
        }
        return tracker, label

    # -- freeze boundary --

    def test_forming_before_window_end(self):
        """Before 09:30 → status is 'forming', nothing materialised."""
        fake    = FakeCache()
        tracker = _make_tracker(fake)
        rows    = [_tracker_row(ltp=2950.0, high=2960.0, low=2910.0)]
        with patch("realtime.orb_ranges._cache", fake):
            tracker.update(rows, self.BASELINES, self._now_ist(9, 20))
        assert rows[0]["or_status"] == "forming"
        assert rows[0]["or_high"] is None
        label = builtin_default_def()["label"]
        assert tracker.ranges[label]["materialized"] is None

    def test_freezes_at_window_end(self):
        """First update at/after 09:30 freezes from poll history high/low."""
        fake    = FakeCache()
        tracker = _make_tracker(fake)
        with patch("realtime.orb_ranges._cache", fake):
            # Polls during the window: high/low grow to 2960/2910
            for hh, mm, hi, lo in [(9, 16, 2940.0, 2920.0), (9, 22, 2955.0, 2912.0),
                                   (9, 29, 2960.0, 2910.0)]:
                rows = [_tracker_row(ltp=2930.0, high=hi, low=lo)]
                now  = self._now_ist(hh, mm)
                tracker.append_poll_history(rows, now)
                tracker.update(rows, self.BASELINES, now)
            assert rows[0]["or_status"] == "forming"

            # First poll past 09:30 → frozen at the window's history h/l
            rows = [_tracker_row(ltp=2930.0, high=2970.0, low=2905.0)]
            now  = self._now_ist(9, 31)
            tracker.append_poll_history(rows, now)
            tracker.update(rows, self.BASELINES, now)

        assert rows[0]["or_high"] == pytest.approx(2960.0)
        assert rows[0]["or_low"]  == pytest.approx(2910.0)
        assert rows[0]["or_status"] == "inside"
        label = builtin_default_def()["label"]
        assert tracker.ranges[label]["materialized"]["RELIANCE"]["frozen_at"] is not None

    def test_no_update_after_freeze(self):
        """Once frozen, expanding session H/L never changes the OR bounds."""
        fake = FakeCache()
        tracker, label = self._frozen_tracker(fake)
        rows = [_tracker_row(ltp=2935.0, high=2980.0, low=2900.0)]
        with patch("realtime.orb_ranges._cache", fake):
            tracker.update(rows, self.BASELINES, self._now_ist(10, 0))
        assert rows[0]["or_high"] == pytest.approx(2960.0)
        assert rows[0]["or_low"]  == pytest.approx(2910.0)

    # -- break logic (compute_break_status is the single shared latch path) --

    def test_status_broke_up(self):
        status, break_atr = compute_break_status(
            "inside", 1515.0, 1510.0, 1480.0, 1490.0, 0.02)
        assert status == "broke_up"
        assert break_atr is not None and break_atr > 0

    def test_status_broke_down(self):
        status, _ = compute_break_status(
            "inside", 3475.0, 3510.0, 3480.0, 3490.0, 0.02)
        assert status == "broke_down"

    def test_latch_no_revert_to_inside(self):
        """Once 'broke_up', LTP retreating inside OR does NOT revert to 'inside'."""
        status, _ = compute_break_status(
            "broke_up", 1795.0, 1810.0, 1780.0, 1790.0, 0.02)
        assert status == "broke_up"

    def test_double_sided_break(self):
        """'broke_up' followed by LTP below or_low → updates to 'broke_down'."""
        status, _ = compute_break_status(
            "broke_up", 488.0, 510.0, 490.0, 495.0, 0.02)
        assert status == "broke_down"

    def test_tracker_latch_through_cycles(self):
        """End-to-end latch across update() cycles on the default range."""
        fake = FakeCache()
        tracker, label = self._frozen_tracker(fake)
        with patch("realtime.orb_ranges._cache", fake):
            rows = [_tracker_row(ltp=2970.0, high=2975.0, low=2910.0, rvol=2.4)]
            tracker.update(rows, self.BASELINES, self._now_ist(10, 0))
            assert rows[0]["or_status"] == "broke_up"
            # Retreat inside the range — latched
            rows = [_tracker_row(ltp=2940.0, high=2975.0, low=2910.0, rvol=2.0)]
            tracker.update(rows, self.BASELINES, self._now_ist(10, 5))
            assert rows[0]["or_status"] == "broke_up"
            assert rows[0]["ranges"][label]["status"] == "broke_up"


# ── Alert rule evaluation ─────────────────────────────────────────────────────

class TestAlertEvaluation:
    def _row(self, **kwargs) -> dict:
        base = {
            "symbol": "TEST", "ltp": 100.0, "change_pct": 1.0, "gap_pct": 1.0,
            "rvol": 1.0, "atr_multiple": 1.0, "or_status": "inside",
            "or_high": 102.0, "or_low": 98.0, "or_break_atr": None,
            "catalyst_line": None,
        }
        base.update(kwargs)
        return base

    def test_orb_break_volume_fires(self):
        from realtime.radar_alerts import load_rules, _matches
        rules = load_rules()
        orb_rule = next(r for r in rules if r["name"] == "orb_break_volume")
        row = self._row(or_status="broke_up", rvol=2.5)
        assert _matches(row, orb_rule["conditions"])

    def test_orb_break_volume_no_fire_low_rvol(self):
        from realtime.radar_alerts import load_rules, _matches
        rules = load_rules()
        orb_rule = next(r for r in rules if r["name"] == "orb_break_volume")
        row = self._row(or_status="broke_up", rvol=1.5)
        assert not _matches(row, orb_rule["conditions"])

    def test_gap_momentum_fires(self):
        from realtime.radar_alerts import load_rules, _matches
        rules = load_rules()
        gm_rule = next(r for r in rules if r["name"] == "gap_momentum")
        row = self._row(gap_pct=3.0, rvol=2.5)
        assert _matches(row, gm_rule["conditions"])

    def test_range_expansion_fires(self):
        from realtime.radar_alerts import load_rules, _matches
        rules = load_rules()
        re_rule = next(r for r in rules if r["name"] == "range_expansion")
        row = self._row(atr_multiple=2.5, rvol=2.0)
        assert _matches(row, re_rule["conditions"])


# ── Alert dedup ───────────────────────────────────────────────────────────────

class TestAlertDedup:
    def _row(self, symbol="RELIANCE", or_status="broke_up", rvol=3.0):
        return {
            "symbol": symbol, "ltp": 2950.0, "change_pct": 1.5, "gap_pct": 0.5,
            "rvol": rvol, "atr_multiple": 1.5, "or_status": or_status,
            "or_high": 2940.0, "or_low": 2910.0, "or_break_atr": 0.5,
            "catalyst_line": None,
        }

    def test_fires_first_time(self):
        """setnx_ex returns True → alert is sent."""
        from realtime.radar_alerts import process_alerts, load_rules

        rules = load_rules()
        rows  = [self._row()]
        sent  = []

        with patch("realtime.radar_alerts._cache") as mc, \
             patch("realtime.radar_alerts._send_alert", side_effect=sent.append):
            mc.setnx_ex.return_value = True   # first time — key newly set
            process_alerts(rows, rules, "2026-06-10", market_open=True, stale=False)

        assert len(sent) >= 1

    def test_dedup_skips_second_time(self):
        """setnx_ex returns False → already alerted, no send."""
        from realtime.radar_alerts import process_alerts, load_rules

        rules = load_rules()
        rows  = [self._row()]
        sent  = []

        with patch("realtime.radar_alerts._cache") as mc, \
             patch("realtime.radar_alerts._send_alert", side_effect=sent.append):
            mc.setnx_ex.return_value = False  # key already existed
            process_alerts(rows, rules, "2026-06-10", market_open=True, stale=False)

        assert len(sent) == 0

    def test_dedup_redis_backed_restart(self):
        """Simulated restart: Redis still holds the key (returns False) → no re-alert."""
        from realtime.radar_alerts import _is_new_alert

        with patch("realtime.radar_alerts._cache") as mc:
            mc.setnx_ex.return_value = False   # Redis already has this key
            result = _is_new_alert("orb_break_volume", "RELIANCE", "2026-06-10")

        assert result is False


# ── News / catalyst fusion ────────────────────────────────────────────────────

class TestNewsJoin:
    def _universe(self):
        return [
            {"symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "index_membership": "BACKTESTER"},
        ]

    def _quote_data(self):
        return {
            "NSE_EQ|INE002A01018": {
                "last_price": 2950.0,
                "ohlc": {"open": 2920.0, "high": 2960.0, "low": 2910.0, "close": 2900.0},
                "volume": 800_000,
            },
        }

    def test_missing_news_cache_no_crash(self):
        """Empty news_cache → all rows have has_news=False, no crash."""
        http      = MagicMock()
        baselines = {"RELIANCE": _baseline(prev_close=2900.0, avg_vol=2_000_000)}
        written   = {}

        fake = FakeCache()
        with patch("realtime.radar_poller._get_full_quotes", return_value=self._quote_data()), \
             patch("realtime.radar_poller._cache") as mock_cache, \
             patch("realtime.orb_ranges._cache", fake), \
             patch("realtime.radar_poller._session_fraction", return_value=0.5), \
             patch("realtime.radar_poller.process_alerts"):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            result = _poll_cycle(
                http, self._universe(), baselines,
                tracker=_make_tracker(fake), trade_date="2026-06-10",
                news_cache={}, alert_rules=[],
            )

        assert result is True
        snap = written[_SNAPSHOT_KEY]
        row  = snap["rows"][0]
        assert row["has_news"] is False
        assert row["catalyst_line"] is None
        assert row["headline_count"] == 0

    def test_news_fusion_injects_catalyst(self):
        """Symbol present in news_cache → has_news=True and catalyst_line populated."""
        http      = MagicMock()
        baselines = {"RELIANCE": _baseline(prev_close=2900.0, avg_vol=2_000_000)}
        written   = {}
        news      = {"RELIANCE": {"catalyst_line": "Q4 beat; Jio strong", "headline_count": 3}}

        fake = FakeCache()
        with patch("realtime.radar_poller._get_full_quotes", return_value=self._quote_data()), \
             patch("realtime.radar_poller._cache") as mock_cache, \
             patch("realtime.orb_ranges._cache", fake), \
             patch("realtime.radar_poller._session_fraction", return_value=0.5), \
             patch("realtime.radar_poller.process_alerts"):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            _poll_cycle(
                http, self._universe(), baselines,
                tracker=_make_tracker(fake), trade_date="2026-06-10",
                news_cache=news, alert_rules=[],
            )

        row = written[_SNAPSHOT_KEY]["rows"][0]
        assert row["has_news"] is True
        assert row["catalyst_line"] == "Q4 beat; Jio strong"
        assert row["headline_count"] == 3


# ── Redis host configuration ──────────────────────────────────────────────────

class TestRedisHostConfig:
    # Use _env_file=None + pop the env var so neither the project .env (which may have
    # redis_host=redis for Docker) nor the process environment interferes.

    def _minimal(self, **extra):
        from utils.config import Settings
        return Settings(
            _env_file=None,
            anthropic_api_key="x",
            upstox_api_key="x",
            upstox_api_secret="x",
            telegram_bot_token="x",
            telegram_chat_id="x",
            postgres_password="x",
            **extra,
        )

    def test_redis_host_default_is_loopback(self):
        """Code-level default for redis_host is 127.0.0.1 (not Docker's 'redis')."""
        import os
        saved = os.environ.pop("REDIS_HOST", None)
        try:
            s = self._minimal()
            assert s.redis_host == "127.0.0.1"
        finally:
            if saved is not None:
                os.environ["REDIS_HOST"] = saved

    def test_redis_host_env_override(self):
        """REDIS_HOST env var overrides the code default."""
        import os
        os.environ["REDIS_HOST"] = "myredis.internal"
        try:
            s = self._minimal()
            assert s.redis_host == "myredis.internal"
        finally:
            del os.environ["REDIS_HOST"]
