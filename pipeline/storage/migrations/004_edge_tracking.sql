-- Migration 004: edge tracking — setups and outcomes tables

CREATE TABLE IF NOT EXISTS setups (
    id              SERIAL PRIMARY KEY,
    trading_date    DATE NOT NULL,
    symbol          TEXT NOT NULL,
    setup_type      TEXT NOT NULL,          -- news_catalyst | gap_play | fii_driven | watchlist
    hypothesis      TEXT,                   -- expected direction: bullish | bearish | neutral
    bias_direction  TEXT,                   -- overall market bias at time of logging
    bias_confidence INTEGER,               -- 1-5
    gap_pct         NUMERIC(6,2),          -- pre-market gap % from Upstox
    gap_source      TEXT,                  -- live | proxy
    score           NUMERIC(6,4),          -- ranker composite score
    signals         JSONB,                 -- full signals dict from ranker
    thesis          TEXT,                  -- ranker thesis string
    key_level       NUMERIC(10,2),         -- optional: support/resistance to watch
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS outcomes (
    id                  SERIAL PRIMARY KEY,
    setup_id            INTEGER NOT NULL REFERENCES setups(id),
    trading_date        DATE NOT NULL,
    symbol              TEXT NOT NULL,
    open_price          NUMERIC(10,2),
    actual_gap_pct      NUMERIC(6,2),          -- open vs prev_close (real, not pre-market estimate)
    day_high            NUMERIC(10,2),
    day_low             NUMERIC(10,2),
    close_price         NUMERIC(10,2),
    move_pct            NUMERIC(6,2),          -- (close - open) / open * 100
    result              TEXT,                  -- win | loss | scratch | skip
    hypothesis_correct  BOOLEAN,               -- did price move in expected direction?
    notes               TEXT,                  -- optional manual note
    fetched_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_setups_date      ON setups(trading_date DESC);
CREATE INDEX IF NOT EXISTS idx_setups_symbol    ON setups(symbol);
CREATE INDEX IF NOT EXISTS idx_setups_type      ON setups(setup_type);
CREATE INDEX IF NOT EXISTS idx_outcomes_setup_id ON outcomes(setup_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_date    ON outcomes(trading_date DESC);
