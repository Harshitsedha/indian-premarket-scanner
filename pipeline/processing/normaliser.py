"""
Pure data-transformation layer.
Takes raw output from all three ingestion modules and produces one clean,
validated dict ready for claude_client.analyse_news() and the renderer.
No API calls, no I/O — side-effect free.
"""

import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

_MIN_HEADLINE_LEN = 25
_MAX_HEADLINES = 25
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)   # sentinel for missing dates


# ── helpers ───────────────────────────────────────────────────────────────────

def _norm_text(text: str) -> str:
    """Lowercase + strip punctuation — used as dedup key only, not stored."""
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", text).strip()


def _parse_dt(value: Any) -> datetime:
    """Parse ISO datetime string; returns _EPOCH on any failure."""
    if not value:
        return _EPOCH
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _EPOCH


def _strip_strings(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v.strip() if isinstance(v, str) else v for k, v in d.items()}


# ── headline pipeline ─────────────────────────────────────────────────────────

def _clean_headlines(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    count_raw = len(raw)

    # 1. Strip whitespace on every string field
    items = [_strip_strings(h) for h in raw]

    # 2. Sort most-recent first so dedup naturally keeps the freshest copy
    items.sort(key=lambda h: _parse_dt(h.get("scraped_at")), reverse=True)

    # 3. Drop headlines shorter than minimum length
    items = [h for h in items if len(h.get("headline", "")) >= _MIN_HEADLINE_LEN]
    logger.debug(f"Headlines: raw={count_raw} -> after_length_filter={len(items)}")

    # 4. Deduplicate by normalised text (keeps most-recent because list is sorted)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for h in items:
        key = _norm_text(h.get("headline", ""))
        if key and key not in seen:
            seen.add(key)
            deduped.append(h)
    logger.debug(f"Headlines: after_dedup={len(deduped)}")

    # 5. Cap and assign sequential ids
    final = deduped[:_MAX_HEADLINES]
    for idx, h in enumerate(final, 1):
        h["id"] = idx

    logger.info(
        f"Headlines: {count_raw} raw -> {len(final)} final "
        f"(length_filter, dedup, cap={_MAX_HEADLINES})"
    )
    return final


# ── global cues validation ────────────────────────────────────────────────────

def _clean_global_cues(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cue in raw:
        price = cue.get("price")
        if not price:                 # drops None and 0
            logger.debug(f"Cue dropped: {cue.get('name')} (price={price})")
            continue
        clean = dict(cue)
        try:
            clean["change_pct"] = float(clean.get("change_pct") or 0.0)
        except (TypeError, ValueError):
            clean["change_pct"] = 0.0
        result.append(clean)
    logger.info(f"Global cues: {len(raw)} raw -> {len(result)} valid")
    return result


# ── FII / DII formatting ──────────────────────────────────────────────────────

def _fmt_crore(value: Any, label: str) -> str | None:
    if value is None:
        return None
    try:
        v = float(value)
        sign = "+" if v >= 0 else ""
        return f"{label}: {sign}{int(round(v))} cr"
    except (TypeError, ValueError):
        return None


def _format_fiidii(fii_dii: dict[str, Any] | None) -> str:
    if not fii_dii:
        return "FII/DII: unavailable"
    fii = _fmt_crore(fii_dii.get("fii_net"), "FII")
    dii = _fmt_crore(fii_dii.get("dii_net"), "DII")
    if fii is None and dii is None:
        return "FII/DII: unavailable"
    parts = [p for p in (fii, dii) if p is not None]
    return " | ".join(parts)


# ── public API ────────────────────────────────────────────────────────────────

def normalise(
    news: list[dict[str, Any]],
    global_cues: list[dict[str, Any]],
    nse_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Clean, validate, and merge outputs from the three ingestion modules.

    Args:
        news:        raw list from news_scraper.scrape_et_markets()
        global_cues: raw list from global_cues.fetch_global_cues()
        nse_data:    raw dict from nse_scraper.fetch_nse_data()

    Returns:
        {
            "headlines":       [...],   # cleaned, deduped, id-tagged
            "global_cues":     [...],   # price-validated
            "fii_dii_summary": "FII: -21105 cr | DII: +16764 cr",
            "trading_date":    "2024-01-15",
            "headline_count":  12,
            "normalised_at":   "2024-01-15T07:30:00+00:00",
        }
    """
    logger.info("Normaliser starting")

    headlines = _clean_headlines(list(news or []))
    cues = _clean_global_cues(list(global_cues or []))

    nse = nse_data or {}
    fii_dii_summary = _format_fiidii(nse.get("fii_dii"))
    trading_date = str(nse.get("trading_date") or "unknown")

    result: dict[str, Any] = {
        "headlines": headlines,
        "global_cues": cues,
        "fii_dii_summary": fii_dii_summary,
        "trading_date": trading_date,
        "headline_count": len(headlines),
        "normalised_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"Normaliser done: {len(headlines)} headlines, {len(cues)} cues, "
        f"fii_dii='{fii_dii_summary}', trading_date={trading_date}"
    )
    return result


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    # --- dummy news (includes edge cases: short headline, duplicate, whitespace) ---
    _NEWS = [
        {"headline": "  Nifty futures slip 200 points as US markets see sharp selloff overnight  ",
         "source": "Economic Times", "url": "https://et.com/1", "scraped_at": "2026-06-01T05:00:00+00:00"},
        {"headline": "Short",   # too short — should be dropped
         "source": "ET", "url": "https://et.com/2", "scraped_at": "2026-06-01T05:01:00+00:00"},
        {"headline": "Nifty futures slip 200 points as US markets see sharp selloff overnight",  # duplicate
         "source": "Mint", "url": "https://mint.com/1", "scraped_at": "2026-06-01T04:55:00+00:00"},
        {"headline": "RBI keeps repo rate unchanged at 6.5%; maintains withdrawal of accommodation stance",
         "source": "Mint", "url": "https://mint.com/2", "scraped_at": "2026-06-01T04:50:00+00:00"},
        {"headline": "Reliance Industries Q4 profit beats estimates, up 18% YoY on retail and Jio strength",
         "source": "Business Standard", "url": "https://bs.com/1", "scraped_at": "2026-06-01T04:45:00+00:00"},
        {"headline": "FII selling continues for fourth consecutive session; DII support limits losses",
         "source": "Economic Times", "url": "https://et.com/3", "scraped_at": "2026-06-01T04:40:00+00:00"},
        {"headline": "Crude oil surges 3% on Middle East tensions; analysts flag inflation risk",
         "source": "Mint", "url": "https://mint.com/3", "scraped_at": None},  # no date
    ]

    # --- dummy global cues (includes a bad cue with price=None) ---
    _CUES = [
        {"name": "dow_futures",    "price": 51077.0,  "change_pct": 0.66,  "direction": "bullish"},
        {"name": "nasdaq_futures", "price": 30405.25, "change_pct": 0.32,  "direction": "bullish"},
        {"name": "sgx_nifty",      "price": 23547.75, "change_pct": -1.50, "direction": "bearish"},
        {"name": "broken_ticker",  "price": None,     "change_pct": None,  "direction": "neutral"},  # bad
        {"name": "crude_oil",      "price": 87.36,    "change_pct": -1.73, "direction": "bearish"},
        {"name": "gold",           "price": 4593.0,   "change_pct": 2.08,  "direction": "bullish"},
        {"name": "usd_inr",        "price": 94.99,    "change_pct": "bad", "direction": "bearish"},  # bad pct
    ]

    # --- dummy NSE data ---
    _NSE = {
        "bhavcopy": [{"symbol": "RELIANCE", "close": 2800.0}],
        "fii_dii": {"fii_net": -21105.86, "dii_net": 16764.14},
        "trading_date": "2026-05-29",
    }

    result = normalise(_NEWS, _CUES, _NSE)

    print(f"\n{'- ' * 35}")
    print(f"  Normalised output")
    print(f"{'- ' * 35}")
    print(f"  headline_count : {result['headline_count']}")
    print(f"  trading_date   : {result['trading_date']}")
    print(f"  fii_dii_summary: {result['fii_dii_summary']}")
    print(f"  normalised_at  : {result['normalised_at']}")
    print(f"\n  Headlines ({len(result['headlines'])}):")
    for h in result["headlines"]:
        print(f"    [{h['id']}] {h['headline'][:65]}...")
    print(f"\n  Global cues ({len(result['global_cues'])}):")
    for c in result["global_cues"]:
        print(f"    {c['name']:18}  price={c['price']}  change_pct={c['change_pct']}")
