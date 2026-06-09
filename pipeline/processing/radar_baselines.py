"""
pipeline/processing/radar_baselines.py — Nightly baseline computation for the radar screener.

For each symbol in radar_universe, fetches 20 trading days of OHLCV via the Upstox
historical-candle endpoint (unadjusted, same convention as the existing gap computation)
and computes:
  - prev_close       : yesterday's close
  - avg_volume_20d   : 20-session mean daily volume
  - avg_range_pct_20d: mean (high-low)/close over 20 sessions (ATR% proxy)
  - prev_high, prev_low: yesterday's high/low

Writes one row per symbol into radar_baselines for today's trade date (upsert).
Run as Step 0 in the morning pipeline before 08:45 IST.

Usage:
    from processing.radar_baselines import run_baselines, baselines_already_computed
    if not baselines_already_computed(date.today().isoformat()):
        result = run_baselines()
        # result = {"computed": N, "failed": K, "total": M}
"""

import time
from datetime import date

import psycopg2
from loguru import logger

from utils.config import settings
from ingestion.upstox_client import UpstoxClient
from ingestion.universe import get_universe

_CANDLES_NEEDED     = 20
_SLEEP_BETWEEN      = 0.25   # ~50 s total for 200 symbols; within daily rate limits
_COVERAGE_THRESHOLD = 0.90   # skip recomputation if ≥90% of universe already done


# ── helpers ───────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )


def _compute_baseline(symbol: str, client: UpstoxClient) -> dict | None:
    """
    Fetch OHLCV history and compute baseline metrics.
    Uses get_ohlcv_history (unadjusted candles via historical-candle endpoint) —
    the same data source as the existing gap computation in upstox_client.py.
    Returns None on any error or insufficient data.
    """
    try:
        candles = client.get_ohlcv_history(symbol, days=_CANDLES_NEEDED)
        if len(candles) < 2:
            logger.warning(
                f"baselines: not enough candles for {symbol} "
                f"({len(candles)} returned, need ≥ 2)"
            )
            return None

        # Keep only the last _CANDLES_NEEDED rows (oldest-first list; tail = most recent)
        recent    = candles[-_CANDLES_NEEDED:]
        yesterday = recent[-1]

        volumes = [c["volume"] for c in recent if c.get("volume", 0) > 0]
        ranges  = [
            (c["high"] - c["low"]) / c["close"]
            for c in recent
            if c.get("close", 0) > 0
        ]

        avg_vol   = int(sum(volumes) / len(volumes)) if volumes else 0
        avg_range = sum(ranges) / len(ranges)        if ranges  else 0.0

        return {
            "symbol":             symbol.upper(),
            "trade_date":         date.today().isoformat(),
            "prev_close":         yesterday["close"],
            "avg_volume_20d":     avg_vol,
            "avg_range_pct_20d":  round(avg_range, 6),
            "prev_high":          yesterday["high"],
            "prev_low":           yesterday["low"],
        }
    except Exception as exc:
        logger.error(f"baselines: error computing {symbol}: {exc}")
        return None


def _upsert_baselines(rows: list[dict]) -> None:
    if not rows:
        return
    conn = _db()
    try:
        with conn, conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO radar_baselines
                        (symbol, trade_date, prev_close, avg_volume_20d,
                         avg_range_pct_20d, prev_high, prev_low)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, trade_date) DO UPDATE
                      SET prev_close        = EXCLUDED.prev_close,
                          avg_volume_20d    = EXCLUDED.avg_volume_20d,
                          avg_range_pct_20d = EXCLUDED.avg_range_pct_20d,
                          prev_high         = EXCLUDED.prev_high,
                          prev_low          = EXCLUDED.prev_low
                    """,
                    (
                        r["symbol"], r["trade_date"], r["prev_close"],
                        r["avg_volume_20d"], r["avg_range_pct_20d"],
                        r["prev_high"], r["prev_low"],
                    ),
                )
    finally:
        conn.close()
    logger.info(f"baselines: persisted {len(rows)} rows for {rows[0]['trade_date']}")


# ── public API ────────────────────────────────────────────────────────────────

def baselines_already_computed(trade_date: str, threshold: float = _COVERAGE_THRESHOLD) -> bool:
    """
    Return True if ≥threshold of radar_universe symbols already have baselines for trade_date.
    Returns False on any DB error (conservative — a re-run is always safe).
    """
    try:
        conn = _db()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM radar_universe")
            total = cur.fetchone()[0]
            if total == 0:
                return False
            cur.execute(
                "SELECT COUNT(*) FROM radar_baselines WHERE trade_date = %s",
                (trade_date,),
            )
            computed = cur.fetchone()[0]
        conn.close()
        coverage = computed / total
        logger.debug(
            f"baselines: coverage for {trade_date}: {computed}/{total} = {coverage:.0%}"
        )
        return coverage >= threshold
    except Exception as exc:
        logger.warning(f"baselines: coverage check failed: {exc}")
        return False  # conservative: re-run on error


def run_baselines() -> dict:
    """
    Compute and persist today's baselines for all radar_universe symbols.
    Returns {"computed": N, "failed": K, "total": M}.
    Skips gracefully (returns totals with 0 computed) if the universe is empty.
    """
    universe = get_universe()
    if not universe:
        logger.warning(
            "baselines: universe is empty — run refresh_universe() first, skipping"
        )
        return {"computed": 0, "failed": 0, "total": 0}

    client:   UpstoxClient = UpstoxClient()
    rows:     list[dict]   = []
    failures: list[str]    = []

    for i, entry in enumerate(universe, 1):
        symbol = entry["symbol"]
        logger.debug(f"baselines: [{i}/{len(universe)}] {symbol}")
        baseline = _compute_baseline(symbol, client)
        if baseline:
            rows.append(baseline)
        else:
            failures.append(symbol)
        time.sleep(_SLEEP_BETWEEN)

    _upsert_baselines(rows)
    logger.info(
        f"Radar baselines: {len(rows)} computed, {len(failures)} failed "
        f"out of {len(universe)} total"
        + (f" — failed: {failures[:20]}" if failures else "")
    )
    return {"computed": len(rows), "failed": len(failures), "total": len(universe)}
