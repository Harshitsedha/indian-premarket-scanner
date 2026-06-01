-- Migration 002: upstox token storage
-- Stores OAuth access/refresh tokens. Always read the row with the highest id
-- (most recent). Insert a new row on every refresh rather than updating in place
-- so you retain an audit trail and can roll back to a prior token if needed.

CREATE TABLE IF NOT EXISTS upstox_tokens (
    id            SERIAL PRIMARY KEY,
    access_token  TEXT        NOT NULL,
    refresh_token TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Speeds up the "SELECT ... ORDER BY id DESC LIMIT 1" pattern used on every request
CREATE INDEX IF NOT EXISTS idx_upstox_tokens_id_desc ON upstox_tokens (id DESC);
