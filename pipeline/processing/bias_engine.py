"""
Pure signal aggregation layer.
Combines Claude news analysis, global market cues, and FII/DII flow
into a single weighted bias score.
No API calls, no DB calls -- deterministic computation only.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# Weights -- must sum to 1.0
_W_NEWS = 0.4
_W_GLOBAL = 0.4
_W_FII = 0.2

# Direction thresholds (applied to final_score)
_BULLISH_THRESHOLD = 0.15
_BEARISH_THRESHOLD = -0.15

# Strength thresholds (applied to abs(final_score))
_STRONG_THRESHOLD = 0.6
_MODERATE_THRESHOLD = 0.3

# Parses "FII: -21106 cr" or "FII: +16764 cr" out of the fii_dii_summary string
_FII_RE = re.compile(r"FII:\s*([+-]?\d+(?:\.\d+)?)\s*cr", re.IGNORECASE)


# -- component scorers --------------------------------------------------------

def _score_news(claude_analysis: dict[str, Any]) -> float:
    """
    bullish=+1, bearish=-1, neutral/mixed=0, scaled by confidence/10.
    Max magnitude 0.5 (confidence=5, strong directional call).
    """
    bias = claude_analysis.get("overall_bias") or {}
    direction = str(bias.get("direction") or "neutral").lower()
    try:
        confidence = float(bias.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    base = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0, "mixed": 0.0}.get(direction, 0.0)
    return base * (confidence / 10.0)


def _score_global(normalised: dict[str, Any]) -> float:
    """
    (bullish_count - bearish_count) / total_cues.
    Range: -1.0 (all bearish) to +1.0 (all bullish).
    """
    cues = normalised.get("global_cues") or []
    if not cues:
        return 0.0
    bullish = sum(1 for c in cues if c.get("direction") == "bullish")
    bearish = sum(1 for c in cues if c.get("direction") == "bearish")
    return (bullish - bearish) / len(cues)


def _score_fii(normalised: dict[str, Any]) -> float:
    """
    Parse FII net crore value from fii_dii_summary string.
    Positive net -> +0.5, negative -> -0.5, parse failure -> 0.
    """
    summary_str = str(normalised.get("fii_dii_summary") or "")
    m = _FII_RE.search(summary_str)
    if not m:
        logger.debug(f"FII score: no match in '{summary_str}' -- defaulting to 0")
        return 0.0
    try:
        value = float(m.group(1))
        return 0.5 if value >= 0 else -0.5
    except (ValueError, TypeError):
        return 0.0


# -- labels -------------------------------------------------------------------

def _direction_label(score: float) -> str:
    if score > _BULLISH_THRESHOLD:
        return "bullish"
    if score < _BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _strength_label(score: float) -> str:
    magnitude = abs(score)
    if magnitude >= _STRONG_THRESHOLD:
        return "strong"
    if magnitude >= _MODERATE_THRESHOLD:
        return "moderate"
    return "weak"


# -- summary string -----------------------------------------------------------

_NEWS_PHRASES = {
    "positive": "news sentiment leans positive",
    "negative": "news sentiment leans negative",
    "zero":     "news sentiment is neutral",
}
_GLOBAL_PHRASES = {
    "positive": "global markets are broadly positive",
    "negative": "global markets are broadly negative",
    "zero":     "global markets are mixed",
}
_FII_PHRASES = {
    "positive": "FII buying provides support",
    "negative": "FII selling weighs on sentiment",
    "zero":     "FII/DII data is unavailable",
}

_PHRASE_MAP = {"news": _NEWS_PHRASES, "global": _GLOBAL_PHRASES, "fii": _FII_PHRASES}


def _polarity(score: float) -> str:
    if score > 0.001:
        return "positive"
    if score < -0.001:
        return "negative"
    return "zero"


def _build_summary(
    direction: str,
    strength: str,
    components: dict[str, dict[str, Any]],
) -> str:
    """
    "{strength} {direction} bias -- {dominant reason}[, but {contrarian note}]"
    Dominant component = highest |contribution|.
    """
    dominant = max(components, key=lambda k: abs(components[k]["contribution"]))
    dominant_reason = _PHRASE_MAP[dominant][_polarity(components[dominant]["score"])]

    dominant_contrib = components[dominant]["contribution"]
    contrarians = [
        k for k in components
        if k != dominant
        and (
            (dominant_contrib > 0 and components[k]["contribution"] < -0.005)
            or (dominant_contrib < 0 and components[k]["contribution"] > 0.005)
        )
    ]

    if contrarians:
        contrast = max(contrarians, key=lambda k: abs(components[k]["contribution"]))
        contrast_phrase = _PHRASE_MAP[contrast][_polarity(components[contrast]["score"])]
        return (
            f"{strength.capitalize()} {direction} bias -- "
            f"{dominant_reason}, but {contrast_phrase}"
        )

    return f"{strength.capitalize()} {direction} bias -- {dominant_reason}"


# -- public API ---------------------------------------------------------------

def compute_bias(
    normalised: dict[str, Any],
    claude_analysis: dict[str, Any],
) -> dict[str, Any]:
    """
    Compute a weighted market bias score from three independent signals.

    Args:
        normalised:      output of normaliser.normalise()
        claude_analysis: output of claude_client.analyse_news()

    Returns:
        {
            "final_score": float,       # -1.0 to +1.0
            "direction":   str,         # bullish | bearish | neutral
            "strength":    str,         # strong | moderate | weak
            "components":  dict,        # per-component score / weight / contribution
            "summary":     str,         # human-readable one-liner
            "computed_at": str,         # ISO UTC timestamp
        }
    """
    news_score   = _score_news(claude_analysis)
    global_score = _score_global(normalised)
    fii_score    = _score_fii(normalised)

    news_contrib   = round(news_score   * _W_NEWS,   4)
    global_contrib = round(global_score * _W_GLOBAL, 4)
    fii_contrib    = round(fii_score    * _W_FII,    4)
    final_score    = round(news_contrib + global_contrib + fii_contrib, 4)

    direction = _direction_label(final_score)
    strength  = _strength_label(final_score)

    components: dict[str, Any] = {
        "news":   {"score": round(news_score,   4), "weight": _W_NEWS,   "contribution": news_contrib},
        "global": {"score": round(global_score, 4), "weight": _W_GLOBAL, "contribution": global_contrib},
        "fii":    {"score": round(fii_score,    4), "weight": _W_FII,    "contribution": fii_contrib},
    }

    summary = _build_summary(direction, strength, components)

    result: dict[str, Any] = {
        "final_score": final_score,
        "direction":   direction,
        "strength":    strength,
        "components":  components,
        "summary":     summary,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"Bias computed: {direction.upper()} ({strength})  "
        f"score={final_score:+.4f}  "
        f"[news={news_score:+.3f}, global={global_score:+.3f}, fii={fii_score:+.1f}]"
    )
    return result


# -- standalone test ----------------------------------------------------------
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    # Test 1: mixed signals -- bearish news + FII selling, split global
    _NORM_1 = {
        "global_cues": [
            {"name": "dow_futures",    "price": 51077.0,  "change_pct":  0.66, "direction": "bullish"},
            {"name": "nasdaq_futures", "price": 30405.25, "change_pct":  0.32, "direction": "bullish"},
            {"name": "sgx_nifty",      "price": 23547.75, "change_pct": -1.50, "direction": "bearish"},
            {"name": "crude_oil",      "price": 87.36,    "change_pct": -1.73, "direction": "bearish"},
            {"name": "gold",           "price": 4593.0,   "change_pct":  2.08, "direction": "bullish"},
            {"name": "usd_inr",        "price": 94.99,    "change_pct":  0.10, "direction": "neutral"},
        ],
        "fii_dii_summary": "FII: -21106 cr | DII: +16764 cr",
        "trading_date": "2026-05-29",
    }
    _CLA_1 = {
        "overall_bias": {
            "direction": "bearish",
            "confidence": 4,
            "reason": "FII selling and US selloff outweigh Reliance beat",
        },
    }

    # Test 2: strong bullish sweep
    _NORM_2 = {
        "global_cues": [
            {"name": "dow_futures",    "price": 51077.0, "change_pct":  1.20, "direction": "bullish"},
            {"name": "nasdaq_futures", "price": 30405.0, "change_pct":  0.80, "direction": "bullish"},
            {"name": "sgx_nifty",      "price": 23547.0, "change_pct":  0.50, "direction": "bullish"},
            {"name": "crude_oil",      "price": 82.0,    "change_pct": -0.30, "direction": "bearish"},
        ],
        "fii_dii_summary": "FII: +4500 cr | DII: +2100 cr",
        "trading_date": "2026-05-29",
    }
    _CLA_2 = {
        "overall_bias": {
            "direction": "bullish",
            "confidence": 5,
            "reason": "Broad buying across all asset classes",
        },
    }

    # Test 3: sparse data -- no cues, no FII, low-confidence neutral news
    _NORM_3: dict[str, Any] = {
        "global_cues": [],
        "fii_dii_summary": "FII/DII: unavailable",
        "trading_date": "2026-05-29",
    }
    _CLA_3 = {
        "overall_bias": {"direction": "neutral", "confidence": 2, "reason": "Insufficient data"},
    }

    # Test 4: neutral direction despite moderate absolute components (balanced)
    _NORM_4 = {
        "global_cues": [
            {"name": "dow_futures",    "price": 51000.0, "change_pct":  0.5, "direction": "bullish"},
            {"name": "nasdaq_futures", "price": 30000.0, "change_pct": -0.5, "direction": "bearish"},
        ],
        "fii_dii_summary": "FII: +100 cr | DII: -80 cr",
        "trading_date": "2026-05-29",
    }
    _CLA_4 = {
        "overall_bias": {"direction": "mixed", "confidence": 3, "reason": "Conflicting signals"},
    }

    _TESTS = [
        ("Test 1 -- mixed signals (bearish news + FII selling)", _NORM_1, _CLA_1),
        ("Test 2 -- strong bullish sweep",                       _NORM_2, _CLA_2),
        ("Test 3 -- sparse / unavailable data",                  _NORM_3, _CLA_3),
        ("Test 4 -- balanced / near-neutral",                    _NORM_4, _CLA_4),
    ]

    for label, norm, cla in _TESTS:
        r = compute_bias(norm, cla)
        print(f"\n{'- ' * 35}")
        print(f"  {label}")
        print(f"{'- ' * 35}")
        print(f"  final_score  : {r['final_score']:+.4f}")
        print(f"  direction    : {r['direction'].upper()}")
        print(f"  strength     : {r['strength']}")
        print(f"  summary      : {r['summary']}")
        print(f"\n  Components:")
        for name, c in r["components"].items():
            filled = max(1, int(abs(c["contribution"]) * 50))
            bar = ("+" if c["contribution"] >= 0 else "-") * filled
            print(
                f"    {name:8}  score={c['score']:+.3f}  "
                f"weight={c['weight']}  contrib={c['contribution']:+.4f}  {bar}"
            )
