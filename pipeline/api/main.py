"""
pipeline/api/main.py — PreMarket Pro read-only REST API.
Run: python api/run.py   (port 8001)
All endpoints use psycopg2 sync — FastAPI runs them in a thread pool.
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from utils.config import settings
from processing.edge_stats import build_stats, query_rows
from backtest.features import FEATURES, parse_features

app = FastAPI(title="PreMarket Pro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
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


# ── Backtest endpoints ────────────────────────────────────────────────────────

_VALID_STRATEGIES = {"gap_and_go"}


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

    if req.strategy not in _VALID_STRATEGIES:
        raise HTTPException(422, f"Unknown strategy {req.strategy!r}")

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


# ── GET /api/backtest/jobs?mode=record&status=done (train dropdown) ───────────
# Already handled by list_jobs, but frontend filters by query params on the response.
