"""
Backtest job worker — polls backtest_jobs, executes one job at a time.

Run:
    python pipeline/backtest/worker.py [--poll-interval 2] [--log-level INFO]

Guarantees:
  - Atomic job claim via FOR UPDATE SKIP LOCKED — safe to run multiple workers,
    but one is enough given typical job durations (seconds to minutes on cache).
  - A crash inside a job sets status=error with full traceback; the loop continues.
  - DB errors in the poll loop are logged and retried after the poll interval.
  - On startup, jobs stuck in 'running' for >10 min are re-queued automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import psycopg2
import psycopg2.extras
from loguru import logger

from utils.config import settings
from utils.logger import setup_logger
from backtest.run import run_single, run_multi


def _db():
    return psycopg2.connect(
        host     = settings.postgres_host,
        port     = settings.postgres_port,
        dbname   = settings.postgres_db,
        user     = settings.postgres_user,
        password = settings.postgres_password,
    )


def _reset_orphaned(conn) -> None:
    """Re-queue any jobs stuck in 'running' from a previous crashed worker."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE backtest_jobs
               SET status = 'queued',
                   started_at = NULL
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '10 minutes'
        """)
        count = cur.rowcount
        conn.commit()
    if count:
        logger.warning(f"Re-queued {count} orphaned running job(s) from previous worker.")


def _claim(conn) -> dict | None:
    """Atomically claim the oldest queued job. Returns the row dict or None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            UPDATE backtest_jobs
               SET status = 'running', started_at = NOW()
             WHERE id = (
                 SELECT id FROM backtest_jobs
                  WHERE status = 'queued'
                  ORDER BY created_at
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
         RETURNING *
        """)
        row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def _set_done(conn, job_id: str, result_path: str, summary: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backtest_jobs
               SET status = 'done', finished_at = NOW(),
                   result_path = %s, summary = %s
             WHERE id = %s
            """,
            (result_path, json.dumps(summary), job_id),
        )
        conn.commit()


def _set_error(conn, job_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backtest_jobs
               SET status = 'error', finished_at = NOW(), error = %s
             WHERE id = %s
            """,
            (error[:8000], job_id),
        )
        conn.commit()


def _execute(job: dict) -> tuple[str, dict]:
    """Dispatch to run_single, run_multi, or run_train_test based on params."""
    params = dict(job["params"])   # psycopg2 returns jsonb as dict already
    mode   = job["mode"]

    if mode == "train_test":
        from backtest.train_test import run_train_test
        return run_train_test(job_id=str(job["id"]), **params)

    # 'features' in params → record run; absent → backtest run
    if mode == "record" and "features" not in params:
        raise ValueError("record mode job missing 'features' in params")

    if "symbol" in params:
        return run_single(**params)
    else:
        return run_multi(**params)


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest job worker")
    p.add_argument("--poll-interval", type=float, default=2.0, dest="poll_interval")
    p.add_argument("--log-level", default="INFO", dest="log_level")
    args = p.parse_args()
    setup_logger(args.log_level)

    logger.info("Backtest worker started. Polling every %.1fs.", args.poll_interval)

    # Reset orphaned jobs before entering the poll loop
    try:
        conn = _db()
        try:
            _reset_orphaned(conn)
        finally:
            conn.close()
    except Exception as exc:
        logger.error(f"Could not reset orphaned jobs on startup: {exc}")

    while True:
        try:
            conn = _db()
            try:
                job = _claim(conn)
                if job:
                    jid = str(job["id"])
                    logger.info(f"[{jid[:8]}] Claimed {job['mode']} job, params={job['params']}")
                    try:
                        result_path, summary = _execute(job)
                        _set_done(conn, jid, result_path, summary)
                        logger.info(f"[{jid[:8]}] Done → {result_path}")
                    except Exception:
                        tb = traceback.format_exc()
                        logger.error(f"[{jid[:8]}] Failed:\n{tb}")
                        _set_error(conn, jid, tb)
            finally:
                conn.close()
        except Exception as exc:
            logger.error(f"Worker DB error: {exc}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()