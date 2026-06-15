"""
Fetch and cache historical candles from Upstox V3.

Public API:
    df = get_candles(instrument_key, interval, start, end, symbol="")

The V3 endpoint allows at most ~31 calendar days per call for minutes/1.
We chunk in 28-day windows (safe margin), fetch newest-to-oldest, stitch,
then sort ascending by timestamp.

Cache: pipeline/backtest/cache/{symbol}_{interval}_{start}_{end}.parquet
A cache hit skips the API entirely.
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from loguru import logger

# Allow importing pipeline packages when this file is run directly.
_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from ingestion.upstox_client import _get_bearer_token  # reuse DB-backed auth
from backtest.exceptions import _JobCancelled

_BASE_V3    = "https://api.upstox.com/v3"
_CACHE_DIR  = Path(__file__).parent / "cache"
_CHUNK_DAYS = 28   # V3 hard limit for minutes/1 is ~31 calendar days; 28 is safe

_EMPTY_COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def _encode_key(key: str) -> str:
    return key.replace("|", "%7C")


# ── Corporate-action guard ────────────────────────────────────────────────────

def scan_ca_jumps(
    df:          pd.DataFrame,
    symbol:      str   = "",
    ca_jump_pct: float = 20.0,
) -> list[dict]:
    """
    Scan session-to-session close→open transitions for potential corporate-action
    events (stock splits, bonus issues, rights issues, etc.).

    A real intraday gap and a split look identical at the candle level; only the
    magnitude distinguishes them.  This function surfaces anything above the
    threshold so the caller can adjudicate.

    Args:
        df:          chronologically-sorted candle DataFrame from get_candles()
        symbol:      used only for log messages
        ca_jump_pct: flag transitions whose absolute jump exceeds this percent
                     (default 20.0 — large-cap NSE stocks rarely gap this much
                     on news alone)

    Returns:
        List of dicts, one per flagged transition:
          {symbol, date, prev_close, curr_open, jump_pct}
        Empty list if no jumps found or df is empty.
        The DataFrame is never modified.
    """
    if df.empty:
        return []

    dates  = sorted(df["date"].unique())
    flagged: list[dict] = []

    for i in range(1, len(dates)):
        prev_last  = df[df["date"] == dates[i - 1]].iloc[-1]
        curr_first = df[df["date"] == dates[i]].iloc[0]
        prev_close = float(prev_last["close"])
        curr_open  = float(curr_first["open"])

        if prev_close <= 0:
            continue

        jump_pct = (curr_open - prev_close) / prev_close * 100
        if abs(jump_pct) > ca_jump_pct:
            date_str = pd.Timestamp(curr_first["timestamp"]).strftime("%Y-%m-%d")
            logger.warning(
                f"Possible corporate-action jump: {symbol}  {date_str}  "
                f"prev_close={prev_close:.2f}  open={curr_open:.2f}  "
                f"jump={jump_pct:+.1f}%  (threshold={ca_jump_pct:.0f}%)"
            )
            flagged.append({
                "symbol":     symbol,
                "date":       date_str,
                "prev_close": prev_close,
                "curr_open":  curr_open,
                "jump_pct":   round(jump_pct, 2),
            })

    return flagged


def _fetch_chunk(
    http: httpx.Client,
    instrument_key: str,
    unit: str,
    interval: str,
    from_date: date,
    to_date: date,
) -> list[list]:
    """Single V3 API call. Returns raw candle list (newest-first) or [] on error."""
    url = (
        f"{_BASE_V3}/historical-candle/{_encode_key(instrument_key)}"
        f"/{unit}/{interval}/{to_date}/{from_date}"
    )
    try:
        resp = http.get(url)
        if resp.status_code == 429:
            logger.warning("V3 rate limit 429 — retrying in 2s")
            time.sleep(2)
            resp = http.get(url)
        if not resp.is_success:
            logger.error(
                f"V3 candle {from_date}–{to_date}: HTTP {resp.status_code} "
                f"{resp.text[:200]}"
            )
            return []
        return resp.json().get("data", {}).get("candles", [])
    except Exception as exc:
        logger.error(f"V3 candle fetch error {from_date}–{to_date}: {exc}")
        return []


def get_candles(
    instrument_key: str,
    interval: str,
    start: date,
    end: date,
    symbol: str = "",
    cancel_event=None,          # threading.Event | None; omit outside worker context
) -> pd.DataFrame:
    """
    Return a chronologically-sorted DataFrame of candles.

    Columns: timestamp (tz-aware), open, high, low, close, volume, oi, date (midnight ts)

    Args:
        instrument_key: e.g. "NSE_EQ|INE002A01018"
        interval:       "minutes/1", "minutes/5", "day/1", etc.
        start / end:    date range (inclusive)
        symbol:         used only for cache file naming; derived if omitted

    Returns an empty DataFrame (correct columns) if no data is found — never raises.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    sym_slug  = symbol.upper() if symbol else instrument_key.replace("|", "_")
    iv_slug   = interval.replace("/", "_")
    cache_file = _CACHE_DIR / f"{sym_slug}_{iv_slug}_{start}_{end}.parquet"

    if cache_file.exists():
        logger.info(f"Cache hit: {cache_file.name}")
        df = pd.read_parquet(cache_file)
        df["date"] = df["timestamp"].dt.normalize()
        return df

    parts = interval.split("/")
    if len(parts) != 2:
        raise ValueError(f"interval must be '<unit>/<n>', e.g. 'minutes/1', got: {interval!r}")
    unit, n = parts[0], parts[1]

    token = _get_bearer_token()
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    all_rows: list[list] = []
    chunk_end = end

    with httpx.Client(headers=headers, timeout=30) as http:
        while chunk_end >= start:
            if cancel_event is not None and cancel_event.is_set():
                raise _JobCancelled(
                    f"candle fetch cancelled ({symbol or instrument_key})"
                )
            chunk_start = max(start, chunk_end - timedelta(days=_CHUNK_DAYS))
            logger.info(
                f"Fetching {symbol or instrument_key} {unit}/{n}: "
                f"{chunk_start} to {chunk_end}"
            )
            rows = _fetch_chunk(http, instrument_key, unit, n, chunk_start, chunk_end)
            if rows:
                all_rows.extend(rows)
            else:
                logger.warning(f"No data returned for {instrument_key} {chunk_start}–{chunk_end}")
            chunk_end = chunk_start - timedelta(days=1)

    if not all_rows:
        logger.warning(
            f"get_candles: zero candles for {instrument_key} {start}–{end} — "
            "returning empty DataFrame"
        )
        return pd.DataFrame(columns=_EMPTY_COLS + ["date"])

    df = pd.DataFrame(all_rows, columns=_EMPTY_COLS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = (
        df.drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
    df["oi"]     = pd.to_numeric(df["oi"], errors="coerce").fillna(0.0)

    # Persist raw columns only; 'date' is cheap to recompute and avoids parquet
    # date32/object ambiguity across pandas versions.
    df.to_parquet(cache_file, index=False)
    logger.info(f"Cached {len(df):,} candles → {cache_file.name}")

    df["date"] = df["timestamp"].dt.normalize()
    return df
