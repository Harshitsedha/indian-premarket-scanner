-- Migration 009: widen backtest_jobs status CHECK to include cancelling/cancelled.
-- Widening a CHECK is always safe against existing rows — no backfill needed.
-- Idempotent: DROP CONSTRAINT IF EXISTS before re-adding.

BEGIN;

ALTER TABLE backtest_jobs
    DROP CONSTRAINT IF EXISTS backtest_jobs_status_check;

ALTER TABLE backtest_jobs
    ADD CONSTRAINT backtest_jobs_status_check
    CHECK (status IN (
        'queued', 'running', 'done', 'error',
        'cancelling', 'cancelled'
    ));

COMMIT;
