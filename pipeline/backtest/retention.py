"""
Backtest result retention — keep the RESULT_RETENTION_COUNT newest result CSVs
per eligible mode and delete the rest from disk.

Safety design:
  - Deletion is driven entirely by the DB (never filesystem globbing or directory
    scanning). Non-qualifying files (candidates_, ca_report_, summary_, etc.) are
    structurally unreachable — they are excluded by the mode filter in the SELECT
    query, and protected again by explicit name-prefix checks.
  - Only modes that produce trade-result CSVs are eligible: 'run' and 'train_test'.
    'record' mode is intentionally excluded — its candidates_ CSVs are training
    inputs for future train_test runs and must never be pruned.
  - Per-mode retention is independent: 5 run results and 5 train_test results are
    kept separately. A cheap single-symbol run cannot evict a train_test result.
  - DB row is NULLed BEFORE the file is deleted.
      Orphaned file (DB nulled, disk delete failed)  → harmless disk waste.
      Orphaned DB pointer (file deleted, DB not nulled) → broken 404 download link.
    We always prefer the harmless failure mode.
  - Path safety: resolve() + ancestry check + suffix check + prefix check.
  - run_retention() never raises; all errors are logged so the caller's job is
    unaffected. The job already succeeded before this runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from loguru import logger

from backtest.recorder import _RESULTS_DIR

# Number of completed result CSVs to retain, counted independently per mode.
RESULT_RETENTION_COUNT = 5

# Modes whose result CSVs are subject to retention.
_PRUNABLE_MODES = frozenset({"run", "train_test"})


def run_retention(conn, completed_mode: str) -> None:
    """
    Prune old result CSVs for `completed_mode` after a job successfully completes.

    Must be called AFTER _set_done has committed so the just-finished job is
    already included in the query result and counted among the jobs to keep.

    `conn` is the worker's existing psycopg2 connection, already in a clean
    (committed) state after _set_done.

    Never raises — any failure is logged and the function returns cleanly.
    """
    if completed_mode not in _PRUNABLE_MODES:
        return
    try:
        _prune_mode(conn, completed_mode)
    except Exception as exc:
        logger.error(f"Retention [{completed_mode}]: unexpected error — {exc}")


def _prune_mode(conn, mode: str) -> None:
    """Inner implementation — called only from run_retention's try/except wrapper."""
    results_dir = _RESULTS_DIR.resolve()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_path, finished_at
              FROM backtest_jobs
             WHERE status = 'done'
               AND result_path IS NOT NULL
               AND finished_at IS NOT NULL
               AND mode = %s
             ORDER BY finished_at DESC, id DESC
            """,
            (mode,),
        )
        rows = cur.fetchall()

    if len(rows) <= RESULT_RETENTION_COUNT:
        logger.debug(
            f"Retention [{mode}]: {len(rows)} result(s) present — "
            f"within limit of {RESULT_RETENTION_COUNT}, nothing to prune."
        )
        return

    to_keep  = rows[:RESULT_RETENTION_COUNT]
    to_prune = rows[RESULT_RETENTION_COUNT:]

    # Full audit trail logged BEFORE any delete or DB mutation
    logger.info(
        f"Retention [{mode}]: keeping {len(to_keep)}, "
        f"pruning {len(to_prune)}: "
        + ", ".join(Path(r[1]).name for r in to_prune)
    )

    for job_id, result_path, finished_at in to_prune:
        job_id_str = str(job_id)

        # ── Path safety checks ─────────────────────────────────────────────
        path = Path(result_path).resolve()

        if results_dir not in path.parents:
            logger.warning(
                f"Retention skip [{job_id_str[:8]}]: {path} is outside "
                f"results dir ({results_dir}) — skipping"
            )
            continue

        if path.suffix != ".csv":
            logger.warning(
                f"Retention skip [{job_id_str[:8]}]: {path.name} is not "
                f"a .csv file — skipping"
            )
            continue

        if path.name.startswith("candidates_") or path.name.startswith("ca_report_"):
            logger.warning(
                f"Retention skip [{job_id_str[:8]}]: {path.name} matches "
                f"protected prefix — skipping"
            )
            continue

        # ── Order of operations: NULL DB first, then delete from disk ──────
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE backtest_jobs SET result_path = NULL WHERE id = %s",
                    (job_id_str,),
                )
            conn.commit()
        except Exception as exc:
            logger.error(
                f"Retention [{job_id_str[:8]}]: DB NULL failed ({exc}) — "
                f"skipping disk delete to preserve DB pointer"
            )
            try:
                conn.rollback()
            except Exception:
                pass
            continue

        # Disk delete is best-effort; DB is already consistent at this point
        try:
            if path.exists():
                path.unlink()
                logger.info(
                    f"Retention [{job_id_str[:8]}]: deleted {path.name} "
                    f"(finished_at={finished_at})"
                )
            else:
                logger.debug(
                    f"Retention [{job_id_str[:8]}]: {path.name} already absent from disk"
                )
        except Exception as exc:
            logger.warning(
                f"Retention [{job_id_str[:8]}]: disk delete failed for "
                f"{path.name} — {exc} (DB already nulled, harmless waste)"
            )
