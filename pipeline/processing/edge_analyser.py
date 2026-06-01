"""
edge_analyser.py — Weekly edge pattern analysis via Claude + Telegram delivery.

Called every Friday at 16:00 IST (10:30 UTC).
Queries last 90 days of setups + outcomes, builds stats summary,
asks Claude Haiku for pattern analysis, delivers to Telegram.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras
from loguru import logger

from utils.config import settings
from processing.edge_stats import query_rows as _query_rows, build_stats as _build_stats


# ── Claude ────────────────────────────────────────────────────────────────────

def _call_claude(stats: dict, lookback_days: int) -> str:
    prompt = f"""You are analysing a trader's pre-market setup log for the Indian equity market (NSE).
Here is a summary of the last {lookback_days} days of setups and outcomes:

STATS:
{json.dumps(stats, indent=2, default=str)}

Analyse this data and provide:
1. Which setup_type has the highest edge (hypothesis correct rate vs base rate)?
2. Which bias_direction days produce the best outcomes?
3. Any patterns in gap_pct that predict hypothesis_correct?
4. Top 3 specific actionable rule suggestions to improve the scoring system.
5. Any setups or symbols that consistently underperform — flag them.

Be specific and quantitative. Reference actual numbers from the stats.
Keep response under 400 words. Use plain text, no markdown headers."""

    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> None:
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Telegram delivery failed in edge_analyser: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def run_edge_analysis(lookback_days: int = 90) -> None:
    """Query last N days of setups+outcomes, ask Claude for patterns, deliver to Telegram."""
    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )
    try:
        rows = _query_rows(conn, lookback_days)
    except Exception as e:
        logger.error(f"DB query failed in edge_analyser: {e}")
        conn.close()
        return
    finally:
        conn.close()

    stats = _build_stats(rows)
    logger.info(
        f"Edge analysis: {stats['total_setups']} setups, "
        f"{stats['with_outcomes']} with outcomes over {lookback_days}d"
    )

    if stats["total_setups"] == 0:
        logger.warning("No setups found for edge analysis — skipping Claude call")
        return

    try:
        analysis_text = _call_claude(stats, lookback_days)
    except Exception as e:
        logger.error(f"Claude edge analysis failed: {e}")
        return

    message = (
        f"Weekly edge report — {lookback_days}d lookback\n"
        f"Setups: {stats['total_setups']} | With outcomes: {stats['with_outcomes']}\n\n"
        f"{analysis_text}"
    )
    _send_telegram(message)
    logger.info("Edge analysis delivered to Telegram")
