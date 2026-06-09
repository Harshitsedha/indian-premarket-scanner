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

    def test_success_writes_snapshot(self):
        """Successful poll writes radar:snapshot to Redis."""
        http = MagicMock()
        baselines = {
            "RELIANCE": _baseline(prev_close=2900.0, avg_vol=2_000_000),
            "INFY":     _baseline(prev_close=1490.0, avg_vol=1_000_000),
        }
        written = {}

        with patch("realtime.radar_poller._get_full_quotes", return_value=self._quote_data()), \
             patch("realtime.radar_poller._cache") as mock_cache, \
             patch("realtime.radar_poller._session_fraction", return_value=0.5):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            result = _poll_cycle(http, self._universe(), baselines)

        assert result is True
        assert _SNAPSHOT_KEY in written
        snap = written[_SNAPSHOT_KEY]
        assert "generated_at" in snap
        assert len(snap["rows"]) == 2
        symbols = {r["symbol"] for r in snap["rows"]}
        assert "RELIANCE" in symbols and "INFY" in symbols

    def test_partial_symbols_missing(self):
        """If some instrument_keys are absent from the quote response, those rows are skipped."""
        partial_data = {
            "NSE_EQ|INE002A01018": _entry(),   # only RELIANCE, no INFY
        }
        http      = MagicMock()
        baselines = {"RELIANCE": _baseline()}
        written   = {}

        with patch("realtime.radar_poller._get_full_quotes", return_value=partial_data), \
             patch("realtime.radar_poller._cache") as mock_cache, \
             patch("realtime.radar_poller._session_fraction", return_value=0.5):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            result = _poll_cycle(http, self._universe(), baselines)

        assert result is True
        snap = written[_SNAPSHOT_KEY]
        assert len(snap["rows"]) == 1
        assert snap["rows"][0]["symbol"] == "RELIANCE"

    def test_401_sets_status_and_returns_false(self):
        """401 response sets radar:status, sends Telegram alert, returns False."""
        http = MagicMock()
        written = {}

        with patch("realtime.radar_poller._get_full_quotes", return_value={"_401": True}), \
             patch("realtime.radar_poller._cache") as mock_cache, \
             patch("realtime.radar_poller._send_token_alert") as mock_alert:
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            result = _poll_cycle(http, self._universe(), {})

        assert result is False
        mock_alert.assert_called_once()
        assert _STATUS_KEY in written
        assert written[_STATUS_KEY]["state"] == "token_expired"
        # snapshot must NOT have been written on 401
        assert _SNAPSHOT_KEY not in written

    def test_none_response_does_not_crash(self):
        """None from _get_full_quotes (network error) returns True and writes nothing."""
        http = MagicMock()
        with patch("realtime.radar_poller._get_full_quotes", return_value=None), \
             patch("realtime.radar_poller._cache") as mock_cache:
            result = _poll_cycle(http, self._universe(), {})

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
        with patch("api.main._cache") as mock_cache:
            mock_cache.get.side_effect = lambda k: old_snapshot if k == "radar:snapshot" else {}
            result = radar_endpoint()
        assert result["stale"] is True

    def test_not_stale_when_snapshot_is_recent(self):
        """snapshot generated <60s ago → stale=False."""
        from api.main import radar as radar_endpoint

        fresh_snapshot = self._make_snapshot(30)
        with patch("api.main._cache") as mock_cache:
            mock_cache.get.side_effect = lambda k: fresh_snapshot if k == "radar:snapshot" else {}
            result = radar_endpoint()
        assert result["stale"] is False

    def test_no_snapshot_returns_none_not_stale(self):
        """No snapshot in Redis → snapshot=None, stale=False."""
        from api.main import radar as radar_endpoint

        with patch("api.main._cache") as mock_cache:
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
