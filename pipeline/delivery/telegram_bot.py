"""
Telegram delivery layer — formats and sends the pre-market briefing.
"""

import asyncio
import html
import sys
from pathlib import Path
from typing import Any

# Ensure pipeline/ root is on sys.path when this file is run directly
_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

import telegram
from loguru import logger

from utils.config import settings


# ── constants ─────────────────────────────────────────────────────────────────

_TELEGRAM_CHAR_LIMIT = 4096

_DIRECTION_EMOJI: dict[str, str] = {
    "bullish": "\U0001f7e2",   # 🟢
    "bearish": "\U0001f534",   # 🔴
    "neutral": "⚪",       # ⚪
    "mixed":   "\U0001f7e1",   # 🟡
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _emoji(direction: str) -> str:
    return _DIRECTION_EMOJI.get((direction or "").lower(), "⚪")


def _global_line(global_cues: list[dict[str, Any]]) -> str:
    if not global_cues:
        return "unavailable"
    bullish = sum(1 for c in global_cues if str(c.get("direction", "")).lower() == "bullish")
    bearish = sum(1 for c in global_cues if str(c.get("direction", "")).lower() == "bearish")
    neutral = len(global_cues) - bullish - bearish
    if bullish > bearish:
        label, em = "Bullish", _emoji("bullish")
    elif bearish > bullish:
        label, em = "Bearish", _emoji("bearish")
    else:
        label, em = "Neutral", _emoji("neutral")
    return f"{em} {label} ({bullish}B / {bearish}Be / {neutral}N, {len(global_cues)} cues)"


def _build_message(
    bias: dict[str, Any],
    stocks: list[dict[str, Any]],
    normalised: dict[str, Any],
) -> str:
    direction     = str(bias.get("direction", "neutral"))
    strength      = str(bias.get("strength", "weak"))
    score         = float(bias.get("final_score", 0.0))
    summary       = html.escape(str(bias.get("summary", "")))
    trading_date  = html.escape(str(normalised.get("trading_date", "")))
    fii_summary   = html.escape(str(normalised.get("fii_dii_summary", "FII/DII: unavailable")))
    global_info   = _global_line(normalised.get("global_cues") or [])

    em = _emoji(direction)

    lines = [
        "<b>\U0001f514 PRE-MARKET BRIEFING</b>",
        f"<i>{trading_date}</i>",
        "",
        f"<b>Market Bias: {em} {html.escape(direction.upper())} ({html.escape(strength)})</b>",
        f"Score: {score:+.3f}",
        summary,
        "",
        f"\U0001f4b0 {fii_summary}",
        f"\U0001f30d Global: {global_info}",
        "",
        "<b>\U0001f4ca Stocks in Play</b>",
    ]

    for s in stocks[:7]:
        rank   = s.get("rank", "?")
        symbol = html.escape(str(s.get("symbol", "")))
        sent   = str(s.get("sentiment", "neutral"))
        setup  = html.escape(str(s.get("setup_type", "")))
        thesis = html.escape(str(s.get("thesis", "")))
        sig       = s.get("signals") or {}
        stock_move = float(sig.get("prior_session_gap_pct", 0.0))
        move_src   = str(sig.get("move_source", "historical_gap"))
        stk_score  = float(s.get("score", 0.0))
        lines.append(f"{rank}. <b>{symbol}</b> — {_emoji(sent)} {html.escape(sent)} · {setup}")
        lines.append(f"   <i>{thesis}</i>")
        lines.append(f"   <i>score={stk_score:.2f} · move={stock_move:+.1f}% ({move_src})</i>")

    return "\n".join(lines)


# ── public API ────────────────────────────────────────────────────────────────

async def send_briefing(
    bias: dict[str, Any],
    stocks: list[dict[str, Any]],
    normalised: dict[str, Any],
) -> None:
    """Format and deliver the pre-market briefing to the configured Telegram chat."""
    text = _build_message(bias, stocks, normalised)

    if len(text) > _TELEGRAM_CHAR_LIMIT:
        text = text[: _TELEGRAM_CHAR_LIMIT - 3] + "..."

    try:
        async with telegram.Bot(token=settings.telegram_bot_token) as bot:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                parse_mode=telegram.constants.ParseMode.HTML,
            )
        logger.info(
            f"Telegram briefing sent to chat_id={settings.telegram_chat_id} "
            f"({len(text)} chars)"
        )
    except telegram.error.TelegramError as exc:
        logger.error(f"Telegram send failed: {type(exc).__name__}: {exc}")
        raise


# ── standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    from utils.logger import setup_logger

    setup_logger("INFO")

    _bias = {
        "direction": "bearish",
        "strength": "moderate",
        "final_score": -0.38,
        "summary": (
            "Bearish moderate: global cues lean bearish, FII selling continues. "
            "Risk-off tone likely at open."
        ),
        "components": {"news": -0.3, "global": -0.4, "fii": -0.5},
        "computed_at": "2026-06-01T05:00:00+00:00",
    }

    _stocks = [
        {
            "rank": 1, "symbol": "INFY", "score": 0.607,
            "sentiment": "bearish", "setup_type": "fii_driven", "mention_count": 2,
            "thesis": "FII selling + bearish earnings guidance; watch for gap-down open",
        },
        {
            "rank": 2, "symbol": "RELIANCE", "score": 0.517,
            "sentiment": "bullish", "setup_type": "news_catalyst", "mention_count": 1,
            "thesis": "Q4 profit beat; retail + Jio strong; possible gap-up",
        },
        {
            "rank": 3, "symbol": "HDFCBANK", "score": 0.300,
            "sentiment": "bearish", "setup_type": "gap_play", "mention_count": 0,
            "thesis": "SGX Nifty down; banking sector may see selling pressure",
        },
        {
            "rank": 4, "symbol": "TCS", "score": 0.175,
            "sentiment": "bearish", "setup_type": "watchlist", "mention_count": 0,
            "thesis": "No catalyst; gap-play candidate if IT sector dips",
        },
        {
            "rank": 5, "symbol": "SBIN", "score": 0.175,
            "sentiment": "bearish", "setup_type": "watchlist", "mention_count": 0,
            "thesis": "No catalyst; gap-play candidate if market-wide selling",
        },
        {
            "rank": 6, "symbol": "BHARTIARTL", "score": 0.175,
            "sentiment": "neutral", "setup_type": "watchlist", "mention_count": 0,
            "thesis": "No catalyst; monitor for range breakout",
        },
        {
            "rank": 7, "symbol": "ITC", "score": 0.175,
            "sentiment": "neutral", "setup_type": "watchlist", "mention_count": 0,
            "thesis": "No catalyst; FMCG may provide relative safety",
        },
    ]

    _normalised = {
        "trading_date": "2026-06-01",
        "fii_dii_summary": "FII: -3245 cr | DII: +2189 cr",
        "headline_count": 10,
        "global_cues": [
            {"name": "sgx_nifty",      "price": 24100.0, "change_pct": -1.2, "direction": "bearish"},
            {"name": "dow_futures",    "price": 42800.0, "change_pct": -0.4, "direction": "bearish"},
            {"name": "nasdaq_futures", "price": 18900.0, "change_pct":  0.2, "direction": "bullish"},
            {"name": "nikkei",         "price": 38200.0, "change_pct": -0.8, "direction": "bearish"},
            {"name": "hangseng",       "price": 17800.0, "change_pct":  0.3, "direction": "bullish"},
            {"name": "crude_oil",      "price":    74.5, "change_pct":  1.8, "direction": "bullish"},
            {"name": "gold",           "price":  3280.0, "change_pct": -0.3, "direction": "bearish"},
        ],
    }

    asyncio.run(send_briefing(_bias, _stocks, _normalised))
