"""
pipeline/realtime/radar_poller.py — Intraday radar screener poller.

Runs as a standalone long-lived process during market hours (09:15–15:30 IST, Mon-Fri).
Every 45 seconds it calls Upstox Full Market Quote for all universe instruments in a
single GET request, computes per-symbol metrics, and writes one snapshot to Redis.

Redis keys written:
  radar:snapshot          — full snapshot JSON (TTL 5 min)
  radar:status            — {"state": "ok"|"token_expired"} (TTL 5 min)
  radar:polls:{date}:{symbol}           — per-poll intraday history LIST (TTL 24h)
  radar:or:{date}:{HH:MM}-{HH:MM}       — frozen OR per-symbol hash (TTL 24h)
  radar:or_state:{date}:{HH:MM}-{HH:MM} — per-range break state hash (TTL 24h)
  radar:day_meta:{date}   — per-symbol gap/news/close context for the EOD job (TTL 24h)
  radar:news:{YYYY-MM-DD} — news/catalyst cache (TTL 1h)
  radar:alerted:{date}:{rule}:{symbol} — dedup keys (TTL 24h)

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
from realtime.radar_alerts import load_rules, process_alerts
from realtime.radar_events import process_events
from realtime.orb_ranges import RangeTracker, load_range_defs


# ── constants ─────────────────────────────────────────────────────────────────

_POLL_INTERVAL    = 45    # seconds between polls during market hours
_BACKOFF_INTERVAL = 300   # seconds to back off after a 401
_SNAPSHOT_TTL     = 300   # Redis TTL in seconds (5 min)
_DAY_META_TTL     = 86_400  # 24h — read by the ORB EOD job
_NEWS_TTL         = 3_600   # 1h — news cache
_NEWS_REFRESH_SECS = 3_600  # reload news cache every hour
_DEFS_REFRESH_SECS = 300    # reload OR range defs every 5 min

_SNAPSHOT_KEY   = "radar:snapshot"
_STATUS_KEY     = "radar:status"
_FRAMES_STREAM  = "radar:frames"   # Edge-page persist worker consumes this (one entry per cycle)
_EVENTS_STREAM  = "radar:events"   # Phase 1 events — same worker, one entry per detected event
_DAY_META_KEY_PREFIX = "radar:day_meta:"
_NEWS_KEY_PREFIX = "radar:news:"

_BASE = "https://api.upstox.com/v2"

IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_START_H, _SESSION_START_M = 9, 15
_SESSION_END_H,   _SESSION_END_M   = 15, 30
_SESSION_MINS = 375   # minutes from 09:15 to 15:30

# NSE trading holidays — extend annually before year-start
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
    if now.weekday() >= 5:
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
    v1 — straight pro-rata. TODO v2: use an intraday volume-profile curve.
    """
    now   = datetime.now(IST)
    start = now.replace(hour=_SESSION_START_H, minute=_SESSION_START_M, second=0, microsecond=0)
    elapsed_mins = max(0.0, (now - start).total_seconds() / 60)
    return max(0.05, min(1.0, elapsed_mins / _SESSION_MINS))


# ── News / catalyst cache ─────────────────────────────────────────────────────

def _load_news_from_db() -> dict[str, dict]:
    """
    Query the most recent briefing for per-symbol catalyst_line and headline count.
    Uses the most recent briefing (ORDER BY id DESC) because setups.trading_date is
    the previous trading day, not today's calendar date.
    Returns {} on any DB error (graceful degradation — news fusion is non-fatal).
    """
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        result: dict[str, dict] = {}
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, trading_date FROM daily_briefings ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                conn.close()
                return {}
            briefing_id, trading_date = row

            # catalyst_line per symbol from setups for that trading date
            cur.execute(
                "SELECT symbol, catalyst_line FROM setups WHERE trading_date = %s",
                (trading_date,),
            )
            for sym, cat in cur.fetchall():
                result[sym.upper()] = {"catalyst_line": cat, "headline_count": 0}

            # headline count per symbol from the briefing's headlines
            cur.execute(
                "SELECT symbols FROM headlines WHERE briefing_id = %s",
                (briefing_id,),
            )
            for (syms,) in cur.fetchall():
                for sym in syms or []:
                    s = str(sym).upper()
                    if s in result:
                        result[s]["headline_count"] = result[s].get("headline_count", 0) + 1
                    else:
                        result[s] = {"catalyst_line": None, "headline_count": 1}
        conn.close()
        logger.info(
            f"news_cache: loaded {len(result)} symbols "
            f"(briefing_id={briefing_id}, trading_date={trading_date})"
        )
        return result
    except Exception as exc:
        logger.warning(f"news_cache: DB load failed (non-fatal): {exc}")
        return {}


def _load_news_cache(calendar_date: str) -> dict[str, dict]:
    """Load news cache from Redis (TTL 1h), falling back to DB on miss."""
    key = f"{_NEWS_KEY_PREFIX}{calendar_date}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    data = _load_news_from_db()
    if data:
        _cache.set_ex(key, data, _NEWS_TTL)
    return data


# ── Telegram 401 alert ────────────────────────────────────────────────────────

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
    Returns {"_401": True} exclusively on HTTP 401.
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
    http:        httpx.Client,
    universe:    list[dict],
    baselines:   dict[str, dict],
    tracker:     RangeTracker,
    trade_date:  str,
    news_cache:  dict,
    alert_rules: list[dict],
) -> bool:
    """
    Execute one poll cycle.
    Appends per-poll history and updates OR range state via the tracker.
    Returns False if a 401 was detected (caller should back off and rebuild HTTP client).
    """
    fraction = _session_fraction()
    now_ist  = datetime.now(IST)

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

    radar_rows: list[dict] = []
    missing = 0

    for ikey, (symbol, membership) in key_to_meta.items():
        # Upstox /market-quote/quotes is currently keyed by exchange:tradingsymbol
        # (e.g. NSE_EQ:JINDALSTEL); older v2 responses keyed by the instrument_key.
        # Try the live format first, fall back to instrument_key so the resolver
        # survives either shape. Exchange is parsed from the instrument_key (segment
        # before '|') so BSE/other segments resolve without hardcoding NSE_EQ.
        exchange = ikey.split("|", 1)[0]
        entry = quote_data.get(f"{exchange}:{symbol}") or quote_data.get(ikey)
        if not entry:
            missing += 1
            continue
        baseline = baselines.get(symbol)
        row = _compute_row(symbol, entry, baseline, fraction, membership)

        # D: News / catalyst fusion
        news = news_cache.get(symbol, {})
        row["has_news"]       = bool(news.get("catalyst_line") or news.get("headline_count"))
        row["catalyst_line"]  = news.get("catalyst_line")
        row["headline_count"] = news.get("headline_count", 0)

        radar_rows.append(row)

    if missing:
        logger.debug(f"radar_poller: {missing} instruments missing from quote response")

    # B: per-poll intraday history, then OR range materialisation + break tracking.
    # History first so a window materialised this cycle includes the current poll.
    tracker.append_poll_history(radar_rows, now_ist)
    tracker.update(radar_rows, baselines, now_ist)

    # Day context the 15:35 ORB EOD job needs after the snapshot TTL has lapsed
    day_meta = {
        r["symbol"]: {
            "gap_pct":    r.get("gap_pct"),
            "has_news":   r.get("has_news", False),
            "ltp":        r.get("ltp"),
            "prev_close": r.get("prev_close"),
        }
        for r in radar_rows
    }
    _cache.set_ex(f"{_DAY_META_KEY_PREFIX}{trade_date}", day_meta, _DAY_META_TTL)

    # All-symbols-missing is a failure even though Redis accepts the empty snapshot:
    # the quote response keys matched no instrument_key, so /radar renders 0 of 0.
    # Surface it loudly and mark status degraded so the dashboard/Telegram can react.
    total_symbols = len(key_to_meta)
    all_missing   = total_symbols > 0 and missing == total_symbols
    if all_missing:
        logger.error(
            f"radar_poller: ALL {total_symbols} symbols unresolved from quote response "
            f"— 0 rows written; likely a quote key-format mismatch at the :410 lookup"
        )

    # One timestamp shared by the live snapshot, the persisted frame, AND any events
    # emitted this cycle — so an event's ts is byte-identical to the radar_snapshots
    # row the worker writes, which the labeler relies on for entry-price alignment.
    generated_at = datetime.now(IST).isoformat()
    snapshot = {
        "generated_at": generated_at,
        "rows":         radar_rows,
    }
    if not _cache.set_ex(_SNAPSHOT_KEY, snapshot, _SNAPSHOT_TTL):
        logger.error(
            f"radar_poller: FAILED to write {_SNAPSHOT_KEY} "
            f"({len(radar_rows)} rows) — Redis write returned False; /radar will be empty"
        )

    # Edge-page data spine (Phase 0): publish the full frame to the radar:frames
    # stream for the out-of-process persist worker. Fire-and-forget — xadd_frame
    # swallows and logs any error so a transport failure can never delay or break
    # this poll loop. The poller has ZERO Postgres dependency for persistence; the
    # live snapshot above is the only sink the /radar UI reads.
    _cache.xadd_frame(_FRAMES_STREAM, snapshot)
    status = (
        {"state": "degraded", "reason": "all_symbols_missing"}
        if all_missing else {"state": "ok"}
    )
    _cache.set_ex(_STATUS_KEY, status, _SNAPSHOT_TTL)

    frozen = sum(1 for r in tracker.ranges.values() if r["materialized"] is not None)
    logger.info(
        f"radar_poller: snapshot written — {len(radar_rows)} rows, "
        f"session_fraction={fraction:.2f}, missing={missing}, "
        f"ranges={len(tracker.ranges)} ({frozen} frozen)"
    )

    # C: Telegram alerts (after snapshot is written so stale=False)
    process_alerts(radar_rows, alert_rules, trade_date, market_open=True, stale=False)

    # C2: Phase 1 event emission — same enriched rows, separate path from alert
    # throttling. First-crossing only (own Redis dedup keyspace); each event is one
    # fire-and-forget XADD to radar:events. No synchronous DB write here, so Phase 0
    # isolation is preserved exactly as for frames.
    process_events(radar_rows, alert_rules, trade_date, generated_at)

    return True


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logger(settings.log_level)
    logger.info("radar_poller: starting up")

    # Redis is the only sink for the snapshot the /radar page reads. If the client
    # failed to connect at import (e.g. wrong REDIS_HOST on the host), set_ex would
    # silently no-op and we'd loop forever logging "snapshot written" while writing
    # nothing. Fail fast instead — mirror the universe guard below.
    if _cache._CLIENT is None:
        logger.error(
            "radar_poller: Redis unavailable — snapshots cannot be written; "
            "check REDIS_HOST and that premarket-redis-1 is reachable; exiting"
        )
        sys.exit(1)

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
        f"radar_poller: {len(baselines)} baselines for {trade_date}"
        + (" (WARNING: 0 baselines — run radar_baselines.run_baselines() first)"
           if not baselines else "")
    )

    # B: OR range tracker — loads defs from DB and any state frozen earlier today
    tracker = RangeTracker(trade_date)
    tracker.set_defs(load_range_defs(trade_date))
    defs_loaded_at = time.monotonic()
    logger.info(f"radar_poller: tracking {len(tracker.ranges)} OR ranges for {trade_date}")

    # D: news/catalyst cache
    news_cache     = _load_news_cache(trade_date)
    news_loaded_at = time.monotonic()

    # C: alert rules (static — loaded once at startup)
    alert_rules = load_rules()
    logger.info(f"radar_poller: {len(alert_rules)} alert rules loaded")

    while True:
        if not _is_market_open():
            logger.debug("radar_poller: outside market hours — sleeping 60s")
            time.sleep(60)
            new_date = date.today().isoformat()
            if new_date != trade_date:
                trade_date     = new_date
                baselines      = _load_baselines(trade_date)
                tracker        = RangeTracker(trade_date)
                tracker.set_defs(load_range_defs(trade_date))
                defs_loaded_at = time.monotonic()
                news_cache     = _load_news_cache(trade_date)
                news_loaded_at = time.monotonic()
                logger.info(
                    f"radar_poller: new trade date {trade_date}, "
                    f"reloaded baselines ({len(baselines)} rows)"
                )
            continue

        # D: hourly news cache refresh
        if time.monotonic() - news_loaded_at > _NEWS_REFRESH_SECS:
            # Invalidate Redis key so _load_news_cache fetches fresh from DB
            _cache.set_ex(f"{_NEWS_KEY_PREFIX}{trade_date}", {}, 1)
            news_cache     = _load_news_cache(trade_date)
            news_loaded_at = time.monotonic()
            logger.debug("radar_poller: news cache refreshed")

        # B: 5-min OR range def refresh (picks up ranges added via the API)
        if time.monotonic() - defs_loaded_at > _DEFS_REFRESH_SECS:
            tracker.set_defs(load_range_defs(trade_date))
            defs_loaded_at = time.monotonic()

        ok = _poll_cycle(http, universe, baselines, tracker, trade_date, news_cache, alert_rules)
        if not ok:
            logger.warning(f"radar_poller: backing off {_BACKOFF_INTERVAL}s after 401")
            time.sleep(_BACKOFF_INTERVAL)
            http = _build_http()
        else:
            time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    main()
