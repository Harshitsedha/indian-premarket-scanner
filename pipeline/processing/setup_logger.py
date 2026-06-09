"""
setup_logger.py — Log today's ranked stocks into the setups table.

Called at end of daily pipeline run after ranker.rank_stocks().
Idempotent: skips symbols already logged for the trading_date.
"""

from datetime import date

import psycopg2
import psycopg2.extras
from loguru import logger

from utils.config import settings


def log_setups(
    ranked: list[dict],
    claude_analysis: dict,
    trading_date: date | None = None,
) -> list[int]:
    """
    Insert one row per ranked stock into setups table.
    Returns list of setup IDs for the date (new inserts + pre-existing).
    Skips symbols already logged for this trading_date (idempotent).
    """
    if trading_date is None:
        trading_date = date.today()

    overall_bias    = claude_analysis.get("overall_bias") or {}
    bias_direction  = overall_bias.get("direction")
    bias_confidence = overall_bias.get("confidence")

    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    setup_ids: list[int] = []

    try:
        for stock in ranked:
            symbol = stock["symbol"]

            with conn:
                with conn.cursor() as cur:
                    # Idempotency check
                    cur.execute(
                        "SELECT id FROM setups WHERE trading_date = %s AND symbol = %s",
                        (trading_date, symbol),
                    )
                    existing = cur.fetchone()

                    if existing:
                        logger.warning(
                            f"Setup already logged for {symbol} on {trading_date} "
                            f"(id={existing[0]}) — skipping"
                        )
                        setup_ids.append(existing[0])
                        continue

                    signals = stock.get("signals") or {}
                    cur.execute(
                        """
                        INSERT INTO setups (
                            trading_date, symbol, setup_type, hypothesis,
                            bias_direction, bias_confidence,
                            gap_pct, gap_source, score, signals, thesis,
                            catalyst_line, direction
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            trading_date,
                            symbol,
                            stock.get("setup_type"),
                            stock.get("sentiment"),                # hypothesis = expected direction
                            bias_direction,
                            bias_confidence,
                            signals.get("prior_session_gap_pct"), # stored in gap_pct DB column
                            signals.get("move_source"),            # stored in gap_source DB column
                            stock.get("score"),
                            psycopg2.extras.Json(signals),
                            stock.get("thesis"),
                            stock.get("catalyst_line"),            # new: trader-facing one-liner
                            stock.get("direction"),                # new: bullish|bearish|neutral
                        ),
                    )
                    setup_id = cur.fetchone()[0]
                    setup_ids.append(setup_id)
                    logger.info(
                        f"Logged setup: {symbol} id={setup_id} "
                        f"({stock.get('setup_type')}, {stock.get('sentiment')})"
                    )

    finally:
        conn.close()

    logger.info(f"log_setups: {len(setup_ids)} setups for {trading_date}")
    return setup_ids
