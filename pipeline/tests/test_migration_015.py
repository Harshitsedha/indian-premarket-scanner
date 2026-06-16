"""
String-diff guards for migration 015 (per-range ORB dedup), mirroring the discipline in
test_event_labeler.py for the 014 day-bucket index: assert the load-bearing SQL fragments
are present verbatim rather than trusting they're equivalent.

Behavioural proof (the COALESCE NULL→'' collapse actually dedups two gap_momentum events,
the index creates clean on real Postgres, IMMUTABLE holds, backfill works) is the
integration proof run against a real database — see the Phase 3 report.
"""

from pathlib import Path

_MIG = Path(__file__).resolve().parents[1] / "storage" / "migrations"


def _flat(name: str) -> str:
    return " ".join((_MIG / name).read_text().split())


# ── 015 up ──────────────────────────────────────────────────────────────────────

def test_015_up_adds_range_label_column():
    up = _flat("015_up.sql")
    assert "ADD COLUMN IF NOT EXISTS range_label TEXT" in up


def test_015_up_index_uses_coalesce_collapse():
    """NULL→'' collapse is load-bearing: it keeps non-OR (NULL range) dedup intact."""
    up = _flat("015_up.sql")
    assert "COALESCE(range_label, '')" in up


def test_015_up_uses_byte_identical_immutable_day_bucket():
    """Same IMMUTABLE interval form as migration 014 / event_labeler — verified, not assumed."""
    up = _flat("015_up.sql")
    assert "ts AT TIME ZONE INTERVAL '5 hours 30 minutes' )::date" in up \
        or "ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date" in up


def test_015_up_drops_old_index_and_creates_new():
    up = _flat("015_up.sql")
    assert "DROP INDEX IF EXISTS radar_events_symbol_type_day_uniq" in up
    assert "CREATE UNIQUE INDEX IF NOT EXISTS radar_events_symbol_type_range_day_uniq" in up


def test_015_up_backfills_orb_rows_to_default_label():
    up = _flat("015_up.sql")
    assert "UPDATE radar_events" in up
    assert "SET range_label = '09:15-09:30'" in up
    assert "event_type = 'orb_break_volume'" in up


# ── 015 down ──────────────────────────────────────────────────────────────────────

def test_015_down_restores_014_index_and_drops_column():
    down = _flat("015_down.sql")
    assert "DROP INDEX IF EXISTS radar_events_symbol_type_range_day_uniq" in down
    assert "CREATE UNIQUE INDEX IF NOT EXISTS radar_events_symbol_type_day_uniq" in down
    assert "DROP COLUMN IF EXISTS range_label" in down
