-- Migration 012: user-defined ORB range definitions + EOD ORB history

CREATE TABLE IF NOT EXISTS orb_range_defs (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,            -- e.g. "Mid-morning 10:00-10:45"
    or_start    TIME NOT NULL,
    or_end      TIME NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('session','standard')),
    session_date DATE,                    -- required when scope='session', null for standard
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    active      BOOLEAN DEFAULT TRUE,
    UNIQUE (or_start, or_end, scope, session_date)
);

-- Postgres treats NULLs as distinct in UNIQUE constraints, so standard-scope
-- duplicates (session_date IS NULL) need their own partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS orb_range_defs_standard_uniq
    ON orb_range_defs (or_start, or_end, scope)
    WHERE session_date IS NULL;

-- Seed the default OR (the previously hard-coded 09:15-09:30 window)
INSERT INTO orb_range_defs (name, or_start, or_end, scope)
SELECT 'Default OR 15m', '09:15'::time, '09:30'::time, 'standard'
WHERE NOT EXISTS (
    SELECT 1 FROM orb_range_defs
    WHERE or_start = '09:15'::time AND or_end = '09:30'::time
      AND scope = 'standard' AND session_date IS NULL
);

CREATE TABLE IF NOT EXISTS orb_history (
    symbol TEXT NOT NULL, date DATE NOT NULL,
    or_start TIME NOT NULL, or_end TIME NOT NULL,
    or_high NUMERIC, or_low NUMERIC, or_range_pct NUMERIC,
    broke_up BOOLEAN, broke_down BOOLEAN,
    first_break_side TEXT, break_time TIMESTAMPTZ,
    break_atr_max NUMERIC, rvol_at_break NUMERIC,
    gap_pct NUMERIC, had_news BOOLEAN, eod_close NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, date, or_start, or_end)
);

COMMENT ON TABLE orb_range_defs IS 'User-defined opening-range windows tracked by the radar poller';
COMMENT ON TABLE orb_history    IS 'EOD persistence of per-symbol ORB outcomes for every tracked range';
