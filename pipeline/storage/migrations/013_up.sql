-- Migration 013 (up): Edge page data spine — radar_snapshots hypertable + 3 empty tables.
--
-- PORTABILITY (decision #3): the four CREATE TABLE statements below are plain
-- ANSI/Postgres SQL and run on vanilla Postgres unchanged. The TimescaleDB-specific
-- steps (hypertable conversion + compression) are isolated in the guarded DO block at
-- the end and are SKIPPED automatically when the timescaledb extension is absent, so
-- the schema is identical on plain Postgres. The continuous aggregate is a SEPARATE
-- file (013b_radar_cagg.sql) because it cannot be created inside a transaction.
-- See docs/PORTABILITY.md.
--
-- Idempotent: safe to re-run (IF NOT EXISTS everywhere; if_not_exists => TRUE on the
-- Timescale calls). NO retention/drop policy is defined anywhere — data is kept
-- indefinitely (decision #4). Dense storage: every frame, every symbol, every 45s.

-- ── radar_snapshots — one row per symbol per 45s poll frame (the only table wired
--    this phase). Columns mirror every per-symbol metric the radar poller computes;
--    none is trivially recomputable from price alone, so all are stored (dense).
CREATE TABLE IF NOT EXISTS radar_snapshots (
    ts                   TIMESTAMPTZ      NOT NULL,   -- frame timestamp (poller "generated_at"), shared by all rows in a frame
    symbol               TEXT             NOT NULL,
    price                DOUBLE PRECISION,            -- live LTP (poller row "ltp")
    prev_close           DOUBLE PRECISION,
    open_price           DOUBLE PRECISION,            -- today's open (poller row "open")
    high                 DOUBLE PRECISION,
    low                  DOUBLE PRECISION,
    volume               BIGINT,
    gap_pct              DOUBLE PRECISION,
    change_pct           DOUBLE PRECISION,
    change_from_open_pct DOUBLE PRECISION,
    rvol                 DOUBLE PRECISION,
    range_used_pct       DOUBLE PRECISION,
    atr_mult             DOUBLE PRECISION,            -- poller row "atr_multiple"
    index_membership     TEXT,                        -- NIFTY100 | MIDCAP150 | BOTH | OVERRIDE
    has_news             BOOLEAN,
    catalyst_line        TEXT,
    headline_count       INTEGER,
    -- Natural key for idempotent writes: a frame stamps all its rows with the same ts,
    -- so (ts, symbol) is unique per frame. Includes ts (the future partition column),
    -- which TimescaleDB requires for any unique constraint on the hypertable.
    CONSTRAINT radar_snapshots_ts_symbol_uniq UNIQUE (ts, symbol)
);

-- Symbol-scoped most-recent-first lookups (slice explorer, edge-decay queries).
CREATE INDEX IF NOT EXISTS idx_radar_snapshots_symbol_ts
    ON radar_snapshots (symbol, ts DESC);

COMMENT ON TABLE radar_snapshots IS
    'Dense intraday radar frames: one row per symbol per 45s poll. No downsampling, kept indefinitely.';

-- ── radar_events — Phase 1 emits rows; created EMPTY now so event_id types are locked.
--    event_id is the identity that threads radar -> setup -> outcome -> journal trade.
CREATE TABLE IF NOT EXISTS radar_events (
    event_id   UUID PRIMARY KEY,
    ts         TIMESTAMPTZ,
    symbol     TEXT,
    event_type TEXT,
    trigger    JSONB,
    regime_id  TEXT             -- regime identifier for the day; Phase 1 defines the link to daily_regime
);

COMMENT ON TABLE radar_events IS
    'Detected radar events (Phase 1). event_id threads radar -> setup -> outcome -> journal.';

-- ── event_labels — Phase 1 forward-return labeling; created EMPTY.
CREATE TABLE IF NOT EXISTS event_labels (
    event_id       UUID             NOT NULL REFERENCES radar_events (event_id),
    horizon        TEXT             NOT NULL,   -- e.g. '5m' | '30m' | 'eod'
    forward_return DOUBLE PRECISION,
    mae            DOUBLE PRECISION,            -- maximum adverse excursion
    mfe            DOUBLE PRECISION,            -- maximum favorable excursion
    CONSTRAINT event_labels_event_horizon_uniq UNIQUE (event_id, horizon)
);

COMMENT ON TABLE event_labels IS
    'Forward-return labels per event per horizon (Phase 1). Empty this phase.';

-- ── daily_regime — Phase 1 populates; created EMPTY.
CREATE TABLE IF NOT EXISTS daily_regime (
    regime_date  DATE PRIMARY KEY,
    breadth      DOUBLE PRECISION,
    pct_gap_up   DOUBLE PRECISION,
    median_rvol  DOUBLE PRECISION,
    india_vix    DOUBLE PRECISION,
    regime_class TEXT
);

COMMENT ON TABLE daily_regime IS
    'Per-day market regime descriptors (Phase 1). Empty this phase.';

-- ── TimescaleDB-only steps (skippable on vanilla Postgres) ───────────────────
-- Guarded so this file runs cleanly on plain Postgres: if the extension is absent
-- the tables above stand on their own and these optimizations are skipped.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        -- Convert radar_snapshots to a hypertable partitioned on ts, 1-day chunks.
        -- Table is empty at migration time; migrate_data => TRUE is a no-op here but
        -- keeps a re-run safe. if_not_exists => TRUE makes the call idempotent.
        PERFORM create_hypertable(
            'radar_snapshots', 'ts',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists       => TRUE,
            migrate_data        => TRUE
        );

        -- Compression (decision #4: compress YES, delete NO). Compress chunks older
        -- than 7 days, segment by symbol, order by ts. NO retention policy is added.
        ALTER TABLE radar_snapshots SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol',
            timescaledb.compress_orderby   = 'ts'
        );
        PERFORM add_compression_policy('radar_snapshots', INTERVAL '7 days', if_not_exists => TRUE);

        RAISE NOTICE 'TimescaleDB: radar_snapshots hypertable (1-day chunks) + compression policy (>7d) configured.';
    ELSE
        RAISE NOTICE 'timescaledb extension absent — skipping hypertable/compression (plain Postgres mode).';
    END IF;
END
$$;
