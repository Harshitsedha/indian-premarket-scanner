"""
Backtest job worker — polls backtest_jobs, executes one job at a time.

Run:
    python pipeline/backtest/worker.py [--poll-interval 2] [--log-level INFO]

Guarantees:
  - Atomic job claim via FOR UPDATE SKIP LOCKED — safe to run multiple workers,
    but one is enough given typical job durations (seconds to minutes on cache).
  - A crash inside a job sets status=error with full traceback; the loop continues.
  - DB errors in the poll loop are logged and retried after the poll interval.
  - On startup, jobs stuck in 'running' or 'cancelling' for >10 min are marked error
    (not re-queued, to break the systemd Restart=on-failure infinite cycle).
  - Per-job signal.alarm(600s) hard timeout raises _JobTimeout.
  - Per-job background thread polls DB every 2s for 'cancelling'; when detected it
    sets cancel_event which execution functions check at safe checkpoints.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
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
from backtest.exceptions import _JobCancelled
from backtest.run import run_single, run_multi

# Hard limit per job. SIGALRM is UNIX-only; this worker runs on a Linux VPS.
_MAX_JOB_SECONDS = 600   # 10 minutes


class _JobTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _JobTimeout(f"Job exceeded {_MAX_JOB_SECONDS // 60}-minute hard limit")


def _db():
    return psycopg2.connect(
        host     = settings.postgres_host,
        port     = settings.postgres_port,
        dbname   = settings.postgres_db,
        user     = settings.postgres_user,
        password = settings.postgres_password,
    )


def _reset_orphaned(conn) -> None:
    """
    Mark jobs stuck in 'running' or 'cancelling' when the worker restarts as error.

    'running' orphans:    worker crashed mid-execution.
    'cancelling' orphans: worker crashed after the cancel signal was set but before
                          the job acknowledged it and reached a terminal state.

    Re-queuing would cause an infinite cycle with systemd Restart=on-failure.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE backtest_jobs
               SET status      = 'error',
                   finished_at = NOW(),
                   error       = 'Job was still running when the worker restarted '
                                 '(likely timed out or the worker process was killed). '
                                 'Resubmit to retry.'
             WHERE status IN ('running', 'cancelling')
               AND started_at < NOW() - INTERVAL '10 minutes'
        """)
        count = cur.rowcount
        conn.commit()
    if count:
        logger.warning(f"Marked {count} orphaned job(s) as error (not re-queued).")


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


def _set_cancelled(conn, job_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE backtest_jobs
               SET status = 'cancelled', finished_at = NOW()
             WHERE id = %s
            """,
            (job_id,),
        )
        conn.commit()


def _poll_cancel(
    job_id: str,
    cancel_event: threading.Event,
    stop_event: threading.Event,
) -> None:
    """
    Background thread: polls DB every 2s for this specific job's status turning
    'cancelling'. Opens its own short-lived connection per poll — never shares the
    main loop's connection or sits inside a long-lived transaction, so it sees the
    cancel endpoint's committed write on the very next poll cycle.

    Scoped strictly to job_id: a lingering thread (if join times out) cannot
    react to a different job's status because the WHERE clause is job-id-specific.
    """
    while not stop_event.is_set():
        try:
            conn = _db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM backtest_jobs WHERE id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            if row and row[0] == "cancelling":
                cancel_event.set()
                return
        except Exception as exc:
            logger.debug(f"[{job_id[:8]}] poll-cancel DB error (non-fatal): {exc}")
        # stop_event.wait acts like sleep but wakes immediately on stop_event.set()
        stop_event.wait(timeout=2.0)


def _execute(job: dict, cancel_event: threading.Event) -> tuple[str, dict]:
    """Dispatch to run_single, run_multi, or run_train_test based on params."""
    params = dict(job["params"])   # psycopg2 returns jsonb as dict already
    mode   = job["mode"]

    if mode == "train_test":
        from backtest.train_test import run_train_test
        return run_train_test(
            job_id=str(job["id"]),
            cancel_event=cancel_event,
            **params,
        )

    # 'features' in params → record run; absent → backtest run
    if mode == "record" and "features" not in params:
        raise ValueError("record mode job missing 'features' in params")

    if "symbol" in params:
        return run_single(cancel_event=cancel_event, **params)
    else:
        return run_multi(cancel_event=cancel_event, **params)


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

                    # Per-job cancel polling thread — own connection, job-id scoped
                    cancel_event = threading.Event()
                    stop_poll    = threading.Event()
                    poll_thread  = threading.Thread(
                        target=_poll_cancel,
                        args=(jid, cancel_event, stop_poll),
                        daemon=True,
                        name=f"cancel-poll-{jid[:8]}",
                    )
                    poll_thread.start()

                    signal.signal(signal.SIGALRM, _alarm_handler)
                    signal.alarm(_MAX_JOB_SECONDS)
                    try:
                        result_path, summary = _execute(job, cancel_event)
                        signal.alarm(0)
                        _set_done(conn, jid, result_path, summary)
                        logger.info(f"[{jid[:8]}] Done -> {result_path}")
                    except _JobCancelled as exc:
                        # Most specific first — must precede bare Exception
                        signal.alarm(0)
                        logger.info(f"[{jid[:8]}] Cancelled: {exc}")
                        _set_cancelled(conn, jid)
                    except _JobTimeout as exc:
                        signal.alarm(0)
                        msg = str(exc)
                        logger.error(f"[{jid[:8]}] TIMEOUT: {msg}")
                        _set_error(conn, jid, f"TIMEOUT: {msg}")
                    except Exception:
                        signal.alarm(0)
                        tb = traceback.format_exc()
                        logger.error(f"[{jid[:8]}] Failed:\n{tb}")
                        _set_error(conn, jid, tb)
                    finally:
                        # Always tear down the poll thread, even if the job errored.
                        # stop_event.set() wakes the wait(2.0) immediately; join(3)
                        # gives it time to exit cleanly. A daemon thread that outlives
                        # the join cannot affect later jobs — its WHERE clause is
                        # scoped to jid.
                        stop_poll.set()
                        poll_thread.join(timeout=3)
            finally:
                conn.close()
        except Exception as exc:
            logger.error(f"Worker DB error: {exc}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
