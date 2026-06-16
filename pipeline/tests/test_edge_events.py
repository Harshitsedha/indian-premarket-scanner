"""
Tests for processing.edge_events (Phase 2 /edge event table).

The load-bearing test is test_day_bucket_expr_byte_identical: the date filter must use
the EXACT same interval-literal day-bucket expression as migration 014 and
event_labeler — verified by string diff, NOT by assuming INTERVAL '5 hours 30 minutes'
is equivalent to other forms — so it buckets identically and can hit the unique index.
"""

import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import pytest

from processing import edge_events as ee


# ── day-bucket expression equivalence (verify by diffing strings) ─────────────────

_MIGRATION_014 = _PIPELINE / "storage" / "migrations" / "014_up.sql"


def test_day_bucket_expr_byte_identical_to_labeler():
    from processing.event_labeler import _IST_DAY_EXPR
    assert ee.IST_DAY_EXPR == _IST_DAY_EXPR
    assert ee.IST_DAY_EXPR == "(ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date"


def test_day_bucket_expr_present_in_migration_014():
    mig = _MIGRATION_014.read_text()
    # Collapse whitespace the way the migration wraps the expression across lines and
    # assert the exact interval literal appears (string diff, not semantic assumption).
    flat = " ".join(mig.split())
    assert "ts AT TIME ZONE INTERVAL '5 hours 30 minutes' )::date" in flat \
        or "ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date" in flat


def test_filter_uses_exact_interval_literal_in_where():
    f = ee.EdgeFilters(date_from="2026-06-01", date_to="2026-06-17")
    clause, params = f.where()
    assert "(e.ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date >= %s" in clause
    assert "(e.ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date <= %s" in clause
    assert len(params) == 2


# ── WHERE building ────────────────────────────────────────────────────────────────

def test_empty_filters_no_where():
    clause, params = ee.EdgeFilters().where()
    assert clause == ""
    assert params == []


def test_symbol_uppercased_and_event_type():
    f = ee.EdgeFilters(symbol="tatasteel", event_type="gap_momentum", direction="up")
    clause, params = f.where()
    assert "e.symbol = %s" in clause
    assert "e.event_type = %s" in clause
    assert "e.trigger->>'direction' = %s" in clause
    assert params == ["TATASTEEL", "gap_momentum", "up"]


def test_range_label_filter_is_surfaced_and_filterable():
    # range_label is a first-class surfaced column, sortable, and filterable on the
    # real radar_events.range_label column (Phase 3 ORB slice key).
    assert "range_label" in ee.SURFACED_COLUMNS
    assert "range_label" in ee.SORT_KEYS
    f = ee.EdgeFilters(range_label="09:45-10:30")
    clause, params = f.where()
    assert "e.range_label = %s" in clause
    assert params == ["09:45-10:30"]


def test_range_label_in_export_schema():
    assert "range_label" in ee.export_columns()


def test_numeric_band_filters():
    f = ee.EdgeFilters(rvol_min=2.0, gap_min=1.0, gap_max=5.0)
    clause, params = f.where()
    assert "(e.trigger->>'rvol')::float8 >= %s" in clause
    assert "(e.trigger->>'gap_pct')::float8 >= %s" in clause
    assert "(e.trigger->>'gap_pct')::float8 <= %s" in clause
    assert params == [2.0, 1.0, 5.0]


def test_invalid_date_raises():
    with pytest.raises(ValueError):
        ee.EdgeFilters(date_from="2026/06/01")


# ── column schema / pivot ──────────────────────────────────────────────────────────

def test_export_columns_schema():
    cols = ee.export_columns()
    # surfaced
    for c in ["event_id", "ts", "symbol", "event_type", "direction", "rvol",
              "gap_pct", "atr_multiple", "or_status", "entry_price", "regime_id"]:
        assert c in cols
    # every horizon × {ret,mae,mfe}
    for h in ("5m", "15m", "30m", "eod"):
        assert f"ret_{h}" in cols and f"mae_{h}" in cols and f"mfe_{h}" in cols
    # offset only for minute horizons, never eod
    assert "offset_5m" in cols and "offset_15m" in cols and "offset_30m" in cols
    assert "offset_eod" not in cols


def test_label_columns_exclude_offsets_for_table():
    table_cols = ee.label_columns(include_offsets=False)
    assert not any(c.startswith("offset_") for c in table_cols)


def test_pivot_uses_max_filter_for_null_vs_zero_distinctness():
    # The NULL-vs-0 guarantee rests on max(col) FILTER (WHERE horizon=...): an absent
    # label aggregates to SQL NULL while a present 0.0 stays 0.0.
    sql, _ = ee._build_query(ee.EdgeFilters(), sort="ts", ascending=False,
                             include_offsets=False)
    assert "max(l.forward_return) FILTER (WHERE l.horizon = '5m')" in sql
    assert "LEFT JOIN event_labels l" in sql


def test_unknown_sort_rejected():
    with pytest.raises(ValueError):
        ee._build_query(ee.EdgeFilters(), sort="; DROP TABLE", ascending=True,
                        include_offsets=False)


def test_default_sort_and_keys():
    assert ee.DEFAULT_SORT == "ts"
    assert "ret_eod" in ee.SORT_KEYS
    assert "entry_price" in ee.SORT_KEYS
