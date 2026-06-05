-- Migration 007: backtest_rulesets + train_test mode
--
-- 1. Extend backtest_jobs.mode to accept 'train_test'.
-- 2. Create backtest_rulesets — stores the Claude-derived rule set that was
--    committed before the test range was ever fetched.
--    committed_at is the structural lockout timestamp: it must precede any
--    test-range data access.

-- ── 1. Extend mode constraint ─────────────────────────────────────────────────
-- Drop the old CHECK and add a new one. No row rewrite needed (CHECK only).

ALTER TABLE backtest_jobs
    DROP CONSTRAINT IF EXISTS backtest_jobs_mode_check;

ALTER TABLE backtest_jobs
    ADD CONSTRAINT backtest_jobs_mode_check
    CHECK (mode IN ('run', 'record', 'train_test'));

-- ── 2. Rule sets table ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS backtest_rulesets (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         UUID        NOT NULL REFERENCES backtest_jobs(id),
    train_csv      TEXT        NOT NULL,    -- absolute path to the source Record CSV
    rules_raw      TEXT        NOT NULL,    -- Claude's full response, verbatim (audit trail)
    rules_parsed   JSONB       NOT NULL,    -- structured rule set (see below)
    feature_stats  JSONB       NOT NULL,    -- compact summary sent to Claude (no raw rows)
    committed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- rules_parsed schema:
-- {
--   "filters": [
--     { "feature": "gap_pct",      "op": "between", "low": 1.0,  "high": 3.0  },
--     { "feature": "or_range_pct", "op": "lt",      "value": 2.5              },
--     { "feature": "day_of_week",  "op": "not_in",  "values": [4]             }
--   ],
--   "confidence": "low|medium|high",
--   "claude_reasoning": "one paragraph"
-- }
-- Supported ops: between, lt, gt, lte, gte, in, not_in

CREATE INDEX IF NOT EXISTS idx_backtest_rulesets_job_id
    ON backtest_rulesets(job_id);
