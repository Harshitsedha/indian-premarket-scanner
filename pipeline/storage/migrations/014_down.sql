-- Migration 014 (down): reverse 014_up.sql.
--
-- Idempotent and safe to re-run. Drops only what 014_up added; the Phase 0 spine
-- tables (013) are untouched.

DROP INDEX IF EXISTS radar_events_symbol_type_day_uniq;
DROP INDEX IF EXISTS idx_radar_events_ts;

ALTER TABLE event_labels
    DROP COLUMN IF EXISTS offset_seconds,
    DROP COLUMN IF EXISTS matched_ts;
