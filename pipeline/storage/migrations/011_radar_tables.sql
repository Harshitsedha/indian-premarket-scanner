-- Migration 011: radar universe and daily baselines for the intraday screener

CREATE TABLE IF NOT EXISTS radar_universe (
    symbol           VARCHAR(32) PRIMARY KEY,
    instrument_key   VARCHAR(64),
    index_membership VARCHAR(32) NOT NULL,   -- 'NIFTY100' | 'MIDCAP150' | 'BOTH' | 'OVERRIDE'
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS radar_baselines (
    symbol             VARCHAR(32)  NOT NULL,
    trade_date         DATE         NOT NULL,
    prev_close         NUMERIC(12,4),
    avg_volume_20d     BIGINT,
    avg_range_pct_20d  NUMERIC(8,6),
    prev_high          NUMERIC(12,4),
    prev_low           NUMERIC(12,4),
    PRIMARY KEY (symbol, trade_date)
);

COMMENT ON TABLE radar_universe   IS 'NIFTY100 + MIDCAP150 constituent symbols with Upstox instrument keys';
COMMENT ON TABLE radar_baselines  IS 'Nightly per-symbol baselines used by the intraday radar poller';
COMMENT ON COLUMN radar_baselines.avg_range_pct_20d IS 'Mean (high-low)/close over last 20 sessions — ATR% proxy';
