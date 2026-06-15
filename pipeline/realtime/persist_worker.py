"""
pipeline/realtime/persist_worker.py — Edge-page persistence worker (Phase 0 data spine).

Consumes the ``radar:frames`` Redis Stream (one entry per 45s poll cycle, each entry the
full ~199-symbol frame serialized as JSON) and batch-inserts every row into the
TimescaleDB ``radar_snapshots`` hypertable.

Phase 1 adds a SECOND stream, ``radar:events`` (one entry per detected event), drained by
the same process and consumer group into ``radar_events``. Events ride a deliberately
lighter path: orphan recovery (XAUTOCLAIM) still guarantees no loss, but events do not
participate in the ordered backlog replay that frames use — order is irrelevant for events
and each is a self-contained single-row insert, so the proven frames path is untouched.

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
# Phase 1 events ride the SAME process and consumer group on a second stream. They are
# drained on a deliberately LIGHTER path than frames: orphan recovery (XAUTOCLAIM) still
# applies so nothing is lost, but events do NOT participate in the ordered backlog
# (check_backlog / last_id) replay — order is irrelevant for events and each entry is a
# single self-contained row, so the proven frames replay logic is left entirely untouched.
_EVENTS_STREAM = "radar:events"
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

# Idempotent single-event insert. Bare ON CONFLICT DO NOTHING (no target) deliberately
# absorbs BOTH unique constraints on radar_events: the event_id PK (a stream redelivery
# of the same event is a no-op) AND the (symbol, event_type, IST-day) unique index from
# migration 014 (a second event with a DIFFERENT event_id — e.g. after a mid-session
# Redis dedup-key flush — is also silently dropped). Naming one arbiter would let the
# other raise and wedge redelivery, so we name neither.
_EVENT_INSERT_SQL = (
    "INSERT INTO radar_events (event_id, ts, symbol, event_type, trigger, regime_id) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT DO NOTHING"
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
        decode_responses=True,
        socket_connect_timeout=5,
        # redis-py 8.0 defaults socket_timeout to 5s. A blocking XREADGROUP holds the
        # socket for the full BLOCK interval, so socket_timeout MUST exceed
        # _BLOCK_MS/1000 or every empty read raises TimeoutError. Keep margin above it.
        socket_timeout=_BLOCK_MS / 1000 + 5,   # 10s > 5s block — never trips on an empty read
    )


def _ensure_group(r: "redis_lib.Redis", stream: str) -> None:
    """
    Create the consumer group at id '0' (+ MKSTREAM) so the very first run drains any
    entries the poller already buffered before the worker existed. On every subsequent
    start the group already exists (BUSYGROUP) and this is a no-op. Called once per
    stream (radar:frames and radar:events share the one group name).
    """
    try:
        r.xgroup_create(name=stream, groupname=_GROUP, id="0", mkstream=True)
        logger.info(f"persist_worker: created consumer group {_GROUP!r} on {stream!r}")
    except redis_lib.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug(f"persist_worker: consumer group already exists on {stream!r}")
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


def _event_to_row(payload: str) -> tuple | None:
    """Parse one radar:events entry's JSON into a radar_events column tuple (None if unusable)."""
    try:
        event = json.loads(payload)
    except Exception as exc:
        logger.error(f"persist_worker: malformed event JSON dropped: {exc}")
        return None

    event_id = event.get("event_id")
    ts_raw   = event.get("ts")
    symbol   = event.get("symbol")
    if not event_id or not ts_raw or not symbol:
        logger.error(f"persist_worker: event missing event_id/ts/symbol, dropped: {event!r}")
        return None
    try:
        ts = datetime.fromisoformat(ts_raw)
    except Exception as exc:
        logger.error(f"persist_worker: unparseable event ts {ts_raw!r}: {exc}")
        return None

    return (
        event_id,
        ts,
        symbol,
        event.get("event_type"),
        psycopg2.extras.Json(event.get("trigger") or {}),
        event.get("regime_id"),
    )


def _persist_event_entry(conn, r: "redis_lib.Redis", entry_id: str, fields: dict) -> int:
    """
    Insert ONE radar_events row, COMMIT, then XACK on the events stream. Returns 1 if the
    entry was handled (inserted or a no-op conflict), 0 if malformed. Raises on DB error
    WITHOUT acking so the entry stays pending and is retried/redelivered. Malformed events
    are acked (nothing to insert) so they do not redeliver forever.
    """
    row = _event_to_row(fields.get("event", ""))
    if row is None:
        r.xack(_EVENTS_STREAM, _GROUP, entry_id)
        return 0
    with conn.cursor() as cur:
        cur.execute(_EVENT_INSERT_SQL, row)
    conn.commit()                              # durability point
    r.xack(_EVENTS_STREAM, _GROUP, entry_id)   # ack ONLY after the commit
    return 1


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


def _claim_orphans(conn, r: "redis_lib.Redis", stream: str, persist_fn) -> int:
    """
    Reclaim and persist pending entries left by a crashed consumer (XAUTOCLAIM across
    the whole group PEL) for one stream. Returns the count persisted. Runs at startup and
    on idle ticks so a worker that died mid-batch has its in-flight entry redelivered and
    finished. Generic over the stream + per-entry persist function so frames and events
    share the identical recovery mechanism (frames pass _persist_entry, events
    _persist_event_entry); the frames behaviour is unchanged from Phase 0.
    """
    persisted = 0
    cursor = "0-0"
    while True:
        res = r.xautoclaim(
            name=stream, groupname=_GROUP, consumername=_CONSUMER,
            min_idle_time=_CLAIM_MIN_IDLE_MS, start_id=cursor, count=_READ_COUNT,
        )
        # redis-py returns (next_cursor, claimed[, deleted]) — index defensively.
        cursor = res[0]
        claimed = res[1]
        if not claimed:
            break
        for entry_id, entry_fields in claimed:
            persisted += persist_fn(conn, r, entry_id, entry_fields)
        if cursor == "0-0":
            break
    if persisted:
        logger.info(
            f"persist_worker: reclaimed + persisted {persisted} item(s) from orphaned "
            f"entries on {stream!r}"
        )
    return persisted


def _cleanup_stale_consumers(r: "redis_lib.Redis", stream: str) -> None:
    """
    Delete dead per-PID consumers left in the group by previous restarts. Each restart
    gets a new consumer name (hostname-pid), so without this the group accumulates one
    idle consumer per restart. Only consumers other than us with ZERO pending are
    removed — any still-pending entries belong to a crashed consumer and must be
    reclaimed by _claim_orphans first (call this AFTER a claim sweep), never dropped.
    """
    try:
        removed = 0
        for c in r.xinfo_consumers(stream, _GROUP):
            name = c.get("name")
            if name != _CONSUMER and int(c.get("pending", 0)) == 0:
                r.xgroup_delconsumer(stream, _GROUP, name)
                removed += 1
        if removed:
            logger.info(
                f"persist_worker: cleaned up {removed} stale consumer(s) from "
                f"group {_GROUP!r} on {stream!r}"
            )
    except redis_lib.RedisError as exc:
        logger.warning(f"persist_worker: stale-consumer cleanup skipped on {stream!r} ({exc})")


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
    _ensure_group(r, _STREAM)
    _ensure_group(r, _EVENTS_STREAM)

    conn = None
    backoff = _BACKOFF_BASE
    # After a restart, first re-read our own pending (id '0') before switching to new
    # entries ('>'). Combined with _claim_orphans this guarantees no delivered-but-
    # unacked frame is left behind. (FRAMES ONLY — events do not use this ordered replay;
    # their orphan recovery is the XAUTOCLAIM sweep below, and live reads use '>'.)
    check_backlog = True
    last_id = "0-0"
    cleaned_consumers = False   # one-time stale-consumer sweep, after the first claim

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
            # Sweep any orphaned pending (crashed prior consumer) before normal reads —
            # both streams, each with its own persist function.
            _claim_orphans(conn, r, _STREAM, _persist_entry)
            _claim_orphans(conn, r, _EVENTS_STREAM, _persist_event_entry)

            # One-time: now that orphaned pending has been reclaimed to us, drop the
            # dead per-PID consumers prior restarts left behind (all now at 0 pending).
            if not cleaned_consumers:
                _cleanup_stale_consumers(r, _STREAM)
                _cleanup_stale_consumers(r, _EVENTS_STREAM)
                cleaned_consumers = True

            # Read BOTH streams in one call (one group name spans both, separate PELs).
            # Frames carry the backlog cursor (ordered replay of our own pending after a
            # restart); events always read live ('>') — their recovery is the orphan
            # sweep above, so the proven frames replay path is unchanged.
            stream_id = last_id if check_backlog else ">"
            resp = r.xreadgroup(
                groupname=_GROUP, consumername=_CONSUMER,
                streams={_STREAM: stream_id, _EVENTS_STREAM: ">"},
                count=_READ_COUNT, block=_BLOCK_MS,
            )

            # Dispatch by stream name — never assume positional order in the response.
            frame_entries: list = []
            event_entries: list = []
            for stream_name, entries in (resp or []):
                if stream_name == _STREAM:
                    frame_entries = entries
                elif stream_name == _EVENTS_STREAM:
                    event_entries = entries

            # Events first (cheap, single-row inserts), independent of the frames cursor.
            events_done = 0
            for entry_id, entry_fields in event_entries:
                events_done += _persist_event_entry(conn, r, entry_id, entry_fields)
            if events_done:
                logger.info(f"persist_worker: persisted {events_done} event(s)")

            if not frame_entries:
                # No frames: our own pending backlog is drained — switch to live ('>').
                # (Keyed on the FRAMES stream specifically; events never gate this.)
                if check_backlog:
                    check_backlog = False
                continue

            frames = 0
            rows_total = 0
            for entry_id, entry_fields in frame_entries:
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

        except redis_lib.TimeoutError:
            # A blocking read returned no data within the socket window — a normal idle
            # tick, NOT a failure. Deliberately leave check_backlog and last_id untouched:
            # a timeout is not proof the backlog is drained (only a real empty *response*
            # is, handled above), and last_id must be preserved so a mid-backlog timeout
            # resumes the pending scan from where it left off rather than re-scanning from
            # 0 or prematurely switching to '>'. The empty-response branch is the SOLE
            # place that transitions backlog (0) -> live ('>').
            continue

        except redis_lib.ConnectionError as exc:
            logger.error(f"persist_worker: Redis connection lost ({exc}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)

        except redis_lib.RedisError as exc:
            logger.error(f"persist_worker: Redis error ({exc}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)


if __name__ == "__main__":
    main()
