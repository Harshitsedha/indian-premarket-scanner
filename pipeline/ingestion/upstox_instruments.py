"""
upstox_instruments.py — NSE instrument master cache.

Public API:
    get_instrument_token(symbol) -> "NSE_EQ|<int>" or None
    refresh_instrument_master()  -> re-downloads CSV and rebuilds cache

The CSV is downloaded from Upstox on first use if missing or >7 days old.
Cache is a plain dict held in module memory; rebuilt after every refresh.
"""

import gzip
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd
from loguru import logger

_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
# Resolve relative to project root (parents[2] of pipeline/ingestion/this_file.py)
_CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "instruments" / "NSE_instruments.csv"
_STALE_AFTER = timedelta(days=7)

_cache: dict[str, str] | None = None


# ── Download ─────────────────────────────────────────────────────────────────

def _download() -> None:
    _CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading NSE instrument master from {_URL}")
    resp = httpx.get(_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    _CSV_PATH.write_bytes(raw)
    logger.info(f"Saved instrument master: {len(raw):,} bytes -> {_CSV_PATH}")


# ── Parse & cache ─────────────────────────────────────────────────────────────

def _build_cache() -> dict[str, str]:
    df = pd.read_csv(_CSV_PATH, low_memory=False)
    # Upstox v2 CSV uses instrument_type="EQUITY" and provides instrument_key directly
    nse_eq = df[(df["exchange"] == "NSE_EQ") & (df["instrument_type"] == "EQUITY")]
    # instrument_key column is already the correct format for v2 API calls (e.g. "NSE_EQ|INE002A01018")
    cache = dict(zip(nse_eq["tradingsymbol"], nse_eq["instrument_key"]))
    logger.info(f"Instrument cache built: {len(cache):,} NSE_EQ equities")
    return cache


def _is_stale() -> bool:
    if not _CSV_PATH.exists():
        return True
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        _CSV_PATH.stat().st_mtime, tz=timezone.utc
    )
    return age > _STALE_AFTER


def _ensure_loaded() -> None:
    global _cache
    if _cache is not None:
        return
    if _is_stale():
        _download()
    _cache = _build_cache()


# ── Public API ────────────────────────────────────────────────────────────────

def get_instrument_token(symbol: str) -> str | None:
    """Return the NSE_EQ instrument key (e.g. 'NSE_EQ|2885') or None."""
    _ensure_loaded()
    return _cache.get(symbol.upper())


def refresh_instrument_master() -> None:
    """Force-download the CSV and rebuild the in-memory cache. Called by scheduler."""
    global _cache
    _download()
    _cache = _build_cache()
