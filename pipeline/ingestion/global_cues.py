import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yfinance as yf
from loguru import logger

TICKERS: dict[str, str] = {
    "dow_futures":    "YM=F",
    "nasdaq_futures": "NQ=F",
    "sp500_futures":  "ES=F",
    "nikkei":         "^N225",
    "hangseng":       "^HSI",
    "sgx_nifty":      "^NSEI",    # proxy -- live Nifty, not SGX futures
    "crude_oil":      "CL=F",
    "gold":           "GC=F",
    "usd_inr":        "INR=X",
    "vix_india":      "^NSEBANK",  # Bank Nifty as India volatility proxy
}

_DIRECTION_THRESHOLD = 0.2   # percent -- below this magnitude = neutral
_FETCH_PERIOD = "5d"         # wide enough to guarantee 2 complete trading days
_FETCH_INTERVAL = "1d"


# ── helpers ───────────────────────────────────────────────────────────────────

def _direction(change_pct: float) -> str:
    if change_pct > _DIRECTION_THRESHOLD:
        return "bullish"
    if change_pct < -_DIRECTION_THRESHOLD:
        return "bearish"
    return "neutral"


def _safe_float(value: Any) -> float | None:
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# ── per-ticker fetch ──────────────────────────────────────────────────────────

def _fetch_ticker(name: str, symbol: str) -> dict[str, Any] | None:
    try:
        df = yf.Ticker(symbol).history(period=_FETCH_PERIOD, interval=_FETCH_INTERVAL)
    except Exception as exc:
        logger.error(f"{name} ({symbol}): yfinance error: {exc}")
        return None

    if df is None or len(df) < 2:
        logger.warning(
            f"{name} ({symbol}): insufficient data "
            f"(got {0 if df is None else len(df)} rows)"
        )
        return None

    prev_close = _safe_float(df["Close"].iloc[-2])
    latest = _safe_float(df["Close"].iloc[-1])

    if prev_close is None or latest is None or prev_close == 0:
        logger.warning(
            f"{name} ({symbol}): invalid price data "
            f"(prev={prev_close}, last={latest})"
        )
        return None

    change_pct = (latest - prev_close) / prev_close * 100
    direction = _direction(change_pct)

    result: dict[str, Any] = {
        "name": name,
        "price": round(latest, 4),
        "change_pct": round(change_pct, 4),
        "direction": direction,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.debug(
        f"{name}: {result['price']}  {result['change_pct']:+.2f}%  [{direction}]"
    )
    return result


# ── public API ────────────────────────────────────────────────────────────────

def fetch_global_cues() -> list[dict[str, Any]]:
    """
    Fetch OHLCV for each global market ticker and return directional signals.
    Each ticker is isolated -- one failure does not stop others.
    """
    results: list[dict[str, Any]] = []
    for name, symbol in TICKERS.items():
        cue = _fetch_ticker(name, symbol)
        if cue is not None:
            results.append(cue)
    logger.info(f"Global cues: {len(results)}/{len(TICKERS)} tickers fetched")
    return results


def bias_summary(cues: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Aggregate directional signals into a single market bias.

    Returns:
        {
            "global_bias":   "bearish",
            "bullish_count": 3,
            "bearish_count": 6,
            "neutral_count": 1,
            "key_movers":    [...],   # top 3 by abs(change_pct)
        }
    """
    if not cues:
        return {
            "global_bias": "neutral",
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "key_movers": [],
        }

    bullish = [c for c in cues if c["direction"] == "bullish"]
    bearish = [c for c in cues if c["direction"] == "bearish"]
    neutral = [c for c in cues if c["direction"] == "neutral"]

    if len(bullish) > len(bearish):
        global_bias = "bullish"
    elif len(bearish) > len(bullish):
        global_bias = "bearish"
    else:
        global_bias = "neutral"

    key_movers = sorted(cues, key=lambda c: abs(c["change_pct"]), reverse=True)[:3]

    return {
        "global_bias": global_bias,
        "bullish_count": len(bullish),
        "bearish_count": len(bearish),
        "neutral_count": len(neutral),
        "key_movers": key_movers,
    }


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    cues = fetch_global_cues()

    print(f"\n{'- ' * 35}")
    print(f"  Global Cues  ({len(cues)} tickers)")
    print(f"{'- ' * 35}")
    for c in cues:
        arrow = "^" if c["direction"] == "bullish" else ("v" if c["direction"] == "bearish" else "~")
        print(
            f"  {arrow} {c['name']:18}  {c['price']:>12,.2f}"
            f"  {c['change_pct']:>+7.2f}%  [{c['direction']}]"
        )

    summary = bias_summary(cues)
    print(f"\n{'- ' * 35}")
    print(f"  Global Bias : {summary['global_bias'].upper()}")
    print(f"  Bullish     : {summary['bullish_count']}")
    print(f"  Bearish     : {summary['bearish_count']}")
    print(f"  Neutral     : {summary['neutral_count']}")
    print(f"\n  Key Movers:")
    for m in summary["key_movers"]:
        print(f"    {m['name']:18}  {m['change_pct']:>+.2f}%")
