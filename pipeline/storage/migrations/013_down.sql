-- Migration 013 (down): reverse 013_up.sql + 013b_radar_cagg.sql.
--
-- Drop order matters: continuous aggregate (and its refresh policy) FIRST, then the
-- compression policy, then the tables (dropping radar_snapshots drops the hypertable
-- and all its chunks). FK order: event_labels before radar_events.
--
-- Idempotent and safe to re-run. Safe on vanilla Postgres: all Timescale calls are
-- guarded by an extension check, so running this where 013 was applied in plain-PG
-- mode (no hypertable/cagg) just drops the tables.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        -- Remove the continuous aggregate's refresh policy (the policy is also dropped
        -- automatically when the cagg is dropped; this is belt-and-suspenders + idempotent).
        IF EXISTS (
            SELECT 1 FROM timescaledb_information.continuous_aggregates
            WHERE view_name = 'radar_snapshots_hourly'
        ) THEN
            PERFORM remove_continuous_aggregate_policy('radar_snapshots_hourly', if_exists => TRUE);
        END IF;

        -- Remove the compression policy if radar_snapshots is still a hypertable.
        BEGIN
            PERFORM remove_compression_policy('radar_snapshots', if_exists => TRUE);
        EXCEPTION
            WHEN undefined_table THEN NULL;   -- table already gone
        END;
    END IF;
END
$$;

-- Continuous aggregate (also removes its policy).
DROP MATERIALIZED VIEW IF EXISTS radar_snapshots_hourly;

-- Tables (event_labels has an FK to radar_events, so drop it first).
DROP TABLE IF EXISTS event_labels;
DROP TABLE IF EXISTS radar_events;
DROP TABLE IF EXISTS daily_regime;
DROP TABLE IF EXISTS radar_snapshots;   -- dropping the hypertable drops all chunks
