-- Migration 015 (down): revert Phase 3 per-range ORB schema to the 014 shape.
--
-- Drops the range-keyed unique index, restores the 014 (symbol, event_type, IST-day)
-- index, and drops the range_label column.
--
-- CAUTION: recreating the 014 index narrows the dedup identity (removes the range
-- dimension). It SUCCEEDS only while at most one ORB row exists per (symbol, event_type,
-- IST-day). Once multi-range ORB events have accumulated — two windows broken by the same
-- symbol on the same day — the old unique index would find duplicates and the CREATE
-- would FAIL. This down migration is therefore clean only before such rows exist (e.g.
-- an immediate rollback right after applying 015, before the next session emits). This is
-- inherent to narrowing a uniqueness constraint, not a defect.

DROP INDEX IF EXISTS radar_events_symbol_type_range_day_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS radar_events_symbol_type_day_uniq
    ON radar_events (
        symbol,
        event_type,
        ((ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date)
    );

COMMENT ON INDEX radar_events_symbol_type_day_uniq IS
    'First-crossing dedup: one event per (symbol, event_type, IST trade day). '
    'IST day via IMMUTABLE interval +05:30 form (named-zone form is only STABLE).';

ALTER TABLE radar_events
    DROP COLUMN IF EXISTS range_label;
