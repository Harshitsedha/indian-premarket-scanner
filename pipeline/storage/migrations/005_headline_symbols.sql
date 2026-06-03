-- Migration 005: add symbols to headlines
-- Stores NSE trading symbols (from the scanner WATCHLIST) that each headline
-- mentions. JSONB chosen over TEXT[] to match the existing signals JSONB
-- pattern in setups, and because psycopg2 RealDictCursor returns it as a
-- native Python list with no extra casting.
-- DEFAULT '[]' so existing rows get an empty array, not NULL.

ALTER TABLE headlines ADD COLUMN IF NOT EXISTS symbols JSONB NOT NULL DEFAULT '[]';
