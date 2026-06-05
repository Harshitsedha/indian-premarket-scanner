-- Migration 006: backtest_jobs — async job queue for the backtester UI
--
-- Uses uuid to avoid exposing a sequential numeric id externally.
-- params (jsonb): full kwargs for run_single() or run_multi() — the worker
--   calls the function directly from this dict.
-- summary (jsonb): mode-specific result (expectancy block for run,
--   candidate counts for record). Null until status=done.
-- result_path (text): absolute filesystem path to the output CSV.

CREATE TABLE IF NOT EXISTS backtest_jobs (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    mode        TEXT        NOT NULL CHECK (mode IN ('run', 'record')),
    params      JSONB       NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'done', 'error')),
    result_path TEXT        NULL,
    summary     JSONB       NULL,
    error       TEXT        NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at  TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_jobs_status     ON backtest_jobs(status);
CREATE INDEX IF NOT EXISTS idx_backtest_jobs_created_at ON backtest_jobs(created_at DESC);
