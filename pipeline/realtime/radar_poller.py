"""
pipeline/realtime/radar_poller.py — Intraday radar screener poller.

Runs as a standalone long-lived process during market hours (09:15–15:30 IST, Mon-Fri).
Every 45 seconds it calls Upstox Full Market Quote for all universe instruments in a
single GET request, computes per-symbol metrics, and writes one snapshot to Redis.

Redis keys:
  radar:snapshot  — full snapshot JSON with generated_at + rows[], TTL 5 min
  radar:status    — {"state": "ok" | "token_expired"}, TTL 5 min

Token 401 handling:
  - Sends a single Telegram alert (once per process lifetime)
  - Sets radar:status = {"state": "token_expired"}
  - Backs off to 5-minute retries until the token is refreshed

Usage:
    python realtime/radar_poller.py
"""

import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
import httpx
from loguru import logger

from utils.config import settings
from utils.logger import setup_logger
from ingestion.universe import get_universe
from ingestion.upstox_client import _get_bearer_token
import storage.redis_client as _cache


# ── constants ─────────────────────────────────────────────────────────────────

_POLL_INTERVAL    = 45    # seconds between polls during market hours
_BACKOFF_INTERVAL = 300   # seconds to back off after a 401
_SNAPSHOT_TTL     = 300   # Redis TTL in seconds (5 min)
_SNAPSHOT_KEY     = "radar:snapshot"
_STATUS_KEY       = "radar:status"

_BASE = "https://api.upstox.com/v2"

IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_START_H, _SESSION_START_M = 9, 15
_SESSION_END_H,   _SESSION_END_M   = 15, 30
_SESSION_MINS = 375   # minutes from 09:15 to 15:30

# NSE trading holidays — extend this list annually before year-start
HOLIDAYS: frozenset[str] = frozenset({
    "2026-01-26",  # Republic Day
    "2026-03-25",  # Holi
    "2026-04-02",  # Ram Navami
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-04-17",  # Good Friday
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-11-04",  # Diwali Laxmi Puja
    "2026-12-25",  # Christmas
})

# Sent at most once per process lifetime to avoid Telegram spam on repeated 401s
_token_alerted: bool = False


# ── market hours ──────────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:                             # Saturday / Sunday
        return False
    if now.strftime("%Y-%m-%d") in HOLIDAYS:
        return False
    start = now.replace(hour=_SESSION_START_H, minute=_SESSION_START_M, second=0, microsecond=0)
    end   = now.replace(hour=_SESSION_END_H,   minute=_SESSION_END_M,   second=0, microsecond=0)
    return start <= now <= end


def _session_fraction() -> float:
    """
    Pro-rata fraction of the session elapsed: minutes_since_0915 / 375.
    Clamped to [0.05, 1.0] so RVOL is never division-by-zero in the first few seconds.
    v1 — straight pro-rata. TODO v2: use an intraday volume-profile curve for more accurate RVOL.
    """
    now   = datetime.now(IST)
    start = now.replace(hour=_SESSION_START_H, minute=_SESSION_START_M, second=0, microsecond=0)
    elapsed_mins = max(0.0, (now - start).total_seconds() / 60)
    return max(0.05, min(1.0, elapsed_mins / _SESSION_MINS))


# ── Telegram alert ────────────────────────────────────────────────────────────

def _send_token_alert() -> None:
    global _token_alerted
    if _token_alerted:
        return
    _token_alerted = True
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text":    "🔴 Radar poller: Upstox token expired — re-run upstox_auth.py",
            },
            timeout=10,
        )
        logger.warning("radar_poller: sent Telegram 401 alert")
    except Exception as exc:
        logger.error(f"radar_poller: Telegram 401 alert failed: {exc}")


# ── Upstox full market quote ──────────────────────────────────────────────────

def _build_http() -> httpx.Client:
    token   = _get_bearer_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=20)


def _get_full_quotes(http: httpx.Client, instrument_keys: list[str]) -> dict | None:
    """
    GET /v2/market-quote/quotes for up to 500 instruments in one request.
    Returns the data dict keyed by instrument_key, or None on network error.
    Returns {"_401": True} exclusively on HTTP 401 so the caller can handle it.
    """
    encoded = ",".join(k.replace("|", "%7C") for k in instrument_keys)
    url     = f"{_BASE}/market-quote/quotes?instrument_key={encoded}"

    try:
        resp = http.get(url)
    except Exception as exc:
        logger.error(f"radar_poller: HTTP request failed: {exc}")
        return None

    if resp.status_code == 401:
        return {"_401": True}

    if resp.status_code == 429:
        logger.warning("radar_poller: 429 rate limit — skipping this cycle")
        return None

    if not resp.is_success:
        logger.error(f"radar_poller: HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        body = resp.json()
    except Exception:
        logger.error("radar_poller: failed to parse JSON response")
        return None

    raw = body.get("data", {})
    # Upstox may return pipe-encoded keys (%7C) or literal pipe (|) — normalise to pipe.
    return {k.replace("%7C", "|").replace("%7c", "|"): v for k, v in raw.items()}


# ── metric computation ────────────────────────────────────────────────────────

def _compute_row(
    symbol:           str,
    entry:            dict,
    baseline:         dict | None,
    session_fraction: float,
    index_membership: str,
) -> dict:
    """Build one radar row dict from a live quote entry and a baseline record."""
    ltp  = float(entry.get("last_price") or 0) or None
    ohlc = entry.get("ohlc") or {}

    # Upstox intraday: ohlc.open/high/low = today's values; ohlc.close = prev session close
    today_open = float(ohlc.get("open")  or 0) or None
    high       = float(ohlc.get("high")  or 0) or None
    low        = float(ohlc.get("low")   or 0) or None
    api_prev   = float(ohlc.get("close") or 0) or None
    volume     = int(entry.get("volume") or 0)

    # Prefer DB baseline for prev_close; fall back to API's ohlc.close
    prev_close     = (baseline.get("prev_close")       if baseline else None) or api_prev
    avg_volume_20d = baseline.get("avg_volume_20d")    if baseline else None
    avg_range_pct  = baseline.get("avg_range_pct_20d") if baseline else None

    # gap_pct: fixed after open — null if today_open not yet available
    gap_pct = (
        round((today_open - prev_close) / prev_close * 100, 2)
        if today_open and prev_close and prev_close > 0
        else None
    )

    change_pct = (
        round((ltp - prev_close) / prev_close * 100, 2)
        if ltp and prev_close and prev_close > 0
        else None
    )

    change_from_open_pct = (
        round((ltp - today_open) / today_open * 100, 2)
        if ltp and today_open and today_open > 0
        else None
    )

    # RVOL v1: pro-rata based on session elapsed fraction
    # TODO v2: replace with intraday volume-profile curve normalization
    rvol = None
    if volume and avg_volume_20d and avg_volume_20d > 0:
        expected = avg_volume_20d * session_fraction
        if expected > 0:
            rvol = round(volume / expected, 2)

    range_used_pct = (
        round((high - low) / prev_close * 100, 2)
        if high is not None and low is not None and prev_close and prev_close > 0
        else None
    )

    atr_multiple = (
        round(range_used_pct / avg_range_pct, 2)
        if range_used_pct is not None and avg_range_pct and avg_range_pct > 0
        else None
    )

    return {
        "symbol":               symbol,
        "ltp":                  ltp,
        "prev_close":           prev_close,
        "open":                 today_open,
        "high":                 high,
        "low":                  low,
        "volume":               volume,
        "gap_pct":              gap_pct,
        "change_pct":           change_pct,
        "change_from_open_pct": change_from_open_pct,
        "rvol":                 rvol,
        "range_used_pct":       range_used_pct,
        "atr_multiple":         atr_multiple,
        "index_membership":     index_membership,
    }


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_baselines(trade_date: str) -> dict[str, dict]:
    """Load today's baselines from DB, keyed by symbol. Returns {} on error."""
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        rows: dict[str, dict] = {}
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, prev_close, avg_volume_20d,
                       avg_range_pct_20d, prev_high, prev_low
                FROM   radar_baselines
                WHERE  trade_date = %s
                """,
                (trade_date,),
            )
            for sym, pc, av, ar, ph, pl in cur.fetchall():
                rows[sym] = {
                    "prev_close":        float(pc) if pc else None,
                    "avg_volume_20d":    int(av)   if av else None,
                    "avg_range_pct_20d": float(ar) if ar else None,
                    "prev_high":         float(ph) if ph else None,
                    "prev_low":          float(pl) if pl else None,
                }
        conn.close()
        return rows
    except Exception as exc:
        logger.error(f"radar_poller: baselines load failed: {exc}")
        return {}


# ── poll cycle ────────────────────────────────────────────────────────────────

def _poll_cycle(
    http:      httpx.Client,
    universe:  list[dict],
    baselines: dict[str, dict],
) -> bool:
    """
    Execute one poll cycle.
    Returns False if a 401 was detected (caller should back off and rebuild HTTP client).
    """
    fraction = _session_fraction()

    key_to_meta: dict[str, tuple[str, str]] = {}
    for row in universe:
        if row.get("instrument_key"):
            key_to_meta[row["instrument_key"]] = (row["symbol"], row["index_membership"])

    if not key_to_meta:
        logger.warning("radar_poller: no instrument_keys in universe — skipping poll")
        return True

    quote_data = _get_full_quotes(http, list(key_to_meta.keys()))

    if quote_data is None:
        logger.warning("radar_poller: empty response — skipping cycle")
        return True

    if quote_data.get("_401"):
        logger.error("radar_poller: 401 detected — token expired")
        _send_token_alert()
        _cache.set_ex(_STATUS_KEY, {"state": "token_expired"}, _SNAPSHOT_TTL)
        return False

    radar_rows = []
    missing    = 0
    for ikey, (symbol, membership) in key_to_meta.items():
        entry = quote_data.get(ikey)
        if not entry:
            missing += 1
            continue
        baseline = baselines.get(symbol)
        radar_rows.append(_compute_row(symbol, entry, baseline, fraction, membership))

    if missing:
        logger.debug(f"radar_poller: {missing} instruments missing from quote response")

    snapshot = {
        "generated_at": datetime.now(IST).isoformat(),
        "rows":         radar_rows,
    }
    _cache.set_ex(_SNAPSHOT_KEY, snapshot, _SNAPSHOT_TTL)
    _cache.set_ex(_STATUS_KEY,   {"state": "ok"}, _SNAPSHOT_TTL)

    logger.info(
        f"radar_poller: snapshot written — {len(radar_rows)} rows, "
        f"session_fraction={fraction:.2f}, missing_instruments={missing}"
    )
    return True


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logger(settings.log_level)
    logger.info("radar_poller: starting up")

    universe = get_universe()
    if not universe:
        logger.error(
            "radar_poller: universe is empty — "
            "run pipeline/ingestion/universe.py refresh_universe() first; exiting"
        )
        sys.exit(1)
    logger.info(f"radar_poller: loaded {len(universe)} symbols from radar_universe")

    http       = _build_http()
    trade_date = date.today().isoformat()
    baselines  = _load_baselines(trade_date)
    logger.info(
        f"radar_poller: loaded {len(baselines)} baselines for {trade_date}"
        + (" (WARNING: 0 baselines — run radar_baselines.run_baselines() first)"
           if not baselines else "")
    )

    while True:
        if not _is_market_open():
            logger.debug("radar_poller: outside market hours — sleeping 60s")
            time.sleep(60)
            # Reload baselines when the calendar date changes
            new_date = date.today().isoformat()
            if new_date != trade_date:
                trade_date = new_date
                baselines  = _load_baselines(trade_date)
                logger.info(
                    f"radar_poller: new trade date {trade_date}, "
                    f"reloaded baselines ({len(baselines)} rows)"
                )
            continue

        ok = _poll_cycle(http, universe, baselines)
        if not ok:
            # 401 — back off and rebuild client in case the token was refreshed
            logger.warning(
                f"radar_poller: backing off {_BACKOFF_INTERVAL}s after 401"
            )
            time.sleep(_BACKOFF_INTERVAL)
            http = _build_http()
        else:
            time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    main()
