"""
pipeline/realtime/persist_worker.py — Edge-page persistence worker (Phase 0 data spine).

Consumes the ``radar:frames`` Redis Stream (one entry per 45s poll cycle, each entry the
full ~199-symbol frame serialized as JSON) and batch-inserts every row into the
TimescaleDB ``radar_snapshots`` hypertable.

Fully isolated from live serving:
  * the radar poller never writes to Postgres — it only XADDs to radar:frames;
  * this worker never touches the live ``radar:snapshot`` key the /radar UI reads.
Stopping Postgres or this worker leaves the live radar page and the poll loop untouched.

Durability model:
  * Redis Stream consumer group (XREADGROUP); explicit XACK only AFTER the DB COMMIT.
    Crash before ack -> the frame is redelivered -> the unique key (ts, symbol) +
    ON CONFLICT DO NOTHING makes the redelivery a no-op. No data loss, no duplicates.
  * Crashed-consumer recovery: XAUTOCLAIM sweeps pending entries left by a dead
    consumer (e.g. the previous PID after a restart) at startup and on every idle tick.
  * DB unavailable -> exponential capped backoff; nothing is acked, so frames simply
    accumulate in the capped stream until Postgres returns, then drain in order.

Stateless per batch: the DB connection and stream cursor live on the stack inside
main(), never in module-level mutable globals — a deploy restart starts clean, so the
stale-module-in-memory incident cannot recur with this service.

Connections (this worker runs OUTSIDE Docker): Postgres + Redis via 127.0.0.1 and the
host-mapped ports, sourced from utils.config.settings (.env). NEVER the compose service
names ``postgres`` / ``redis``.

Run:
    python realtime/persist_worker.py
"""

import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
import psycopg2.extras
import redis as redis_lib
from loguru import logger

from utils.config import settings
from utils.logger import setup_logger


# ── constants ───────────────────────────────────────────────────────────────────
_STREAM   = "radar:frames"
_GROUP    = "persist"
# Per-process consumer name. A restart gets a new name; the previous name's pending
# entries are recovered by XAUTOCLAIM (which reclaims across the whole group PEL).
_CONSUMER = f"persist-{socket.gethostname()}-{os.getpid()}"

_READ_COUNT = 50      # max entries (frames) pulled per XREADGROUP — drains backlog fast
_BLOCK_MS   = 5_000   # block up to 5s waiting for new frames before an idle tick

# Reclaim pending entries idle longer than this (ms). Low because this is a
# single-worker deployment — there is no sibling to steal in-flight work from, and a
# small threshold makes redelivery after a mid-batch crash near-immediate.
_CLAIM_MIN_IDLE_MS = int(os.getenv("PERSIST_CLAIM_MIN_IDLE_MS", "10000"))

_BACKOFF_BASE = 1.0    # seconds — first DB/Redis-down retry delay
_BACKOFF_CAP  = 60.0   # seconds — capped exponential backoff ceiling

# Insert column order — must match the tuple order built in _frame_to_rows().
_COLUMNS = (
    "ts", "symbol", "price", "prev_close", "open_price", "high", "low", "volume",
    "gap_pct", "change_pct", "change_from_open_pct", "rvol", "range_used_pct",
    "atr_mult", "index_membership", "has_news", "catalyst_line", "headline_count",
)

# Idempotent batch insert: redelivered frames collide on (ts, symbol) and are skipped.
_INSERT_SQL = (
    f"INSERT INTO radar_snapshots ({', '.join(_COLUMNS)}) VALUES %s "
    f"ON CONFLICT (ts, symbol) DO NOTHING"
)


# ── connections ──────────────────────────────────────────────────────────────────

def _connect_db():
    """Open a fresh psycopg2 connection. Caller owns it — no module-level DB state."""
    conn = psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
        connect_timeout=5,
    )
    conn.autocommit = False
    return conn


def _connect_redis() -> "redis_lib.Redis":
    return redis_lib.Redis(
        host=settings.redis_host, port=settings.redis_port,
        decode_responses=True, socket_connect_timeout=5,
    )


def _ensure_group(r: "redis_lib.Redis") -> None:
    """
    Create the consumer group at id '0' (+ MKSTREAM) so the very first run drains any
    frames the poller already buffered before the worker existed. On every subsequent
    start the group already exists (BUSYGROUP) and this is a no-op.
    """
    try:
        r.xgroup_create(name=_STREAM, groupname=_GROUP, id="0", mkstream=True)
        logger.info(f"persist_worker: created consumer group {_GROUP!r} on {_STREAM!r}")
    except redis_lib.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug("persist_worker: consumer group already exists")
        else:
            raise


# ── frame -> rows ────────────────────────────────────────────────────────────────

def _frame_to_rows(payload: str) -> list[tuple]:
    """Parse one stream entry's JSON frame into a list of column tuples ([] if unusable)."""
    try:
        frame = json.loads(payload)
    except Exception as exc:
        logger.error(f"persist_worker: malformed frame JSON dropped: {exc}")
        return []

    generated_at = frame.get("generated_at")
    rows = frame.get("rows") or []
    if not generated_at or not rows:
        return []
    try:
        ts = datetime.fromisoformat(generated_at)
    except Exception as exc:
        logger.error(f"persist_worker: unparseable generated_at {generated_at!r}: {exc}")
        return []

    out: list[tuple] = []
    for row in rows:
        sym = row.get("symbol")
        if not sym:
            continue
        vol = row.get("volume")
        out.append((
            ts,
            sym,
            row.get("ltp"),
            row.get("prev_close"),
            row.get("open"),
            row.get("high"),
            row.get("low"),
            int(vol) if vol is not None else None,
            row.get("gap_pct"),
            row.get("change_pct"),
            row.get("change_from_open_pct"),
            row.get("rvol"),
            row.get("range_used_pct"),
            row.get("atr_multiple"),
            row.get("index_membership"),
            row.get("has_news"),
            row.get("catalyst_line"),
            row.get("headline_count"),
        ))
    return out


def _persist_entry(conn, r: "redis_lib.Redis", entry_id: str, fields: dict) -> int:
    """
    Insert one frame's ~199 rows in a SINGLE batch, COMMIT, then XACK.

    Returns the number of rows in the frame. Raises on DB error WITHOUT acking, so the
    entry stays pending and is retried/redelivered. Empty or malformed frames are acked
    (nothing to insert) so they do not redeliver forever.
    """
    rows = _frame_to_rows(fields.get("frame", ""))
    if not rows:
        r.xack(_STREAM, _GROUP, entry_id)
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, _INSERT_SQL, rows, page_size=len(rows))
    conn.commit()                       # durability point
    r.xack(_STREAM, _GROUP, entry_id)   # ack ONLY after the commit
    return len(rows)


# ── health / recovery ────────────────────────────────────────────────────────────

def _stream_lag(r: "redis_lib.Redis") -> int | None:
    """Entries pending+undelivered for the group — the worker's health metric."""
    try:
        for g in r.xinfo_groups(_STREAM):
            if g.get("name") == _GROUP:
                lag = g.get("lag")
                return int(lag) if lag is not None else None
    except Exception:
        return None
    return None


def _claim_orphans(conn, r: "redis_lib.Redis") -> int:
    """
    Reclaim and persist pending entries left by a crashed consumer (XAUTOCLAIM across
    the whole group PEL). Returns rows persisted. Runs at startup and on idle ticks so
    a worker that died mid-batch has its in-flight frame redelivered and finished.
    """
    persisted = 0
    cursor = "0-0"
    while True:
        res = r.xautoclaim(
            name=_STREAM, groupname=_GROUP, consumername=_CONSUMER,
            min_idle_time=_CLAIM_MIN_IDLE_MS, start_id=cursor, count=_READ_COUNT,
        )
        # redis-py returns (next_cursor, claimed[, deleted]) — index defensively.
        cursor = res[0]
        claimed = res[1]
        if not claimed:
            break
        for entry_id, entry_fields in claimed:
            persisted += _persist_entry(conn, r, entry_id, entry_fields)
        if cursor == "0-0":
            break
    if persisted:
        logger.info(f"persist_worker: reclaimed + persisted {persisted} row(s) from orphaned frames")
    return persisted


# ── main loop ────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logger(settings.log_level)
    logger.info(f"persist_worker: starting up as consumer {_CONSUMER!r}")

    r = _connect_redis()
    try:
        r.ping()
    except Exception as exc:
        logger.error(
            f"persist_worker: Redis unavailable at "
            f"{settings.redis_host}:{settings.redis_port} ({exc}); exiting"
        )
        sys.exit(1)
    _ensure_group(r)

    conn = None
    backoff = _BACKOFF_BASE
    # After a restart, first re-read our own pending (id '0') before switching to new
    # entries ('>'). Combined with _claim_orphans this guarantees no delivered-but-
    # unacked frame is left behind.
    check_backlog = True
    last_id = "0-0"

    while True:
        # (Re)establish the DB connection with capped exponential backoff. While the
        # DB is down we read nothing and ack nothing — frames pile up in the stream.
        if conn is None or conn.closed:
            try:
                conn = _connect_db()
                logger.info(
                    f"persist_worker: connected to Postgres "
                    f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
                )
                backoff = _BACKOFF_BASE
            except Exception as exc:
                logger.error(
                    f"persist_worker: Postgres unavailable ({exc}); backing off "
                    f"{backoff:.0f}s — frames accumulate in {_STREAM}"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)
                continue

        try:
            # Sweep any orphaned pending (crashed prior consumer) before normal reads.
            _claim_orphans(conn, r)

            stream_id = last_id if check_backlog else ">"
            resp = r.xreadgroup(
                groupname=_GROUP, consumername=_CONSUMER,
                streams={_STREAM: stream_id}, count=_READ_COUNT, block=_BLOCK_MS,
            )

            if not resp or not resp[0][1]:
                # No entries: our own pending backlog is drained — switch to live ('>').
                if check_backlog:
                    check_backlog = False
                continue

            entries = resp[0][1]
            frames = 0
            rows_total = 0
            for entry_id, entry_fields in entries:
                rows_total += _persist_entry(conn, r, entry_id, entry_fields)
                frames += 1
                if check_backlog:
                    last_id = entry_id

            lag = _stream_lag(r)
            logger.info(
                f"persist_worker: persisted {frames} frame(s), {rows_total} row(s)"
                + (f", stream_lag={lag}" if lag is not None else "")
            )
            backoff = _BACKOFF_BASE

        except (psycopg2.OperationalError, psycopg2.InterfaceError, psycopg2.DatabaseError) as exc:
            logger.error(
                f"persist_worker: DB error mid-batch ({exc}); rolling back + reconnecting, "
                f"backoff {backoff:.0f}s. Unacked frame(s) will be redelivered."
            )
            try:
                if conn and not conn.closed:
                    conn.rollback()
                    conn.close()
            except Exception:
                pass
            conn = None
            # Re-read our own pending next iteration so the failed frame retries promptly.
            check_backlog = True
            last_id = "0-0"
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)

        except redis_lib.RedisError as exc:
            logger.error(f"persist_worker: Redis error ({exc}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)


if __name__ == "__main__":
    main()
