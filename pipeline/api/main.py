"""
pipeline/api/main.py — PreMarket Pro read-only REST API.
Run: python api/run.py   (port 8001)
All endpoints use psycopg2 sync — FastAPI runs them in a thread pool.
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from loguru import logger
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from utils.config import settings
from processing.edge_stats import build_stats, query_rows
from processing.tagging_universe import TAGGING_UNIVERSE_SET
from backtest.features import FEATURES, parse_features
from backtest.recorder import _RESULTS_DIR

# NSE indices accepted in the single-symbol run/record backtest path only.
# Deliberately NOT added to TAGGING_UNIVERSE_SET / tagging_universe.txt, so the
# radar universe (ingestion.universe._load_tagging_universe) stays byte-identical.
# Resolution to NSE_INDEX|... happens in run_single via _resolve_symbol_key.
_BACKTEST_INDEX_SYMBOLS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})
import storage.redis_client as _cache

app = FastAPI(title="PreMarket Pro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )


def _safe(obj: Any) -> Any:
    """Recursively convert Decimal→float and date/datetime→ISO string."""
    if isinstance(obj, dict):
        return {k: _safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(i) for i in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):     # datetime is a subclass of date; check first
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


# ── GET /api/health ───────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

            cur.execute(
                "SELECT expires_at FROM upstox_tokens ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            token_expires = row[0].isoformat() if row and row[0] else None

            cur.execute(
                "SELECT trading_date FROM daily_briefings ORDER BY trading_date DESC LIMIT 1"
            )
            row = cur.fetchone()
            last_briefing = row[0].isoformat() if row and row[0] else None

    except Exception as exc:
        return {
            "status": "error",
            "db": str(exc),
            "upstox_token_expires": None,
            "last_briefing": None,
        }
    finally:
        conn.close()

    return {
        "status": "ok",
        "db": "connected",
        "upstox_token_expires": token_expires,
        "last_briefing": last_briefing,
    }


# ── GET /api/market-snapshot ─────────────────────────────────────────────────

_SNAPSHOT_CACHE_KEY = "market_snapshot:v1"
_SNAPSHOT_TTL       = 60   # seconds

@app.get("/api/market-snapshot")
def market_snapshot():
    """
    Return live market snapshot tiles (indices, commodities, FX).
    Cached in Redis for 60 s. Each failed tile has stale=True and null prices
    rather than raising an error — the caller should render '—' for stale tiles.
    """
    cached = _cache.get(_SNAPSHOT_CACHE_KEY)
    if cached is not None:
        return cached

    try:
        from ingestion.market_snapshot import fetch_snapshot
        tiles = fetch_snapshot()
    except Exception as exc:
        logger.error(f"market_snapshot fetch failed: {exc}")
        tiles = []

    payload = _safe({"tiles": tiles})
    _cache.set_ex(_SNAPSHOT_CACHE_KEY, payload, _SNAPSHOT_TTL)
    return payload


# ── GET /api/radar ────────────────────────────────────────────────────────────

_RADAR_SNAPSHOT_KEY = "radar:snapshot"
_RADAR_STATUS_KEY   = "radar:status"
_RADAR_STALE_SECS   = 180   # snapshot older than 3 min is considered stale
_IST                = timedelta(hours=5, minutes=30)

@app.get("/api/radar")
def radar():
    """
    Return the latest intraday radar snapshot from Redis plus a market_open flag.
    The snapshot is produced by pipeline/realtime/radar_poller.py every 45 s.
    Returns stale=true if the snapshot is >3 min old. Returns the last snapshot
    regardless of age — never 404 — so the UI can always render something.
    """
    snapshot = _cache.get(_RADAR_SNAPSHOT_KEY)
    status   = _cache.get(_RADAR_STATUS_KEY) or {}

    stale = False
    if snapshot:
        try:
            generated_at = datetime.fromisoformat(snapshot["generated_at"])
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - generated_at).total_seconds()
            stale = age > _RADAR_STALE_SECS
        except Exception:
            stale = True

    now_ist  = datetime.now(timezone.utc).astimezone(timezone(_IST))
    wd       = now_ist.weekday()   # 0 = Mon, 6 = Sun
    h, m     = now_ist.hour, now_ist.minute
    market_open = (
        wd < 5
        and (h > 9 or (h == 9 and m >= 15))
        and (h < 15 or (h == 15 and m <= 30))
    )

    return {
        "snapshot":    snapshot,
        "stale":       stale,
        "market_open": market_open,
        "status":      status,
        "ranges":      _load_active_ranges(),
    }


# ── OR range definitions ──────────────────────────────────────────────────────

from datetime import time as _dtime

from realtime.orb_ranges import (
    MAX_ACTIVE_RANGES,
    SESSION_END as _OR_SESSION_END,
    SESSION_START as _OR_SESSION_START,
    default_or_end,
    range_label,
)


def _load_active_ranges() -> list[dict]:
    """Active range defs for today (standard + today's session). [] on DB error."""
    default_end = default_or_end()
    try:
        conn = _db()
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, or_start, or_end, scope, session_date
                FROM   orb_range_defs
                WHERE  active = TRUE
                  AND  (scope = 'standard' OR (scope = 'session' AND session_date = %s))
                ORDER BY or_start, or_end
                """,
                (date.today(),),
            )
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as exc:
        logger.warning(f"radar ranges load failed: {exc}")
        return []

    out = []
    for r in rows:
        out.append({
            "id":           r["id"],
            "name":         r["name"],
            "or_start":     r["or_start"].strftime("%H:%M"),
            "or_end":       r["or_end"].strftime("%H:%M"),
            "scope":        r["scope"],
            "session_date": r["session_date"].isoformat() if r["session_date"] else None,
            "label":        range_label(r["or_start"], r["or_end"]),
            "is_default":   (
                r["scope"] == "standard"
                and r["or_start"] == _OR_SESSION_START
                and r["or_end"] == default_end
            ),
        })
    return out


class CreateRangeRequest(BaseModel):
    name:     str
    or_start: str   # "HH:MM"
    or_end:   str   # "HH:MM"
    scope:    str   # "session" | "standard"


def _parse_hm(value: str, field: str) -> _dtime:
    try:
        parts = value.split(":")
        return _dtime(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError) as exc:
        raise HTTPException(422, f"Invalid {field} {value!r} — expected HH:MM") from exc


@app.get("/api/radar/ranges")
def list_radar_ranges():
    return _load_active_ranges()


@app.post("/api/radar/ranges", status_code=201)
def create_radar_range(req: CreateRangeRequest):
    if not req.name.strip():
        raise HTTPException(422, "name must not be empty")
    if req.scope not in ("session", "standard"):
        raise HTTPException(422, f"scope must be 'session' or 'standard', got {req.scope!r}")

    or_start = _parse_hm(req.or_start, "or_start")
    or_end   = _parse_hm(req.or_end, "or_end")
    if or_start < _OR_SESSION_START:
        raise HTTPException(422, f"or_start must be >= {_OR_SESSION_START:%H:%M}")
    if or_end > _OR_SESSION_END:
        raise HTTPException(422, f"or_end must be <= {_OR_SESSION_END:%H:%M}")
    if or_end <= or_start:
        raise HTTPException(422, "or_end must be after or_start")

    if len(_load_active_ranges()) >= MAX_ACTIVE_RANGES:
        raise HTTPException(
            422, f"Max {MAX_ACTIVE_RANGES} active ranges per day (poller cost control)"
        )

    session_date = date.today() if req.scope == "session" else None

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM orb_range_defs
                WHERE  or_start = %s AND or_end = %s AND scope = %s
                  AND  session_date IS NOT DISTINCT FROM %s
                """,
                (or_start, or_end, req.scope, session_date),
            )
            existing = cur.fetchone()
            if existing:
                # Re-activate a previously soft-deleted identical def instead of 409ing
                cur.execute(
                    """
                    UPDATE orb_range_defs SET active = TRUE, name = %s
                    WHERE id = %s AND active = FALSE
                    """,
                    (req.name.strip(), existing[0]),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    raise HTTPException(409, "An identical active range already exists")
                conn.commit()
                return {"id": existing[0], "reactivated": True}

            cur.execute(
                """
                INSERT INTO orb_range_defs (name, or_start, or_end, scope, session_date)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (req.name.strip(), or_start, or_end, req.scope, session_date),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return {"id": new_id, "reactivated": False}


@app.delete("/api/radar/ranges/{range_id}")
def delete_radar_range(range_id: int):
    """Soft delete (active=false). The default OR window is protected."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT or_start, or_end, scope FROM orb_range_defs WHERE id = %s",
                (range_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Range not found")
            or_start, or_end, scope = row
            if (
                scope == "standard"
                and or_start == _OR_SESSION_START
                and or_end == default_or_end()
            ):
                raise HTTPException(403, "The default OR range cannot be deleted")

            cur.execute(
                "UPDATE orb_range_defs SET active = FALSE WHERE id = %s AND active = TRUE",
                (range_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise HTTPException(409, "Range is already inactive")
            conn.commit()
    finally:
        conn.close()
    return {"deleted": range_id}


# ── GET /api/briefing/today ───────────────────────────────────────────────────

@app.get("/api/briefing/today")
def briefing_today():
    # trading_date is the *previous* trading day (from Upstox candles), not today's
    # calendar date — always query the most recent briefing by id to avoid date mismatch.
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trading_date FROM daily_briefings ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    today = row[0] if row else date.today()
    conn  = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, trading_date, bias_direction, bias_score, bias_strength,
                       bias_summary, fii_net, dii_net, created_at
                FROM daily_briefings
                WHERE trading_date = %s
                ORDER BY id DESC LIMIT 1
                """,
                (today,),
            )
            briefing = cur.fetchone()
            briefing = dict(briefing) if briefing else None

            briefing_id = briefing["id"] if briefing else None

            cur.execute(
                """
                SELECT
                    s.id        AS setup_id,
                    s.symbol,
                    s.setup_type,
                    s.hypothesis AS sentiment,
                    s.score,
                    s.gap_pct,
                    s.gap_source,
                    s.signals,
                    s.thesis,
                    s.catalyst_line,
                    s.direction,
                    s.bias_confidence,
                    sip.rank,
                    sip.mention_count
                FROM setups s
                LEFT JOIN stocks_in_play sip
                    ON  sip.symbol      = s.symbol
                    AND sip.briefing_id = %s
                WHERE s.trading_date = %s
                ORDER BY COALESCE(sip.rank, 99), s.score DESC
                """,
                (briefing_id, today),
            )
            stocks_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not briefing and not stocks_rows:
        return _safe({
            "date":         today.isoformat(),
            "bias":         None,
            "stocks":       [],
            "generated_at": None,
        })

    # Bias block
    bias = None
    if briefing:
        fii_net = briefing.get("fii_net")
        dii_net = briefing.get("dii_net")
        fii_dii = (
            f"FII: {float(fii_net):+,.0f} cr | DII: {float(dii_net):+,.0f} cr"
            if fii_net is not None and dii_net is not None
            else ""
        )
        confidence = stocks_rows[0].get("bias_confidence") if stocks_rows else None
        bias = {
            "direction":      briefing["bias_direction"],
            "confidence":     confidence,
            "score":          briefing["bias_score"],
            "reason":         briefing["bias_summary"],
            "fii_dii":        fii_dii,
            "global_summary": None,   # not persisted in DB
        }

    stocks_out = [
        {
            "rank":                  r.get("rank"),
            "symbol":                r["symbol"],
            "sentiment":             r["sentiment"],
            "setup_type":            r["setup_type"],
            "thesis":                r["thesis"],
            "catalyst_line":         r.get("catalyst_line"),   # new
            "direction":             r.get("direction"),        # new
            "score":                 r["score"],
            "gap_pct":               r["gap_pct"],   # field the frontend reads
            "prior_session_gap_pct": r["gap_pct"],   # kept for API compat
            "move_source":           r["gap_source"],
            "mention_count":         r.get("mention_count") or 0,
            "signals":               r["signals"] or {},
        }
        for r in stocks_rows
    ]

    return _safe({
        "date":         today.isoformat(),
        "bias":         bias,
        "stocks":       stocks_out,
        "generated_at": briefing["created_at"].isoformat()
                        if briefing and briefing.get("created_at") else None,
    })


# ── GET /api/briefing/history ─────────────────────────────────────────────────

@app.get("/api/briefing/history")
def briefing_history(days: int = Query(default=14, ge=1, le=90)):
    cutoff = date.today() - timedelta(days=days)
    conn   = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    s.trading_date, s.symbol, s.bias_direction,
                    s.bias_confidence, s.score,
                    o.hypothesis_correct
                FROM setups s
                LEFT JOIN outcomes o ON o.setup_id = s.id
                WHERE s.trading_date >= %s
                ORDER BY s.trading_date DESC, s.score DESC
                """,
                (cutoff,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # Group by date in Python
    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        by_date[r["trading_date"].isoformat()].append(r)

    result = []
    for d in sorted(by_date, reverse=True):
        day_rows = by_date[d]
        outcomes = [r for r in day_rows if r["hypothesis_correct"] is not None]
        correct  = [r for r in outcomes if r["hypothesis_correct"] is True]
        hit_rate = round(len(correct) / len(outcomes), 2) if outcomes else None
        top3     = [r["symbol"] for r in day_rows[:3]]
        result.append({
            "date":                    d,
            "bias_direction":          day_rows[0]["bias_direction"],
            "bias_confidence":         day_rows[0]["bias_confidence"],
            "setup_count":             len(day_rows),
            "outcome_count":           len(outcomes),
            "hypothesis_correct_rate": hit_rate,
            "top_symbols":             top3,
        })

    return _safe(result)


# ── GET /api/setups/{date_str} ────────────────────────────────────────────────

@app.get("/api/setups/{date_str}")
def setups_for_date(date_str: str):
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {date_str}")

    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    s.id AS setup_id, s.symbol, s.setup_type, s.hypothesis,
                    s.score, s.gap_pct, s.gap_source, s.signals, s.thesis,
                    o.move_pct, o.hypothesis_correct, o.result, o.actual_gap_pct
                FROM setups s
                LEFT JOIN outcomes o ON o.setup_id = s.id
                WHERE s.trading_date = %s
                ORDER BY s.score DESC
                """,
                (target,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    result = []
    for r in rows:
        has_outcome = r["move_pct"] is not None or r["hypothesis_correct"] is not None
        result.append({
            "setup_id":   r["setup_id"],
            "symbol":     r["symbol"],
            "setup_type": r["setup_type"],
            "hypothesis": r["hypothesis"],
            "score":      r["score"],
            "prior_session_gap_pct": r["gap_pct"],     # DB column is gap_pct; field renamed
            "move_source":        r["gap_source"],  # DB column is gap_source; field renamed
            "signals":            r["signals"] or {},
            "thesis":             r["thesis"],
            "outcome":    {
                "move_pct":           r["move_pct"],
                "hypothesis_correct": r["hypothesis_correct"],
                "result":             r["result"],
                "actual_gap_pct":     r["actual_gap_pct"],
            } if has_outcome else None,
        })

    return _safe(result)


# ── GET /api/news/today ───────────────────────────────────────────────────────

@app.get("/api/news/today")
def news_today():
    # trading_date is stored as the previous trading day (from Upstox candles),
    # not the calendar date when the pipeline ran — always use the latest briefing.
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trading_date FROM daily_briefings ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    target = row[0] if row else date.today()
    return _news_for_date(target)


# ── GET /api/news/{date_str} ──────────────────────────────────────────────────

@app.get("/api/news/{date_str}")
def news_for_date(date_str: str):
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {date_str}")
    return _news_for_date(target)


def _news_for_date(target: date) -> dict:
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT h.id, h.source, h.headline, h.url, h.sentiment,
                       h.importance, h.reason, h.symbols, h.scraped_at
                FROM headlines h
                JOIN daily_briefings b ON b.id = h.briefing_id
                WHERE b.trading_date = %s
                ORDER BY h.importance DESC NULLS LAST, h.scraped_at DESC NULLS LAST
                """,
                (target,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return _safe({
        "date":      target.isoformat(),
        "count":     len(rows),
        "headlines": rows,
    })


# ── GET /api/edge/summary ─────────────────────────────────────────────────────

@app.get("/api/edge/summary")
def edge_summary(days: int = Query(default=90, ge=1, le=365)):
    conn = _db()
    try:
        rows  = query_rows(conn, days)
        stats = build_stats(rows)
    finally:
        conn.close()
    return _safe(stats)


# ── Backtest endpoints ────────────────────────────────────────────────────────


def _strategy_exists_and_validated(name: str) -> bool:
    """Return True iff name is in backtest_strategies with validated=true."""
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM backtest_strategies
                 WHERE name = %s AND validated = true AND deleted_at IS NULL
                """,
                (name,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


class CreateJobRequest(BaseModel):
    mode:            str          # "run" | "record" | "train_test"
    # run / record fields
    symbol:          str | None  = None    # single-symbol mode
    multi:           bool        = False   # watchlist mode
    start:           str         = ""
    end:             str         = ""
    strategy:        str         = "gap_and_go"
    interval:        str         = "minutes/1"
    ca_jump_pct:     float       = 20.0
    strict_ca:       bool        = False
    ca_ack:          bool        = True
    features:        str | None  = None    # required for record; required for train_test
    strategy_params: dict | None = None    # optional GapAndGo constructor overrides
    # train_test-specific fields
    train_csv:       str | None  = None    # absolute path to a completed Record CSV
    test_symbol:     str | None  = None    # test scope: single symbol
    test_multi:      bool        = False   # test scope: full watchlist
    test_start:      str         = ""
    test_end:        str         = ""


def _validate_job(req: CreateJobRequest) -> dict:
    """
    Validate a CreateJobRequest and return the params dict to store in the DB.
    Raises HTTPException 422 on any validation error.
    """
    if req.mode not in ("run", "record", "train_test"):
        raise HTTPException(422, f"mode must be 'run', 'record', or 'train_test', got {req.mode!r}")

    if not _strategy_exists_and_validated(req.strategy):
        raise HTTPException(422, f"Unknown or unvalidated strategy {req.strategy!r}")

    # ── train_test validation ─────────────────────────────────────────────────
    if req.mode == "train_test":
        if not req.train_csv:
            raise HTTPException(422, "train_csv is required for train_test mode")
        from pathlib import Path as _Path
        if not _Path(req.train_csv).exists():
            raise HTTPException(422, f"train_csv file not found on server: {req.train_csv}")
        if not req.features:
            raise HTTPException(422, "features is required for train_test mode")
        try:
            parse_features(req.features)
        except ValueError as exc:
            raise HTTPException(422, {"detail": str(exc), "available_features": ", ".join(sorted(FEATURES.keys()))}) from exc
        if not req.test_symbol and not req.test_multi:
            raise HTTPException(422, "Provide test_symbol or test_multi=true for train_test mode")
        if req.test_symbol and req.test_multi:
            raise HTTPException(422, "Provide test_symbol or test_multi=true, not both")
        try:
            date.fromisoformat(req.test_start)
            date.fromisoformat(req.test_end)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid test date: {exc}") from exc
        if date.fromisoformat(req.test_start) > date.fromisoformat(req.test_end):
            raise HTTPException(422, "test_start must be ≤ test_end")
        if req.test_symbol and req.test_symbol.upper() not in TAGGING_UNIVERSE_SET:
            raise HTTPException(400, f"Unknown symbol '{req.test_symbol.upper()}' — not in the backtest universe. Select from the autocomplete list.")

        params: dict = {
            "train_csv":   req.train_csv,
            "test_start":  req.test_start,
            "test_end":    req.test_end,
            "features":    req.features,
            "strategy":    req.strategy,
            "interval":    req.interval,
            "ca_jump_pct": req.ca_jump_pct,
            "strict_ca":   req.strict_ca,
            "ca_ack":      req.ca_ack,
        }
        if req.test_symbol:
            params["test_symbol"] = req.test_symbol.upper()
        else:
            params["test_multi"] = True
        if req.strategy_params:
            params["strategy_params"] = req.strategy_params
        return params

    # ── run / record validation ───────────────────────────────────────────────
    if not req.symbol and not req.multi:
        raise HTTPException(422, "Provide either symbol (single mode) or multi=true")
    if req.symbol and req.multi:
        raise HTTPException(422, "Provide symbol or multi=true, not both")

    if not req.start or not req.end:
        raise HTTPException(422, "start and end are required for run/record mode")

    try:
        date.fromisoformat(req.start)
        date.fromisoformat(req.end)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid date: {exc}") from exc

    if date.fromisoformat(req.start) > date.fromisoformat(req.end):
        raise HTTPException(422, "start must be ≤ end")
    if (req.symbol and req.symbol.upper() not in TAGGING_UNIVERSE_SET
            and req.symbol.upper() not in _BACKTEST_INDEX_SYMBOLS):
        raise HTTPException(400, f"Unknown symbol '{req.symbol.upper()}' — not in the backtest universe. Select from the autocomplete list.")

    if req.mode == "record":
        if not req.features:
            available = ", ".join(sorted(FEATURES.keys()))
            raise HTTPException(
                422,
                f"features is required for record mode. Available: {available}",
            )
        try:
            parse_features(req.features)
        except ValueError as exc:
            available = ", ".join(sorted(FEATURES.keys()))
            raise HTTPException(
                422,
                {"detail": str(exc), "available_features": available},
            ) from exc

    # Build the params dict the worker will pass as **kwargs to run_single/run_multi
    params = {
        "start":       req.start,
        "end":         req.end,
        "strategy":    req.strategy,
        "interval":    req.interval,
        "ca_jump_pct": req.ca_jump_pct,
        "strict_ca":   req.strict_ca,
    }
    if req.symbol:
        params["symbol"] = req.symbol.upper()
    else:
        params["ca_ack"] = req.ca_ack

    if req.mode == "record":
        params["features"] = req.features

    if req.strategy_params:
        params["strategy_params"] = req.strategy_params

    return params


# ── POST /api/backtest/jobs ───────────────────────────────────────────────────

@app.post("/api/backtest/jobs", status_code=201)
def create_job(req: CreateJobRequest):
    params = _validate_job(req)
    conn   = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO backtest_jobs (mode, params)
                VALUES (%s, %s)
                RETURNING id, created_at
                """,
                (req.mode, json.dumps(params)),
            )
            row = cur.fetchone()
            conn.commit()
    finally:
        conn.close()
    return {"job_id": str(row[0]), "status": "queued", "created_at": row[1].isoformat()}


# ── POST /api/backtest/cancel ─────────────────────────────────────────────────

class _CancelRequest(BaseModel):
    job_id: str


@app.post("/api/backtest/cancel")
def cancel_job(req: _CancelRequest):
    """
    Cancel a queued or running job.

    Two atomic conditional UPDATEs — no read-then-write TOCTOU race:
      queued  → cancelled  (immediate; finished_at set now)
      running → cancelling (signals the worker's poll thread)

    If neither matches (job is already in a terminal state), returns 409.
    The worker's poll thread sees 'cancelling' within 2s and raises _JobCancelled,
    which transitions the job to 'cancelled' with finished_at set.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            # Attempt 1: claim the queued→cancelled slot atomically
            cur.execute(
                "UPDATE backtest_jobs"
                "   SET status = 'cancelled', finished_at = NOW()"
                " WHERE id = %s AND status = 'queued'",
                (req.job_id,),
            )
            if cur.rowcount:
                conn.commit()
                return {"status": "cancelled"}

            # Attempt 2: signal the running worker → cancelling atomically
            cur.execute(
                "UPDATE backtest_jobs"
                "   SET status = 'cancelling'"
                " WHERE id = %s AND status = 'running'",
                (req.job_id,),
            )
            if cur.rowcount:
                conn.commit()
                return {"status": "cancelling"}

            # Job is in a terminal or unknown state — fetch for 409 body
            cur.execute(
                "SELECT status FROM backtest_jobs WHERE id = %s",
                (req.job_id,),
            )
            row = cur.fetchone()
            conn.rollback()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, "Job not found")
    raise HTTPException(409, f"Job already in state '{row[0]}'")


# ── DELETE /api/backtest/jobs/{job_id} ────────────────────────────────────────

# Statuses where a worker is actively executing — deletion is unsafe.
_ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})

# Path-safety checks reused from retention (same constraints).
_PROTECTED_PREFIXES = ("candidates_", "ca_report_")


@app.delete("/api/backtest/jobs/{job_id}")
def delete_job(job_id: str):
    """
    Permanently delete a job row and its result CSV.

    Only allowed when status is terminal (done, error, cancelled).
    Returns 409 for active jobs (queued/running/cancelling) — cancel first.

    FK ordering (within one transaction):
      1. DELETE backtest_rulesets WHERE job_id = %s   (clears the FK)
      2. DELETE backtest_jobs     WHERE id      = %s
      COMMIT
      3. unlink result file (best-effort — DB is already consistent)

    An orphaned file on disk (DB deleted, unlink failed) is harmless disk waste.
    A dangling DB pointer (file deleted, DB not updated) would cause 404 downloads.
    We always prefer the harmless failure mode.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result_path FROM backtest_jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Job not found")

        status, result_path = row

        if status in _ACTIVE_STATUSES:
            raise HTTPException(
                409,
                f"Job is active (status='{status}') — cancel it before deleting",
            )

        # Validate and resolve the file path BEFORE the transaction so we can
        # log it and detect safety issues without touching the DB.
        safe_path: Path | None = None
        if result_path:
            results_dir = _RESULTS_DIR.resolve()
            candidate = Path(result_path).resolve()
            if (
                results_dir in candidate.parents
                and candidate.suffix == ".csv"
                and not candidate.name.startswith(_PROTECTED_PREFIXES)
            ):
                safe_path = candidate
            else:
                logger.warning(
                    f"delete_job {job_id[:8]}: result_path {result_path!r} failed "
                    f"safety checks — DB rows will be deleted but file skipped"
                )

        # Transaction: rulesets first (FK), then job row, then commit.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM backtest_rulesets WHERE job_id = %s", (job_id,))
            cur.execute("DELETE FROM backtest_jobs     WHERE id      = %s", (job_id,))
        conn.commit()
    finally:
        conn.close()

    # File unlink is after commit — best-effort; DB is already clean.
    if safe_path is not None:
        logger.info(f"delete_job {job_id[:8]}: unlinking {safe_path.name}")
        try:
            if safe_path.exists():
                safe_path.unlink()
        except Exception as exc:
            logger.warning(
                f"delete_job {job_id[:8]}: file unlink failed ({exc}) — "
                f"harmless orphaned disk"
            )

    return {"deleted": True}


# ── GET /api/backtest/jobs ────────────────────────────────────────────────────

@app.get("/api/backtest/jobs")
def list_jobs(limit: int = Query(default=50, ge=1, le=200)):
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, mode, params, status, summary, error,
                       created_at, started_at, finished_at, result_path
                  FROM backtest_jobs
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return _safe(rows)


# ── GET /api/backtest/jobs/{id} ───────────────────────────────────────────────

@app.get("/api/backtest/jobs/{job_id}")
def get_job(job_id: str):
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, mode, params, status, summary, error,
                       created_at, started_at, finished_at, result_path
                  FROM backtest_jobs
                 WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"Job {job_id} not found")
    return _safe(dict(row))


# ── GET /api/backtest/jobs/{id}/result ───────────────────────────────────────

@app.get("/api/backtest/jobs/{job_id}/result")
def download_result(job_id: str):
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result_path FROM backtest_jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"Job {job_id} not found")
    status, result_path = row
    if status != "done" or not result_path:
        raise HTTPException(404, "Result not available (job not done)")
    path = Path(result_path)
    if not path.exists():
        raise HTTPException(404, f"Result file missing: {path.name}")
    return FileResponse(
        path        = str(path),
        media_type  = "text/csv",
        filename    = path.name,
    )


# ── GET /api/backtest/symbols ─────────────────────────────────────────────────

@app.get("/api/backtest/symbols")
def list_symbols():
    """Tagging universe + the two supported NSE indices, for the single-symbol
    backtest autocomplete. Indices are appended here only — not in the shared
    TAGGING_UNIVERSE_SET — so the radar universe is unaffected."""
    return [{"symbol": s} for s in sorted(TAGGING_UNIVERSE_SET | _BACKTEST_INDEX_SYMBOLS)]


# ── GET /api/backtest/features ────────────────────────────────────────────────

@app.get("/api/backtest/features")
def list_features():
    """Return the feature registry so the frontend can build the Record form."""
    return {
        "features": [
            {
                "name":        name,
                "description": spec.description,
                "params": [
                    {
                        "name":    p.name,
                        "type":    p.type.__name__,
                        "default": p.default,
                    }
                    for p in spec.params
                ],
            }
            for name, spec in FEATURES.items()
        ]
    }


# ── GET /api/backtest/rulesets/{job_id} ───────────────────────────────────────

@app.get("/api/backtest/rulesets/{job_id}")
def get_ruleset(job_id: str):
    """Return the committed ruleset for a train_test job."""
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, job_id, train_csv, rules_raw, rules_parsed,
                       feature_stats, committed_at
                  FROM backtest_rulesets
                 WHERE job_id = %s
                 ORDER BY committed_at DESC
                 LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"No ruleset found for job {job_id}")
    return _safe(dict(row))


# ── Strategy Manager endpoints ────────────────────────────────────────────────

import re as _re


class _GenerateRequest(BaseModel):
    description:   str
    existing_code: str | None = None


class _ValidateRequest(BaseModel):
    name:         str    # machine name: lowercase alphanum + underscores
    display_name: str
    description:  str
    code:         str


@app.post("/api/backtest/strategies/generate")
def generate_strategy_endpoint(req: _GenerateRequest):
    """Call Claude to generate a strategy class. Returns {code: str}."""
    if not req.description.strip():
        raise HTTPException(422, "description must not be empty")
    from backtest.strategy_gen import generate_strategy
    try:
        code = generate_strategy(req.description, req.existing_code)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"code": code}


@app.post("/api/backtest/strategies/validate")
def validate_strategy_endpoint(req: _ValidateRequest):
    """
    Call Claude to validate the strategy code, then persist to DB.
    Returns the full ValidationReport regardless of pass/fail.
    If passed=true, the strategy is marked validated and appears in the dropdown.
    """
    if not _re.match(r'^[a-z][a-z0-9_]*$', req.name):
        raise HTTPException(
            422,
            "name must be lowercase letters/digits/underscores, starting with a letter",
        )
    if req.name == "gap_and_go":
        raise HTTPException(403, "gap_and_go is the immutable baseline and cannot be modified")
    if not req.display_name.strip():
        raise HTTPException(422, "display_name must not be empty")
    if not req.code.strip():
        raise HTTPException(422, "code must not be empty")

    from backtest.strategy_val import validate_strategy
    try:
        report = validate_strategy(req.code)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO backtest_strategies
                    (name, display_name, description, code, validated, validation_report)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    display_name      = EXCLUDED.display_name,
                    description       = EXCLUDED.description,
                    code              = EXCLUDED.code,
                    validated         = EXCLUDED.validated,
                    validation_report = EXCLUDED.validation_report,
                    updated_at        = NOW(),
                    deleted_at        = NULL
                """,
                (
                    req.name, req.display_name, req.description,
                    req.code, report["passed"], json.dumps(report),
                ),
            )
            conn.commit()
    finally:
        conn.close()

    # Evict cache so next load_strategy call picks up fresh code
    from backtest.loader import _CACHE
    _CACHE.clear()

    return report


@app.get("/api/backtest/strategies")
def list_validated_strategies():
    """Return validated strategies (for the job form dropdown). No code field."""
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, display_name, description,
                       validated, created_at, updated_at
                  FROM backtest_strategies
                 WHERE validated = true AND deleted_at IS NULL
                 ORDER BY name
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return _safe(rows)


@app.get("/api/backtest/strategies/all")
def list_all_strategies():
    """Return all strategies including drafts (for Strategy Manager UI). Includes code."""
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, display_name, description, code,
                       validated, validation_report, created_at, updated_at
                  FROM backtest_strategies
                 WHERE deleted_at IS NULL
                 ORDER BY name
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return _safe(rows)


# ── POST /api/backtest/report ─────────────────────────────────────────────────

class _ReportRequest(BaseModel):
    job_id: str


@app.post("/api/backtest/report")
def generate_backtest_report(req: _ReportRequest):
    """
    Load the result CSV for a completed run/train_test job, compute full stats,
    and return a Claude-written markdown analysis.
    """
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mode, status, result_path FROM backtest_jobs WHERE id = %s",
                (req.job_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(404, f"Job {req.job_id} not found")
    mode, status, result_path = row
    if status != "done":
        raise HTTPException(400, "Job is not done yet")
    if not result_path:
        raise HTTPException(404, "Job has no result file (record-mode jobs have no trade CSV)")
    if mode not in ("run", "train_test"):
        raise HTTPException(400, f"AI report is only available for run/train_test jobs, not {mode!r}")

    path = Path(result_path)
    if not path.exists():
        raise HTTPException(404, f"Result file missing on server: {path.name}")

    from backtest.report_stats import load_and_compute
    from backtest.claude_report import generate_report

    try:
        stats = load_and_compute(path)
    except Exception as exc:
        raise HTTPException(422, f"Stats computation failed: {exc}") from exc

    if stats.get("total_trades", 0) == 0:
        raise HTTPException(422, "No trades in result CSV — nothing to analyse")

    try:
        markdown = generate_report(stats)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"markdown": markdown}


@app.delete("/api/backtest/strategies/{name}")
def delete_strategy(name: str):
    """Soft-delete a strategy (sets deleted_at). gap_and_go is immutable — returns 403."""
    if name == "gap_and_go":
        raise HTTPException(403, "gap_and_go is the immutable baseline and cannot be deleted")
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE backtest_strategies
                   SET deleted_at = NOW()
                 WHERE name = %s AND deleted_at IS NULL
                """,
                (name,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise HTTPException(404, f"Strategy {name!r} not found")
            conn.commit()
    finally:
        conn.close()

    # Evict cache
    from backtest.loader import _CACHE
    _CACHE.clear()

    return {"deleted": name}
