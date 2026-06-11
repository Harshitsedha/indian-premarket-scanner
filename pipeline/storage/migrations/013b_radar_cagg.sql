-- Migration 013b: continuous aggregate (hourly per-symbol rollup) — OPTIMIZATION ONLY.
--
-- ┌─────────────────────────────────────────────────────────────────────────────┐
-- │  !!! MUST NOT BE RUN INSIDE A TRANSACTION BLOCK !!!                          │
-- │  CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) cannot be        │
-- │  created inside an explicit transaction. Apply by piping into psql WITHOUT   │
-- │  the -1 / --single-transaction flag. The default docker-exec psql            │
-- │  invocation (no -1) runs each statement in its own transaction — which is    │
-- │  exactly what this file needs. This is split out of 013_up.sql precisely so  │
-- │  a future --single-transaction apply of the core schema can never break it.  │
-- └─────────────────────────────────────────────────────────────────────────────┘
--
-- PORTABILITY (decision #3): this continuous aggregate is an OPTIMIZATION LAYER ONLY.
-- No application logic may depend on it — later phases must be able to compute the
-- same hourly rollup directly from radar_snapshots. It is safe to DROP with zero data
-- loss, and it does not exist on vanilla Postgres. See docs/PORTABILITY.md.

-- Fail loudly if someone runs this on plain Postgres (it is Timescale-only; on vanilla
-- PG you simply skip this file — the core schema in 013_up.sql is complete without it).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE EXCEPTION
            'timescaledb extension absent — 013b is Timescale-only; skip it on vanilla Postgres.';
    END IF;
END
$$;

-- Hourly per-symbol rollup of rvol / gap / change. WITH NO DATA so creation does not
-- materialize synchronously; the refresh policy below backfills and keeps it current.
CREATE MATERIALIZED VIEW IF NOT EXISTS radar_snapshots_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    symbol,
    avg(rvol)       AS avg_rvol,
    avg(gap_pct)    AS avg_gap_pct,
    avg(change_pct) AS avg_change_pct,
    count(*)        AS sample_count
FROM radar_snapshots
GROUP BY bucket, symbol
WITH NO DATA;

-- Refresh policy: materialize completed hours, leaving the most recent hour open.
SELECT add_continuous_aggregate_policy(
    'radar_snapshots_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);
