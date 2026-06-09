-- Migration 010: catalyst fields for ranked stocks
-- Adds catalyst_line (trader-facing one-liner) and direction (expected price direction)
-- to the setups table. setup_type values will expand with new Claude output schema.

ALTER TABLE setups
    ADD COLUMN IF NOT EXISTS catalyst_line TEXT,
    ADD COLUMN IF NOT EXISTS direction      VARCHAR(16);

COMMENT ON COLUMN setups.catalyst_line IS 'Trader-focused one-liner ≤120 chars explaining the catalyst';
COMMENT ON COLUMN setups.direction IS 'Expected price direction at open: bullish | bearish | neutral';
