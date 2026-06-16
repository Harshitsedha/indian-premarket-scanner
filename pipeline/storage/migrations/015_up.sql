-- Migration 015 (up): Phase 3 — per-OR-range ORB events.
--
-- Phase 1 emitted one orb_break_volume event per (symbol, IST-day) off the flat default
-- 09:15–09:30 OR status. Phase 3 emits one event per OR WINDOW that broke (default +
-- custom, e.g. 09:45–10:30), so the dedup identity gains a range dimension.
--
-- Adds, on top of 014:
--   1. radar_events.range_label TEXT — the canonical slice key: the OR window string
--      "HH:MM-HH:MM" that broke. The SLICE DIMENSION IS THE WINDOW, not a human name:
--      redefining a range's window yields a new label and starts a new sample; old rows
--      stay under the old label and must not be pooled. NULL for non-OR events
--      (gap_momentum / range_expansion) — they are not OR-window-specific.
--   2. The unique index re-keyed to include the range dimension via COALESCE(range_label,'').
--
-- Plain ANSI/Postgres — no TimescaleDB objects, runs unchanged on vanilla Postgres
-- (portability decision #3). Additive column + index swap; idempotent (IF [NOT] EXISTS).

-- ── 1. additive column ────────────────────────────────────────────────────────────
ALTER TABLE radar_events
    ADD COLUMN IF NOT EXISTS range_label TEXT;

COMMENT ON COLUMN radar_events.range_label IS
    'OR window that broke ("HH:MM-HH:MM") — canonical ORB slice key. The slice dimension '
    'is the WINDOW, not the range name (redefining a window starts a new sample). '
    'NULL for non-OR events (gap_momentum / range_expansion).';

-- ── 2. backfill existing ORB rows to the default window label ───────────────────────
-- Every pre-Phase-3 orb_break_volume row IS a default-window break, so labelling them
-- with the default OR window pools them with new default-range events in a slice.
-- '09:15-09:30' = 09:15 + OR_WINDOW_MINUTES (default 15). If the VPS overrides
-- OR_WINDOW_MINUTES, change this literal to match the default window before applying.
-- gap_momentum / range_expansion rows are intentionally left NULL.
UPDATE radar_events
   SET range_label = '09:15-09:30'
 WHERE event_type = 'orb_break_volume'
   AND range_label IS NULL;

-- ── 3. re-key the first-crossing unique index ───────────────────────────────────────
-- DROP the 014 index and CREATE one that adds the range dimension. COALESCE(range_label,'')
-- (IMMUTABLE) is load-bearing: a plain nullable column in a unique index treats two NULLs
-- as DISTINCT, which would silently break dedup for gap_momentum / range_expansion (NULL
-- range, two/day -> both allowed). Collapsing NULL -> '' keeps their dedup exactly as 014,
-- while ORB events dedup per window. The IST-day bucket is the byte-identical IMMUTABLE
-- interval form from 014 (the named-zone form is only STABLE and unindexable).
-- Safe to create: existing rows were unique under (symbol, event_type, IST-day) and the
-- backfill assigns one deterministic label per group, so no uniqueness violation.
DROP INDEX IF EXISTS radar_events_symbol_type_day_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS radar_events_symbol_type_range_day_uniq
    ON radar_events (
        symbol,
        event_type,
        COALESCE(range_label, ''),
        ((ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date)
    );

COMMENT ON INDEX radar_events_symbol_type_range_day_uniq IS
    'First-crossing dedup, per OR window: one event per '
    '(symbol, event_type, COALESCE(range_label,''''), IST trade day). '
    'COALESCE collapses NULL ranges (non-OR events) to a constant so their dedup is '
    'unchanged from 014; IST day via the IMMUTABLE interval +05:30 form.';
