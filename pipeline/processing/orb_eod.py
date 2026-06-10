"""
pipeline/processing/orb_eod.py — EOD persistence of ORB range outcomes.

Scheduled 15:35 IST (scheduler.py). For every active range def for today it
reads the materialised OR + break state the poller left in Redis, joins the
day's gap/news context (radar:day_meta), and upserts one row per
(symbol, date, or_start, or_end) into orb_history.

Cleanup contract (prior data-loss incidents — never delete unverified):
  Redis keys are deleted ONLY after the DB row count for today matches the
  number of rows we expected to persist. On any mismatch the deletion is
  skipped and the discrepancy logged; a re-run is idempotent (ON CONFLICT
  DO UPDATE).
"""

import sys
from datetime import date
from pathlib import Path

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
import psycopg2.extras
from loguru import logger

from utils.config import settings
import storage.redis_client as _cache
from realtime.orb_ranges import load_range_defs

_OR_KEY_FMT    = "radar:or:{date}:{label}"
_STATE_KEY_FMT = "radar:or_state:{date}:{label}"

_UPSERT_SQL = """
INSERT INTO orb_history (
    symbol, date, or_start, or_end,
    or_high, or_low, or_range_pct,
    broke_up, broke_down, first_break_side, break_time,
    break_atr_max, rvol_at_break,
    gap_pct, had_news, eod_close
) VALUES %s
ON CONFLICT (symbol, date, or_start, or_end) DO UPDATE SET
    or_high          = EXCLUDED.or_high,
    or_low           = EXCLUDED.or_low,
    or_range_pct     = EXCLUDED.or_range_pct,
    broke_up         = EXCLUDED.broke_up,
    broke_down       = EXCLUDED.broke_down,
    first_break_side = EXCLUDED.first_break_side,
    break_time       = EXCLUDED.break_time,
    break_atr_max    = EXCLUDED.break_atr_max,
    rvol_at_break    = EXCLUDED.rvol_at_break,
    gap_pct          = EXCLUDED.gap_pct,
    had_news         = EXCLUDED.had_news,
    eod_close        = EXCLUDED.eod_close
"""


def _db():
    return psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )


def _build_rows(trade_date: str, defs: list[dict], day_meta: dict) -> list[tuple]:
    """One orb_history tuple per (symbol, range) with a materialised OR."""
    rows: list[tuple] = []
    for d in defs:
        label        = d["label"]
        materialized = _cache.hgetall_json(_OR_KEY_FMT.format(date=trade_date, label=label))
        materialized.pop("_meta", None)
        if not materialized:
            logger.warning(f"orb_eod: range {label} has no materialised OR — skipping")
            continue
        state = _cache.hgetall_json(_STATE_KEY_FMT.format(date=trade_date, label=label))

        for sym, m in materialized.items():
            st   = state.get(sym) or {}
            meta = day_meta.get(sym) or {}
            or_high, or_low = m.get("or_high"), m.get("or_low")

            or_range_pct = None
            if or_high is not None and or_low is not None:
                ref = meta.get("prev_close") or (or_high + or_low) / 2
                if ref and ref > 0:
                    or_range_pct = round((or_high - or_low) / ref * 100, 4)

            rows.append((
                sym, trade_date, str(d["start"]), str(d["end"]),
                or_high, or_low, or_range_pct,
                bool(st.get("broke_up")), bool(st.get("broke_down")),
                st.get("first_break_side"), st.get("break_time"),
                st.get("break_atr_max"), st.get("rvol_at_break"),
                meta.get("gap_pct"), bool(meta.get("has_news")), meta.get("ltp"),
            ))
    return rows


def run_orb_eod(trade_date: str | None = None) -> dict:
    """
    Persist today's ORB outcomes. Returns a summary dict
    {persisted, expected, verified, cleaned} for logging/tests.
    """
    trade_date = trade_date or date.today().isoformat()
    defs = load_range_defs(trade_date)

    day_meta = _cache.get(f"radar:day_meta:{trade_date}") or {}
    rows = _build_rows(trade_date, defs, day_meta)

    if not rows:
        logger.info(
            f"orb_eod: no materialised OR data for {trade_date} "
            "(poller not running today?) — exiting cleanly, nothing persisted"
        )
        return {"persisted": 0, "expected": 0, "verified": False, "cleaned": False}

    expected = len(rows)
    conn = _db()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, _UPSERT_SQL, rows, page_size=500)
        conn.commit()

        # Verify before any deletion: DB must hold every row we just wrote.
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orb_history WHERE date = %s", (trade_date,))
            db_count = cur.fetchone()[0]

        verified = db_count >= expected
        if not verified:
            logger.error(
                f"orb_eod: VERIFICATION FAILED — expected >= {expected} rows for "
                f"{trade_date}, found {db_count}. Skipping Redis cleanup and "
                "session-range deactivation; data stays for a re-run."
            )
            return {"persisted": expected, "expected": expected,
                    "verified": False, "cleaned": False}

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE orb_range_defs SET active = FALSE
                WHERE scope = 'session' AND session_date = %s AND active = TRUE
                """,
                (trade_date,),
            )
            deactivated = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    deleted = 0
    for pattern in (
        f"radar:polls:{trade_date}:*",
        f"radar:or:{trade_date}:*",
        f"radar:or_state:{trade_date}:*",
        f"radar:day_meta:{trade_date}",
    ):
        deleted += _cache.delete_pattern(pattern)

    logger.info(
        f"orb_eod: persisted {expected} rows for {trade_date} "
        f"({len(defs)} ranges), deactivated {deactivated} session range(s), "
        f"deleted {deleted} Redis keys"
    )
    return {"persisted": expected, "expected": expected, "verified": True, "cleaned": True}


if __name__ == "__main__":
    from utils.logger import setup_logger
    setup_logger("INFO")
    run_orb_eod(sys.argv[1] if len(sys.argv) > 1 else None)
