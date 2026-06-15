"""
pipeline/processing/event_labeler.py — Phase 1 forward-return labeler.

Separate scheduled batch job (scheduler.py). Pure reader of radar_snapshots.price →
writer of event_labels. Never touches live serving or the persist worker, so it carries
none of the Phase 0 isolation risk.

For each radar_events row with an incomplete label set it computes, per horizon:
  * 5m / 15m / 30m — forward return at event_ts + N minutes, using the NEAREST
    radar_snapshots frame within ±tolerance of the ideal time (not the first in-window).
  * eod            — forward return to the last frame of the event's trade day.

forward_return is signed by trigger["direction"], resolved + frozen at emission
(radar_events.py); the labeler NEVER recomputes direction. "up" → ×+1, "down" → ×−1,
None (range_expansion) → ×+1 i.e. a raw, unsigned signed-move. mae/mfe are the most
adverse / most favorable excursions over the window, in the same signed convention.

Hard rules baked in (review refinements):
  * Tolerance ±50s (frames land every ~45s, so a real match is ≤~23s off). NEAREST within
    the window. No frame within tolerance → write the label with NULL metrics (a polling
    gap / halt) — never stretch-match to a far frame.
  * EOD floor: the eod horizon uses the day's last frame ONLY if its ts ≥ 15:15 IST. If a
    symbol's last frame is earlier (illiquid / halted mid-day), write eod NULL rather than
    passing a mid-day stop off as the close.
  * Each label records matched_ts + offset_seconds so loose matches can be filtered later.
  * Idempotent: ON CONFLICT (event_id, horizon) DO UPDATE.

DAY-BOUNDARY EXPRESSION — load-bearing, must stay byte-identical to migration 014's
unique index. We bucket an event into a trade day with the IMMUTABLE interval form
``(ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date`` (NOT the named-zone form, which
is only STABLE and unindexable). In Python the equivalent is ``ts.astimezone(IST).date()``
where IST is the fixed +05:30 offset — identical because IST has no DST. If these two ever
diverge, a midnight-UTC event could be deduped under one day but labeled under another,
opening a silent hole. See migration 014 and the assertion in test_event_labeler.py.

Public API:
    run_event_labeler(trade_date: str | None = None) -> dict
"""

import os
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
from loguru import logger

from utils.config import settings


# ── constants ───────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# Minute horizons; "eod" is handled separately.
_MINUTE_HORIZONS = {"5m": 5, "15m": 15, "30m": 30}
_ALL_HORIZONS    = (*_MINUTE_HORIZONS.keys(), "eod")

# ±tolerance for nearest-frame matching. 50s default: tight enough that a genuine match
# is the adjacent ~45s frame, loose enough to absorb one jittered cycle.
_TOLERANCE_SECS = int(os.getenv("EVENT_LABEL_TOLERANCE_SECS", "50"))

# Session close + EOD floor (IST). The eod label is only trustworthy if the day's last
# frame is at/after the floor — earlier means the symbol stopped printing mid-session.
_SESSION_CLOSE = dtime(15, 30)
_EOD_FLOOR     = dtime(15, 15)

# How far back to scan for events with incomplete labels when no explicit trade_date is
# given. 2 days comfortably covers maturity of every horizon (eod matures same day).
_LOOKBACK_DAYS = int(os.getenv("EVENT_LABEL_LOOKBACK_DAYS", "2"))

# Byte-identical to migration 014's unique-index day expression. Defined ONCE here and
# interpolated into SQL so the two can never drift independently.
_IST_DAY_EXPR = "(ts AT TIME ZONE INTERVAL '5 hours 30 minutes')::date"

_UPSERT_SQL = """
INSERT INTO event_labels
    (event_id, horizon, forward_return, mae, mfe, matched_ts, offset_seconds)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (event_id, horizon) DO UPDATE SET
    forward_return = EXCLUDED.forward_return,
    mae            = EXCLUDED.mae,
    mfe            = EXCLUDED.mfe,
    matched_ts     = EXCLUDED.matched_ts,
    offset_seconds = EXCLUDED.offset_seconds
"""


def _db():
    return psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )


# ── direction / day helpers ───────────────────────────────────────────────────────

def _direction_mult(direction: str | None) -> int:
    """Frozen convention: up→+1, down→−1, None→+1 (raw unsigned move). Never guesses."""
    if direction == "down":
        return -1
    return 1   # "up" and None both multiply by +1


def _trade_day(event_ts: datetime) -> date:
    """IST trade day — byte-equivalent to the migration's _IST_DAY_EXPR (fixed +05:30)."""
    return event_ts.astimezone(IST).date()


# ── price lookups ──────────────────────────────────────────────────────────────────

def _nearest_frame(cur, symbol: str, target_ts: datetime) -> tuple[datetime, float] | None:
    """
    Nearest radar_snapshots frame to target_ts within ±_TOLERANCE_SECS, by absolute
    distance (NOT first-in-window). Returns (matched_ts, price) or None on a polling gap.
    """
    lo = target_ts - timedelta(seconds=_TOLERANCE_SECS)
    hi = target_ts + timedelta(seconds=_TOLERANCE_SECS)
    cur.execute(
        """
        SELECT ts, price FROM radar_snapshots
        WHERE symbol = %s AND price IS NOT NULL AND ts BETWEEN %s AND %s
        ORDER BY abs(extract(epoch FROM (ts - %s)))
        LIMIT 1
        """,
        (symbol, lo, hi, target_ts),
    )
    row = cur.fetchone()
    return (row[0], float(row[1])) if row else None


def _last_frame_of_day(cur, symbol: str, trade_day: date) -> tuple[datetime, float] | None:
    """Last priced frame for the symbol on its IST trade day. Returns (ts, price) or None."""
    cur.execute(
        f"""
        SELECT ts, price FROM radar_snapshots
        WHERE symbol = %s AND price IS NOT NULL AND {_IST_DAY_EXPR} = %s
        ORDER BY ts DESC
        LIMIT 1
        """,
        (symbol, trade_day),
    )
    row = cur.fetchone()
    return (row[0], float(row[1])) if row else None


def _excursions(cur, symbol: str, start_ts: datetime, end_ts: datetime,
                entry: float, mult: int) -> tuple[float | None, float | None]:
    """
    MAE/MFE over (start_ts, end_ts] in the signed convention: for each priced frame the
    signed return is (price-entry)/entry*100*mult; mfe = max, mae = min. Returns (mae, mfe),
    or (None, None) if no frames fall in the window.
    """
    cur.execute(
        """
        SELECT price FROM radar_snapshots
        WHERE symbol = %s AND price IS NOT NULL AND ts > %s AND ts <= %s
        """,
        (symbol, start_ts, end_ts),
    )
    rets = [(float(p) - entry) / entry * 100.0 * mult for (p,) in cur.fetchall()]
    if not rets:
        return None, None
    return round(min(rets), 4), round(max(rets), 4)


# ── per-horizon label computation ───────────────────────────────────────────────────

def _signed_return(entry: float, exit_price: float, mult: int) -> float:
    return round((exit_price - entry) / entry * 100.0 * mult, 4)


def _label_minute_horizon(cur, event, entry, mult, target_ts):
    """Build the (forward_return, mae, mfe, matched_ts, offset_seconds) tuple for a T+N horizon."""
    match = _nearest_frame(cur, event["symbol"], target_ts)
    if match is None:
        return (None, None, None, None, None)   # polling gap — recorded, not stretch-matched
    matched_ts, exit_price = match
    fr        = _signed_return(entry, exit_price, mult)
    offset    = (matched_ts - target_ts).total_seconds()
    mae, mfe  = _excursions(cur, event["symbol"], event["ts"], matched_ts, entry, mult)
    return (fr, mae, mfe, matched_ts, offset)


def _label_eod(cur, event, entry, mult):
    """Build the eod label tuple, honoring the 15:15 IST floor."""
    last = _last_frame_of_day(cur, event["symbol"], _trade_day(event["ts"]))
    if last is None:
        return (None, None, None, None, None)
    last_ts, exit_price = last
    if last_ts.astimezone(IST).time() < _EOD_FLOOR:
        # Last print is before the floor — a mid-day stop, not a real close. Record NULL.
        return (None, None, None, None, None)
    fr       = _signed_return(entry, exit_price, mult)
    mae, mfe = _excursions(cur, event["symbol"], event["ts"], last_ts, entry, mult)
    return (fr, mae, mfe, last_ts, None)   # offset is meaningless for eod


# ── maturity gating ───────────────────────────────────────────────────────────────

def _minute_matured(target_ts: datetime, now_utc: datetime) -> bool:
    """A T+N horizon is ready once now is past the far edge of its tolerance window."""
    return now_utc >= target_ts + timedelta(seconds=_TOLERANCE_SECS)


def _eod_matured(trade_day: date, now_utc: datetime) -> bool:
    """The eod horizon is ready once the trade day's 15:30 IST close has passed."""
    close_utc = datetime.combine(trade_day, _SESSION_CLOSE, tzinfo=IST).astimezone(timezone.utc)
    return now_utc >= close_utc


# ── candidate selection ─────────────────────────────────────────────────────────────

def _fetch_candidates(cur, trade_date: str | None) -> list[dict]:
    """
    Events with an incomplete label set (< all horizons present). When trade_date is given,
    scope to that IST day; otherwise scan the last _LOOKBACK_DAYS. trigger is JSONB.
    """
    n_horizons = len(_ALL_HORIZONS)
    if trade_date is not None:
        cur.execute(
            f"""
            SELECT e.event_id, e.ts, e.symbol, e.trigger
            FROM radar_events e
            WHERE {_IST_DAY_EXPR.replace('ts', 'e.ts')} = %s
              AND (SELECT count(*) FROM event_labels l WHERE l.event_id = e.event_id) < %s
            """,
            (trade_date, n_horizons),
        )
    else:
        cur.execute(
            f"""
            SELECT e.event_id, e.ts, e.symbol, e.trigger
            FROM radar_events e
            WHERE e.ts >= now() - %s::interval
              AND (SELECT count(*) FROM event_labels l WHERE l.event_id = e.event_id) < %s
            """,
            (f"{_LOOKBACK_DAYS} days", n_horizons),
        )
    return [
        {"event_id": r[0], "ts": r[1], "symbol": r[2], "trigger": r[3] or {}}
        for r in cur.fetchall()
    ]


def _existing_horizons(cur, event_ids: list[str]) -> dict[str, set[str]]:
    if not event_ids:
        return {}
    # Cast the text[] param to uuid[] — event_id is a uuid column and there is no
    # implicit text=uuid operator for the ANY() comparison.
    cur.execute(
        "SELECT event_id, horizon FROM event_labels WHERE event_id = ANY(%s::uuid[])",
        ([str(e) for e in event_ids],),
    )
    out: dict[str, set[str]] = {}
    for ev_id, horizon in cur.fetchall():
        out.setdefault(str(ev_id), set()).add(horizon)
    return out


# ── main entry ──────────────────────────────────────────────────────────────────────

def run_event_labeler(trade_date: str | None = None) -> dict:
    """
    Label all events with matured-but-missing horizons. Idempotent and safe to run on any
    cadence. Returns {scanned, written, nulls} for logging/tests.
    """
    now_utc = datetime.now(timezone.utc)
    conn = _db()
    written = 0
    nulls   = 0
    try:
        with conn.cursor() as cur:
            candidates = _fetch_candidates(cur, trade_date)
            existing   = _existing_horizons(cur, [c["event_id"] for c in candidates])

            for event in candidates:
                ev_id      = str(event["event_id"])
                have       = existing.get(ev_id, set())
                entry      = event["trigger"].get("price")
                direction  = event["trigger"].get("direction")
                mult       = _direction_mult(direction)
                trade_day  = _trade_day(event["ts"])

                for horizon in _ALL_HORIZONS:
                    if horizon in have:
                        continue

                    if horizon == "eod":
                        if not _eod_matured(trade_day, now_utc):
                            continue
                        label = (
                            _label_eod(cur, event, entry, mult)
                            if entry else (None, None, None, None, None)
                        )
                    else:
                        target_ts = event["ts"] + timedelta(minutes=_MINUTE_HORIZONS[horizon])
                        if not _minute_matured(target_ts, now_utc):
                            continue
                        label = (
                            _label_minute_horizon(cur, event, entry, mult, target_ts)
                            if entry else (None, None, None, None, None)
                        )

                    cur.execute(_UPSERT_SQL, (ev_id, horizon, *label))
                    written += 1
                    if label[0] is None:
                        nulls += 1

        conn.commit()
    finally:
        conn.close()

    logger.info(
        f"event_labeler: scanned {len(candidates)} event(s), wrote {written} label(s) "
        f"({nulls} NULL on gaps/floor)"
        + (f" for {trade_date}" if trade_date else "")
    )
    return {"scanned": len(candidates), "written": written, "nulls": nulls}


if __name__ == "__main__":
    from utils.logger import setup_logger
    setup_logger("INFO")
    run_event_labeler(sys.argv[1] if len(sys.argv) > 1 else None)
