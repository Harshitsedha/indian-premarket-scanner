-- Migration 014 (up): Phase 1 event emission + forward-return labeling support.
--
-- Adds, on top of the Phase 0 spine (013):
--   1. A unique index on radar_events that hardens first-crossing dedup at the DB
--      level (defense-in-depth beyond the poller's Redis setnx): at most ONE event
--      row per (symbol, event_type, IST trade day). A Redis flush mid-session can
--      therefore never create a duplicate event row — the persist worker's
--      `INSERT ... ON CONFLICT DO NOTHING` (no target) absorbs both this constraint
--      and the event_id PK (redelivery).
--   2. Provenance columns on event_labels so the labeler can record HOW it matched a
--      forward price: the actual frame ts it used and the realized offset from the
--      ideal horizon time. Lets sloppy/gap matches be filtered later.
--
-- Idempotent (IF NOT EXISTS everywhere). Plain ANSI/Postgres — no TimescaleDB
-- objects here, so it runs unchanged on vanilla Postgres (portability decision #3).

-- ── radar_events: DB-level first-crossing dedup ─────────────────────────────────
-- DAY-BOUNDARY EXPRESSION (load-bearing — see docs + event_labeler.py):
--   We bucket an event into a trading day with
--       (ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date
--   NOT  (ts AT TIME ZONE 'Asia/Kolkata')::date.
-- Reason: a unique-index expression must be IMMUTABLE. `timezone(text, timestamptz)`
-- (the named-zone form) is only STABLE — Postgres rejects it in an index. The
-- interval form `timezone(interval, timestamptz)` IS IMMUTABLE and, because IST is a
-- fixed +05:30 offset with no DST, is exactly equivalent to Asia/Kolkata for every
-- timestamp. event_labeler.py MUST bucket events with this byte-identical expression
-- so there is no midnight-UTC day-boundary mismatch opening a silent dedup hole.
CREATE UNIQUE INDEX IF NOT EXISTS radar_events_symbol_type_day_uniq
    ON radar_events (
        symbol,
        event_type,
        ((ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date)
    );

-- Labeler lookup: scan recent events to find unlabeled horizons.
CREATE INDEX IF NOT EXISTS idx_radar_events_ts
    ON radar_events (ts);

COMMENT ON INDEX radar_events_symbol_type_day_uniq IS
    'First-crossing dedup: one event per (symbol, event_type, IST trade day). '
    'IST day via IMMUTABLE interval +05:30 form (named-zone form is only STABLE).';

-- ── event_labels: match provenance ──────────────────────────────────────────────
-- matched_ts      = the radar_snapshots frame ts the forward price was actually read
--                   from (NULL when no frame fell within tolerance — a polling gap /
--                   halt; the label's forward_return/mae/mfe are then NULL too).
-- offset_seconds  = signed seconds between matched_ts and the ideal horizon time
--                   (matched_ts - target). Lets later analysis filter loose matches.
ALTER TABLE event_labels
    ADD COLUMN IF NOT EXISTS matched_ts     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS offset_seconds DOUBLE PRECISION;

COMMENT ON COLUMN event_labels.matched_ts IS
    'Frame ts the forward price was read from; NULL = no frame within tolerance (gap).';
COMMENT ON COLUMN event_labels.offset_seconds IS
    'Realized seconds from the ideal horizon time (matched_ts - target_ts).';
