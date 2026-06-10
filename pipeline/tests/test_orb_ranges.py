"""
Tests for the custom ORB range framework:

- compute_window_hl: anchored vs non-anchored (10:00-10:45) materialisation
- RangeTracker: forming -> frozen transition at or_end, custom-range snapshot cells
- Break latch + first_break_side + break_time + rvol_at_break + break_atr_max capture
- API: max 6 active ranges, time-window validation, default range delete protection
- load_range_defs: session/standard scoping query contract, DB-error fallback
- orb_eod: idempotent double-run, skip-deletion on row-count mismatch,
  session-range deactivation only after verified persist
- Default range backward compat: snapshot rows still carry or_high/or_low/or_status
"""

import sys
from datetime import datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime.orb_ranges import (
    RangeTracker,
    builtin_default_def,
    compute_window_hl,
    load_range_defs,
    range_label,
    MAX_ACTIVE_RANGES,
)
from realtime.radar_poller import _poll_cycle, _SNAPSHOT_KEY, IST
from tests.test_radar import FakeCache, _make_tracker, _tracker_row, _baseline, _entry

TRADE_DATE = "2026-06-10"


def _now(h, m, s=0):
    return datetime(2026, 6, 10, h, m, s, tzinfo=IST)


def _custom_def(start=dtime(10, 0), end=dtime(10, 45), name="Mid-morning", rid=2,
                scope="session"):
    return {"id": rid, "name": name, "start": start, "end": end,
            "label": range_label(start, end), "scope": scope, "is_default": False}


def _poll_rec(t, p, h, l, v=1000):
    return {"t": t, "p": p, "h": h, "l": l, "v": v}


# ── window high/low materialisation ──────────────────────────────────────────

class TestWindowHL:
    def test_anchored_uses_day_high_low(self):
        """Anchored (09:15) window: day-cumulative h/l at last poll IS the OR."""
        records = [
            _poll_rec("09:16:00", 100.0, 101.0, 99.5),
            _poll_rec("09:29:30", 100.5, 103.0, 99.0),
        ]
        hi, lo = compute_window_hl(records, anchored=True)
        assert hi == pytest.approx(103.0)
        assert lo == pytest.approx(99.0)

    def test_non_anchored_uses_price_samples(self):
        """10:00-10:45 window: day h/l predates the window — use sampled prices."""
        records = [
            _poll_rec("10:05:00", 98.0,  105.0, 95.0),
            _poll_rec("10:20:00", 102.0, 105.0, 95.0),
            _poll_rec("10:44:00", 101.0, 105.0, 95.0),
        ]
        hi, lo = compute_window_hl(records, anchored=False)
        assert hi == pytest.approx(102.0)   # NOT the 105 day high from before 10:00
        assert lo == pytest.approx(98.0)    # NOT the 95 day low from before 10:00

    def test_non_anchored_day_extreme_moved_during_window(self):
        """A new day high/low set INSIDE the window extends the sampled extremes."""
        records = [
            _poll_rec("10:05:00", 98.0,  105.0, 95.0),
            _poll_rec("10:44:00", 101.0, 107.0, 93.0),   # day h/l moved in-window
        ]
        hi, lo = compute_window_hl(records, anchored=False)
        assert hi == pytest.approx(107.0)
        assert lo == pytest.approx(93.0)

    def test_empty_records(self):
        assert compute_window_hl([], anchored=False) == (None, None)


# ── forming -> frozen transition ──────────────────────────────────────────────

class TestRangeMaterialisation:
    BASELINES = {"RELIANCE": {"avg_range_pct_20d": 0.02}}

    def test_custom_range_forming_then_frozen(self):
        """A 10:00-10:45 range is 'forming' until 10:45, then frozen from history."""
        fake    = FakeCache()
        d       = _custom_def()
        tracker = _make_tracker(fake, defs=[builtin_default_def(), d])

        with patch("realtime.orb_ranges._cache", fake):
            # Pre-window poll at 09:30 (day high 105 set here)
            rows = [_tracker_row(ltp=100.0, high=105.0, low=95.0, prev_close=100.0)]
            tracker.append_poll_history(rows, _now(9, 30))

            # In-window polls
            for hh, mm, p in [(10, 5, 98.0), (10, 20, 102.0), (10, 44, 101.0)]:
                rows = [_tracker_row(ltp=p, high=105.0, low=95.0, prev_close=100.0)]
                now  = _now(hh, mm)
                tracker.append_poll_history(rows, now)
                tracker.update(rows, self.BASELINES, now)
                assert rows[0]["ranges"][d["label"]]["status"] == "forming"

            assert tracker.ranges[d["label"]]["materialized"] is None

            # First poll past 10:45 → frozen from window samples (102/98)
            rows = [_tracker_row(ltp=101.0, high=105.0, low=95.0, prev_close=100.0)]
            now  = _now(10, 46)
            tracker.append_poll_history(rows, now)
            tracker.update(rows, self.BASELINES, now)

        m = tracker.ranges[d["label"]]["materialized"]["RELIANCE"]
        assert m["or_high"] == pytest.approx(102.0)
        assert m["or_low"]  == pytest.approx(98.0)
        assert rows[0]["ranges"][d["label"]]["status"] == "inside"
        # Persisted to Redis with a freeze marker — never recomputed
        or_hash = fake.hashes[f"radar:or:{TRADE_DATE}:{d['label']}"]
        assert "_meta" in or_hash and "RELIANCE" in or_hash

    def test_materialised_range_survives_restart(self):
        """A new tracker (poller restart) reloads the frozen OR from Redis."""
        fake = FakeCache()
        d    = _custom_def()
        fake.hashes[f"radar:or:{TRADE_DATE}:{d['label']}"] = {
            "_meta":    {"frozen_at": "x"},
            "RELIANCE": {"or_high": 102.0, "or_low": 98.0, "frozen_at": "x"},
        }
        tracker = _make_tracker(fake, defs=[d])
        assert tracker.ranges[d["label"]]["materialized"]["RELIANCE"]["or_high"] == 102.0


# ── break tracking: first break capture + running max ─────────────────────────

class TestBreakCapture:
    BASELINES = {"RELIANCE": {"avg_range_pct_20d": 0.02}}

    def _frozen(self, fake):
        d       = _custom_def()
        tracker = _make_tracker(fake, defs=[d])
        tracker.ranges[d["label"]]["materialized"] = {
            "RELIANCE": {"or_high": 102.0, "or_low": 98.0, "frozen_at": "x"},
        }
        return tracker, d["label"]

    def test_first_break_capture_and_max_extension(self):
        fake = FakeCache()
        tracker, label = self._frozen(fake)
        with patch("realtime.orb_ranges._cache", fake):
            # Cycle 1: first break up, RVOL 3.1
            rows = [_tracker_row(ltp=103.0, high=104.0, low=95.0, prev_close=100.0, rvol=3.1)]
            tracker.update(rows, self.BASELINES, _now(11, 0))
            st = tracker.ranges[label]["state"]["RELIANCE"]
            assert st["first_break_side"] == "up"
            assert st["rvol_at_break"] == pytest.approx(3.1)
            assert "11:00" in st["break_time"]
            first_max = st["break_atr_max"]
            assert first_max > 0

            # Cycle 2: deeper extension, higher RVOL — first-break context unchanged
            rows = [_tracker_row(ltp=105.0, high=105.5, low=95.0, prev_close=100.0, rvol=4.0)]
            tracker.update(rows, self.BASELINES, _now(11, 5))
            st = tracker.ranges[label]["state"]["RELIANCE"]
            assert st["rvol_at_break"] == pytest.approx(3.1)   # captured at FIRST break
            assert "11:00" in st["break_time"]
            assert st["break_atr_max"] > first_max
            assert st["broke_up"] is True and not st.get("broke_down")

            # Cycle 3: opposite-side break — both flags end up True, first side stays "up"
            rows = [_tracker_row(ltp=97.0, high=105.5, low=96.5, prev_close=100.0, rvol=2.0)]
            tracker.update(rows, self.BASELINES, _now(11, 10))
            st = tracker.ranges[label]["state"]["RELIANCE"]
            assert st["first_break_side"] == "up"
            assert st["broke_up"] is True and st["broke_down"] is True
            assert rows[0]["ranges"][label]["status"] == "broke_down"

        # State persisted to Redis for restart-safety
        assert "RELIANCE" in fake.hashes[f"radar:or_state:{TRADE_DATE}:{label}"]


# ── range def loading: scoping + fallback ─────────────────────────────────────

class TestRangeDefLoading:
    def test_query_scopes_session_ranges_to_trade_date(self):
        """The SQL only admits standard defs OR session defs for THIS date."""
        mock_conn = MagicMock()
        mock_cur  = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            (1, "Default OR 15m", dtime(9, 15), dtime(9, 30), "standard"),
            (2, "Mid-morning",    dtime(10, 0), dtime(10, 45), "session"),
        ]
        with patch("realtime.orb_ranges.psycopg2.connect", return_value=mock_conn):
            defs = load_range_defs(TRADE_DATE)

        sql, params = mock_cur.execute.call_args[0]
        assert "scope = 'standard'" in sql
        assert "scope = 'session' AND session_date = %s" in sql
        assert "active = TRUE" in sql
        assert params == (TRADE_DATE,)

        assert [d["label"] for d in defs] == ["09:15-09:30", "10:00-10:45"]
        assert defs[0]["is_default"] is True
        assert defs[1]["is_default"] is False

    def test_db_error_falls_back_to_builtin_default(self):
        with patch("realtime.orb_ranges.psycopg2.connect", side_effect=Exception("no db")):
            defs = load_range_defs(TRADE_DATE)
        assert len(defs) == 1
        assert defs[0]["label"] == builtin_default_def()["label"]
        assert defs[0]["is_default"] is True

    def test_cap_at_max_active_ranges(self):
        mock_conn = MagicMock()
        mock_cur  = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = [
            (i, f"r{i}", dtime(9, 15 + i), dtime(10, 15 + i), "standard")
            for i in range(MAX_ACTIVE_RANGES + 3)
        ]
        with patch("realtime.orb_ranges.psycopg2.connect", return_value=mock_conn):
            defs = load_range_defs(TRADE_DATE)
        assert len(defs) == MAX_ACTIVE_RANGES


# ── API validation ────────────────────────────────────────────────────────────

class TestRangeApi:
    def _req(self, **kw):
        from api.main import CreateRangeRequest
        base = {"name": "Test", "or_start": "10:00", "or_end": "10:45", "scope": "session"}
        base.update(kw)
        return CreateRangeRequest(**base)

    def test_max_six_active_ranges_rejected(self):
        from fastapi import HTTPException
        from api.main import create_radar_range
        six = [{"id": i} for i in range(6)]
        with patch("api.main._load_active_ranges", return_value=six):
            with pytest.raises(HTTPException) as exc:
                create_radar_range(self._req())
        assert exc.value.status_code == 422
        assert "Max 6" in str(exc.value.detail)

    @pytest.mark.parametrize("patch_kw, msg", [
        ({"or_start": "09:00"},                  "or_start"),
        ({"or_end": "15:45"},                    "or_end"),
        ({"or_start": "11:00", "or_end": "10:00"}, "after"),
        ({"or_end": "10:00", "or_start": "10:00"}, "after"),
        ({"scope": "weekly"},                    "scope"),
        ({"name": "  "},                         "name"),
        ({"or_start": "abc"},                    "or_start"),
    ])
    def test_validation_rejected(self, patch_kw, msg):
        from fastapi import HTTPException
        from api.main import create_radar_range
        with pytest.raises(HTTPException) as exc:
            create_radar_range(self._req(**patch_kw))
        assert exc.value.status_code == 422
        assert msg in str(exc.value.detail)

    def test_default_range_delete_protected(self):
        from fastapi import HTTPException
        from api.main import delete_radar_range
        mock_conn = MagicMock()
        mock_cur  = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = (dtime(9, 15), dtime(9, 30), "standard")
        with patch("api.main._db", return_value=mock_conn):
            with pytest.raises(HTTPException) as exc:
                delete_radar_range(1)
        assert exc.value.status_code == 403

    def test_custom_range_delete_soft_deletes(self):
        from api.main import delete_radar_range
        mock_conn = MagicMock()
        mock_cur  = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = (dtime(10, 0), dtime(10, 45), "session")
        mock_cur.rowcount = 1
        with patch("api.main._db", return_value=mock_conn):
            result = delete_radar_range(7)
        assert result == {"deleted": 7}
        update_sql = mock_cur.execute.call_args_list[-1][0][0]
        assert "active = FALSE" in update_sql


# ── EOD job ───────────────────────────────────────────────────────────────────

class _FakeEodCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "SELECT COUNT(*) FROM orb_history" in sql:
            n = self.db.count_override
            self._result = (len(self.db.store) if n is None else n,)
        elif "UPDATE orb_range_defs" in sql:
            self.db.deactivated += 1
            self.rowcount = 1

    def fetchone(self):
        return self._result


class _FakeEodDB:
    """Captures execute_values upserts in a PK-keyed dict (ON CONFLICT semantics)."""

    def __init__(self, count_override=None):
        self.store: dict = {}
        self.deactivated = 0
        self.count_override = count_override

    def cursor(self):
        return _FakeEodCursor(self)

    def commit(self):
        pass

    def close(self):
        pass


def _fake_execute_values(cur, sql, rows, page_size=100):
    assert "ON CONFLICT" in sql
    for r in rows:
        cur.db.store[(r[0], r[1], r[2], r[3])] = r


class TestOrbEod:
    def _seed_cache(self, fake, label="09:15-09:30"):
        fake.hashes[f"radar:or:{TRADE_DATE}:{label}"] = {
            "_meta":    {"frozen_at": "x"},
            "RELIANCE": {"or_high": 2960.0, "or_low": 2910.0, "frozen_at": "x"},
            "INFY":     {"or_high": 1510.0, "or_low": 1480.0, "frozen_at": "x"},
        }
        fake.hashes[f"radar:or_state:{TRADE_DATE}:{label}"] = {
            "RELIANCE": {"or_status": "broke_up", "broke_up": True,
                         "first_break_side": "up",
                         "break_time": "2026-06-10T10:00:00+05:30",
                         "rvol_at_break": 2.4, "break_atr_max": 0.8},
        }
        fake.kv[f"radar:day_meta:{TRADE_DATE}"] = {
            "RELIANCE": {"gap_pct": 1.2, "has_news": True, "ltp": 2970.0, "prev_close": 2900.0},
            "INFY":     {"gap_pct": -0.4, "has_news": False, "ltp": 1490.0, "prev_close": 1495.0},
        }
        fake.lists[f"radar:polls:{TRADE_DATE}:RELIANCE"] = [{"t": "09:16:00"}]

    def _defs(self):
        return [
            builtin_default_def(),
            # session-scoped def so deactivation is observable
        ]

    def _run(self, fake, db):
        from processing import orb_eod
        with patch("processing.orb_eod._cache", fake), \
             patch("processing.orb_eod._db", return_value=db), \
             patch("processing.orb_eod.load_range_defs", return_value=self._defs()), \
             patch("psycopg2.extras.execute_values", _fake_execute_values):
            return orb_eod.run_orb_eod(TRADE_DATE)

    def test_persists_and_cleans_when_verified(self):
        fake = FakeCache()
        self._seed_cache(fake)
        db = _FakeEodDB()
        result = self._run(fake, db)

        assert result["persisted"] == 2 and result["verified"] and result["cleaned"]
        assert len(db.store) == 2
        rel = db.store[("RELIANCE", TRADE_DATE, "09:15:00", "09:30:00")]
        # broke_up, first_break_side, rvol_at_break, gap, news, eod_close joined in
        assert rel[7] is True and rel[9] == "up"
        assert rel[12] == pytest.approx(2.4)
        assert rel[13] == pytest.approx(1.2) and rel[14] is True
        assert rel[15] == pytest.approx(2970.0)
        assert db.deactivated == 1
        # Redis cleaned: polls, or, or_state, day_meta all gone
        assert not fake.lists and not fake.hashes
        assert f"radar:day_meta:{TRADE_DATE}" not in fake.kv

    def test_double_run_is_idempotent(self):
        """Re-running after a verified persist is a clean no-op — no duplicates."""
        fake = FakeCache()
        self._seed_cache(fake)
        db = _FakeEodDB()
        first  = self._run(fake, db)
        second = self._run(fake, db)
        assert first["persisted"] == 2
        assert second["persisted"] == 0          # Redis already cleaned
        assert len(db.store) == 2                # PK upsert — still exactly 2 rows

    def test_rerun_after_failed_verification_no_duplicates(self):
        """Failed verification keeps Redis; the retry upserts the same PKs."""
        fake = FakeCache()
        self._seed_cache(fake)
        db = _FakeEodDB(count_override=1)        # simulate missing rows
        first = self._run(fake, db)
        assert first["verified"] is False
        assert fake.hashes                        # nothing deleted

        db.count_override = None                  # DB healthy now
        second = self._run(fake, db)
        assert second["verified"] is True
        assert len(db.store) == 2                 # no duplicates from the re-run

    def test_mismatch_skips_deletion_and_deactivation(self):
        fake = FakeCache()
        self._seed_cache(fake)
        db = _FakeEodDB(count_override=1)
        result = self._run(fake, db)

        assert result["verified"] is False and result["cleaned"] is False
        assert db.deactivated == 0
        assert f"radar:or:{TRADE_DATE}:09:15-09:30" in fake.hashes
        assert f"radar:polls:{TRADE_DATE}:RELIANCE" in fake.lists
        assert f"radar:day_meta:{TRADE_DATE}" in fake.kv

    def test_no_poll_data_exits_cleanly(self):
        fake = FakeCache()   # nothing materialised
        db = _FakeEodDB()
        result = self._run(fake, db)
        assert result == {"persisted": 0, "expected": 0, "verified": False, "cleaned": False}
        assert len(db.store) == 0 and db.deactivated == 0


# ── default range backward compat through the full poll cycle ─────────────────

class TestSnapshotBackwardCompat:
    def test_snapshot_rows_keep_default_or_fields_and_gain_ranges(self):
        """or_high/or_low/or_status/or_break_atr still populated from the default
        range; rows additionally carry the per-range 'ranges' dict."""
        universe = [{"symbol": "RELIANCE", "instrument_key": "NSE_EQ|X", "index_membership": "NIFTY100"}]
        quotes   = {"NSE_EQ|X": _entry(ltp=2970.0, open_=2920.0, high=2975.0, low=2910.0,
                                       volume=800_000, prev_close=2900.0)}
        baselines = {"RELIANCE": _baseline(prev_close=2900.0, avg_vol=2_000_000)}

        fake = FakeCache()
        default = builtin_default_def()
        custom  = _custom_def()
        tracker = _make_tracker(fake, defs=[default, custom])
        # Both ranges already frozen (mid-session restart scenario)
        tracker.ranges[default["label"]]["materialized"] = {
            "RELIANCE": {"or_high": 2960.0, "or_low": 2910.0, "frozen_at": "x"}}
        tracker.ranges[custom["label"]]["materialized"] = {
            "RELIANCE": {"or_high": 2965.0, "or_low": 2940.0, "frozen_at": "x"}}

        written = {}
        mock_cache = MagicMock()
        with patch("realtime.radar_poller._get_full_quotes", return_value=quotes), \
             patch("realtime.radar_poller._cache", mock_cache), \
             patch("realtime.orb_ranges._cache", fake), \
             patch("realtime.radar_poller._session_fraction", return_value=0.5), \
             patch("realtime.radar_poller.process_alerts"):
            mock_cache.set_ex.side_effect = lambda k, v, ttl: written.update({k: v})
            assert _poll_cycle(MagicMock(), universe, baselines,
                               tracker=tracker, trade_date=TRADE_DATE,
                               news_cache={}, alert_rules=[]) is True

        row = written[_SNAPSHOT_KEY]["rows"][0]
        # Legacy fields from the default range — alert rules + UI keep working
        assert row["or_high"] == pytest.approx(2960.0)
        assert row["or_low"]  == pytest.approx(2910.0)
        assert row["or_status"] == "broke_up"
        assert row["or_break_atr"] is not None and row["or_break_atr"] > 0
        # New per-range cells
        assert row["ranges"][default["label"]]["status"] == "broke_up"
        assert row["ranges"][custom["label"]]["status"] == "broke_up"
        # Per-poll history appended
        assert fake.lists[f"radar:polls:{TRADE_DATE}:RELIANCE"]
        # Day meta written for the EOD job
        assert f"radar:day_meta:{TRADE_DATE}" in written
        assert written[f"radar:day_meta:{TRADE_DATE}"]["RELIANCE"]["gap_pct"] is not None
