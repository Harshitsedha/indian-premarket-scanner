-- Migration 013 (down): reverse 013_up.sql + 013b_radar_cagg.sql.
--
-- Drop order: continuous aggregate FIRST, then the tables (event_labels before
-- radar_events for the FK; radar_snapshots last). Dropping the cagg removes its
-- refresh policy automatically, and dropping the radar_snapshots hypertable removes
-- its compression policy, jobs, and all chunks automatically.
--
-- We deliberately do NOT call remove_continuous_aggregate_policy /
-- remove_compression_policy explicitly: those take the job advisory lock and can
-- DEADLOCK against an actively-running policy background worker (observed in testing
-- right after the policies are created). The DROPs below cascade-remove the same
-- objects without that contention.
--
-- Idempotent and safe to re-run. Safe on vanilla Postgres too: with no timescaledb
-- extension there is no cagg/hypertable, and plain DROP ... IF EXISTS just drops the
-- tables.

-- Continuous aggregate (also removes its refresh policy).
DROP MATERIALIZED VIEW IF EXISTS radar_snapshots_hourly;

-- Tables (event_labels has an FK to radar_events, so drop it first).
DROP TABLE IF EXISTS event_labels;
DROP TABLE IF EXISTS radar_events;
DROP TABLE IF EXISTS daily_regime;
DROP TABLE IF EXISTS radar_snapshots;   -- dropping the hypertable drops chunks + policies + jobs
