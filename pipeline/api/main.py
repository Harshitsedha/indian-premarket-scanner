"""
pipeline/api/main.py — PreMarket Pro read-only REST API.
Run: python api/run.py   (port 8001)
All endpoints use psycopg2 sync — FastAPI runs them in a thread pool.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from utils.config import settings
from processing.edge_stats import build_stats, query_rows

app = FastAPI(title="PreMarket Pro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET"],
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
            "rank":               r.get("rank"),
            "symbol":             r["symbol"],
            "sentiment":          r["sentiment"],
            "setup_type":         r["setup_type"],
            "thesis":             r["thesis"],
            "score":              r["score"],
            "prior_session_gap_pct": r["gap_pct"],     # DB column is gap_pct; field renamed
            "move_source":        r["gap_source"],  # DB column is gap_source; field renamed
            "mention_count":      r.get("mention_count") or 0,
            "signals":            r["signals"] or {},
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
