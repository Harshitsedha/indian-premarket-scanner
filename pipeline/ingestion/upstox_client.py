"""
upstox_client.py — Upstox v2 API wrapper.

Public interface:
    client = UpstoxClient()
    client.get_prev_close("RELIANCE")     -> dict | None
    client.get_premarket_quote("RELIANCE") -> dict | None
    client.get_bulk_quotes(["RELIANCE", "INFY"]) -> list[dict]
    client.get_ohlcv_history("RELIANCE", days=20) -> list[dict]

All methods return None / [] on any error — never raise.
"""

import time
import psycopg2
from datetime import date, timedelta

import httpx
from loguru import logger

from utils.config import settings
from ingestion.upstox_instruments import get_instrument_token

_BASE = "https://api.upstox.com/v2"

# Sent at most once per process lifetime to avoid Telegram spam on repeated 401s
_401_alerted: bool = False


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_bearer_token() -> str:
    """Read extended_token from DB (highest id row). Fall back to env var."""
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT extended_token FROM upstox_tokens ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        logger.warning(f"DB token read failed, falling back to env: {e}")
    return settings.upstox_extended_token


def _send_401_alert() -> None:
    global _401_alerted
    if _401_alerted:
        return
    _401_alerted = True
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": "🔴 Upstox token expired or invalid — re-run upstox_auth.py",
            },
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Failed to send 401 Telegram alert: {e}")


# ── UpstoxClient ──────────────────────────────────────────────────────────────

class UpstoxClient:
    def __init__(self) -> None:
        token = _get_bearer_token()
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=15,
        )

    # ── Internal request handler ──────────────────────────────────────────────

    def _get(self, url: str) -> dict | None:
        """GET with 401/429 handling. Returns parsed JSON body or None."""
        try:
            resp = self._http.get(url)
        except Exception as e:
            logger.error(f"Upstox request failed: {e}")
            return None

        if resp.status_code == 401:
            logger.error("Upstox token expired or invalid")
            _send_401_alert()
            return None

        if resp.status_code == 429:
            logger.warning("Upstox 429 rate limit — retrying in 2s")
            time.sleep(2)
            try:
                resp = self._http.get(url)
            except Exception as e:
                logger.error(f"Upstox retry after 429 failed: {e}")
                return None

        if not resp.is_success:
            logger.error(f"Upstox API {resp.status_code}: {resp.text[:300]}")
            return None

        return resp.json()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_key(instrument_key: str) -> str:
        """URL-encode the pipe separator required by Upstox v2 endpoints."""
        return instrument_key.replace("|", "%7C")

    @staticmethod
    def _parse_candle(symbol: str, candle: list) -> dict:
        """[timestamp, open, high, low, close, volume, oi] → named dict."""
        return {
            "symbol": symbol.upper(),
            "open":   float(candle[1]),
            "high":   float(candle[2]),
            "low":    float(candle[3]),
            "close":  float(candle[4]),
            "volume": int(candle[5]),
            "date":   str(candle[0])[:10],   # "2025-05-30T00:00:00+05:30" → "2025-05-30"
        }

    # ── Public methods ────────────────────────────────────────────────────────

    def get_prev_close(self, symbol: str) -> dict | None:
        """Previous trading day OHLCV. Returns None if symbol unknown or API error."""
        key = get_instrument_token(symbol)
        if not key:
            logger.warning(f"Symbol not in instrument master: {symbol}")
            return None

        today      = date.today()
        to_date    = today.strftime("%Y-%m-%d")
        from_date  = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        url = f"{_BASE}/historical-candle/{self._encode_key(key)}/day/{to_date}/{from_date}"

        body = self._get(url)
        if not body:
            return None

        candles = body.get("data", {}).get("candles", [])
        if not candles:
            logger.warning(f"No candles in response for {symbol}")
            return None

        return self._parse_candle(symbol, candles[0])

    def get_premarket_quote(self, symbol: str) -> dict | None:
        """Live LTP with gap_pct vs prev close. Falls back gracefully when market is closed."""
        key = get_instrument_token(symbol)
        if not key:
            return None

        prev = self.get_prev_close(symbol)
        if not prev:
            return None
        time.sleep(0.05)

        url  = f"{_BASE}/market-quote/ltp?instrument_key={self._encode_key(key)}"
        body = self._get(url)

        # Market closed / no data — return prev_close as ltp
        if not body:
            return {
                "symbol":       symbol.upper(),
                "ltp":          prev["close"],
                "prev_close":   prev["close"],
                "gap_pct":      0.0,
                "is_premarket": False,
            }

        quote_data = body.get("data", {})
        # Response may be keyed by original (pipe) key or encoded key — try both
        entry = quote_data.get(key) or next(iter(quote_data.values()), None)

        if not entry or entry.get("last_price") is None:
            return {
                "symbol":       symbol.upper(),
                "ltp":          prev["close"],
                "prev_close":   prev["close"],
                "gap_pct":      0.0,
                "is_premarket": False,
            }

        ltp     = float(entry["last_price"])
        gap_pct = round((ltp - prev["close"]) / prev["close"] * 100, 2)
        return {
            "symbol":       symbol.upper(),
            "ltp":          ltp,
            "prev_close":   prev["close"],
            "gap_pct":      gap_pct,
            "is_premarket": True,
        }

    def get_bulk_quotes(self, symbols: list[str]) -> list[dict]:
        """
        Batch LTP for up to 500 symbols. Fetches prev_closes individually
        (0.05s sleep between), then one bulk LTP call.
        """
        # Build prev_close lookup — sequential with rate-limit sleep
        prev_closes: dict[str, dict] = {}
        for sym in symbols:
            result = self.get_prev_close(sym)
            if result:
                prev_closes[sym.upper()] = result
            time.sleep(0.05)

        # Collect valid instrument keys
        key_to_symbol: dict[str, str] = {}   # instrument_key → UPPER symbol
        for sym in symbols:
            k = get_instrument_token(sym)
            if k:
                key_to_symbol[k] = sym.upper()

        if not key_to_symbol:
            return []

        # Comma-separated, both pipe and comma encoded
        encoded_keys = "%2C".join(self._encode_key(k) for k in key_to_symbol)
        url  = f"{_BASE}/market-quote/ltp?instrument_key={encoded_keys}"
        body = self._get(url)

        quote_data = body.get("data", {}) if body else {}

        results: list[dict] = []
        for ikey, sym in key_to_symbol.items():
            prev = prev_closes.get(sym)
            if not prev:
                continue

            entry = quote_data.get(ikey)
            if not entry or entry.get("last_price") is None:
                results.append({
                    "symbol":       sym,
                    "ltp":          prev["close"],
                    "prev_close":   prev["close"],
                    "gap_pct":      0.0,
                    "is_premarket": False,
                })
            else:
                ltp     = float(entry["last_price"])
                gap_pct = round((ltp - prev["close"]) / prev["close"] * 100, 2)
                results.append({
                    "symbol":       sym,
                    "ltp":          ltp,
                    "prev_close":   prev["close"],
                    "gap_pct":      gap_pct,
                    "is_premarket": True,
                })

        return results

    def get_ohlcv_history(self, symbol: str, days: int = 20) -> list[dict]:
        """
        Last N trading days OHLCV sorted oldest → newest.
        Adds 10 extra calendar days to the lookback to cover weekends and holidays.
        """
        key = get_instrument_token(symbol)
        if not key:
            return []

        today     = date.today()
        to_date   = today.strftime("%Y-%m-%d")
        from_date = (today - timedelta(days=days + 10)).strftime("%Y-%m-%d")
        url = f"{_BASE}/historical-candle/{self._encode_key(key)}/day/{to_date}/{from_date}"

        body = self._get(url)
        if not body:
            return []

        candles = body.get("data", {}).get("candles", [])
        rows = [
            {
                "date":   str(c[0])[:10],
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": int(c[5]),
            }
            for c in candles
        ]
        return list(reversed(rows))   # candles arrive newest-first; return oldest-first


# ── module-level helpers ──────────────────────────────────────────────────────

def fetch_upstox_trading_date(
    probe_symbols: list[str] | None = None,
) -> str | None:
    """
    Return the most recent trading date (ISO string, e.g. "2026-06-01") by
    pulling the latest daily candle for a set of large-cap probe symbols.

    Tries each symbol in order until one succeeds.  Returns None only if all
    probes fail (token missing, network error, etc.).
    """
    if probe_symbols is None:
        probe_symbols = ["RELIANCE", "INFY", "TCS"]
    client = UpstoxClient()
    for symbol in probe_symbols:
        try:
            result = client.get_prev_close(symbol)
            if result and result.get("date"):
                logger.info(f"Upstox trading date: {result['date']} (via {symbol})")
                return result["date"]
        except Exception as exc:
            logger.warning(f"fetch_upstox_trading_date: {symbol} probe failed: {exc}")
    logger.warning("fetch_upstox_trading_date: all probes failed")
    return None
