"""
Market snapshot fetcher: Indian indices, global indices, commodities, FX.
Powers the /api/market-snapshot endpoint (60 s Redis cache) and briefing header.

All tickers sourced via yfinance — no paid keys required.
Each instrument is fetched independently; failures produce a stale tile
(last=None, change_pct=None, stale=True) so callers never break on partial outages.

Interface:
    fetch_snapshot() -> list[SnapshotTile]

SnapshotTile fields:
    label       str         display name  e.g. "NIFTY 50"
    category    str         "index" | "commodity" | "fx"
    last        float|None  latest close
    prev_close  float|None  previous session close
    change_pct  float|None  (last-prev)/prev * 100
    asof        str         ISO-8601 UTC timestamp of the fetch
    stale       bool        True when data could not be fetched
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import yfinance as yf
from loguru import logger

# ── instrument registry ───────────────────────────────────────────────────────
# Order determines left-to-right display in the header bar.
# GIFT Nifty futures are not available on yfinance — ^NSEI (spot Nifty) is used
# as the closest proxy for pre-open gap read.
# BZ=F (Brent) may occasionally fall back to CL=F (WTI) if data is thin.

_INSTRUMENTS: list[dict[str, str]] = [
    # Indian indices
    {"key": "NIFTY 50",   "ticker": "^NSEI",    "category": "index"},
    {"key": "SENSEX",     "ticker": "^BSESN",   "category": "index"},
    {"key": "BANK NIFTY", "ticker": "^NSEBANK",  "category": "index"},
    {"key": "GIFT Nifty", "ticker": "^NSEI",    "category": "index"},
    # Global indices / futures
    {"key": "DAX",        "ticker": "^GDAXI",   "category": "index"},
    {"key": "S&P Fut",    "ticker": "ES=F",     "category": "index"},
    # Commodities
    {"key": "Brent",      "ticker": "BZ=F",     "category": "commodity"},
    {"key": "Gold",       "ticker": "GC=F",     "category": "commodity"},
    {"key": "Silver",     "ticker": "SI=F",     "category": "commodity"},
    {"key": "Copper",     "ticker": "HG=F",     "category": "commodity"},
    # FX
    {"key": "USD/INR",    "ticker": "INR=X",    "category": "fx"},
]

_FETCH_PERIOD   = "5d"   # wide enough to guarantee ≥2 complete trading sessions
_FETCH_INTERVAL = "1d"


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _fetch_one(instr: dict[str, str]) -> dict[str, Any]:
    key    = instr["key"]
    ticker = instr["ticker"]
    asof   = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {
        "label":      key,
        "ticker":     ticker,
        "category":   instr["category"],
        "last":       None,
        "prev_close": None,
        "change_pct": None,
        "asof":       asof,
        "stale":      True,
    }
    try:
        df = yf.Ticker(ticker).history(period=_FETCH_PERIOD, interval=_FETCH_INTERVAL)
        if df is None or len(df) < 2:
            logger.warning(
                f"MarketSnapshot: {key} ({ticker}): insufficient rows "
                f"({0 if df is None else len(df)}) — stale tile"
            )
            return base

        prev = _safe_float(df["Close"].iloc[-2])
        last = _safe_float(df["Close"].iloc[-1])

        if prev is None or last is None or prev == 0:
            logger.warning(
                f"MarketSnapshot: {key} ({ticker}): bad prices "
                f"prev={prev} last={last} — stale tile"
            )
            return base

        change_pct = (last - prev) / prev * 100
        logger.debug(
            f"  {key:12}  last={last:,.2f}  chg={change_pct:+.2f}%"
        )
        return {
            **base,
            "last":       round(last, 4),
            "prev_close": round(prev, 4),
            "change_pct": round(change_pct, 4),
            "stale":      False,
        }

    except Exception as exc:
        logger.warning(f"MarketSnapshot: {key} ({ticker}) error: {exc} — stale tile")
        return base


# ── public API ────────────────────────────────────────────────────────────────

def fetch_snapshot() -> list[dict[str, Any]]:
    """
    Fetch all market snapshot tiles.
    Each ticker is isolated — one failure does not affect others.
    Always returns a list of len(_INSTRUMENTS) dicts (stale=True for failures).
    """
    tiles = [_fetch_one(instr) for instr in _INSTRUMENTS]
    ok = sum(1 for t in tiles if not t["stale"])
    logger.info(f"MarketSnapshot: {ok}/{len(tiles)} tiles fetched OK")
    return tiles


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    tiles = fetch_snapshot()
    print(f"\n{'- ' * 35}")
    print(f"  Market Snapshot  ({len(tiles)} tiles)")
    print(f"{'- ' * 35}")
    for t in tiles:
        if t["stale"]:
            print(f"  [STALE] {t['label']:14}  —")
        else:
            arrow = "^" if (t["change_pct"] or 0) >= 0 else "v"
            print(
                f"  {arrow} {t['label']:14}  {t['last']:>12,.2f}"
                f"  {t['change_pct']:>+7.2f}%  [{t['category']}]"
            )
