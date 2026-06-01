"""
outcome_fetcher.py — Fetch EOD OHLCV for today's setups and log outcomes.

Called by scheduler at 15:45 IST (10:15 UTC) every weekday.
After 15:30 IST market close, get_prev_close() returns the completed day candle.
"""

import time
from datetime import date

import psycopg2
import psycopg2.extras
from loguru import logger

from utils.config import settings
from ingestion.upstox_client import UpstoxClient


def fetch_and_log_outcomes(trading_date: date | None = None) -> None:
    """
    For each setup in setups for trading_date with no outcome yet,
    fetch EOD OHLCV from Upstox and insert into outcomes table.
    """
    if trading_date is None:
        trading_date = date.today()

    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, symbol, hypothesis
                FROM setups
                WHERE trading_date = %s
                  AND id NOT IN (SELECT setup_id FROM outcomes)
                ORDER BY id
                """,
                (trading_date,),
            )
            pending = cur.fetchall()
    except Exception as e:
        logger.error(f"DB query failed in outcome_fetcher: {e}")
        conn.close()
        return

    if not pending:
        logger.info(f"No pending setups for {trading_date} — nothing to fetch")
        conn.close()
        return

    client    = UpstoxClient()
    n_fetched = 0

    for setup in pending:
        setup_id   = setup["id"]
        symbol     = setup["symbol"]
        hypothesis = setup["hypothesis"]

        try:
            # After 15:45 IST, get_prev_close returns today's completed candle
            today_ohlcv = client.get_prev_close(symbol)
            if not today_ohlcv:
                logger.warning(f"No EOD data for {symbol} — skipping outcome")
                time.sleep(0.1)
                continue

            # get_ohlcv_history returns oldest-first; [-2] = yesterday, [-1] = today
            history = client.get_ohlcv_history(symbol, days=2)
            if len(history) >= 2:
                prev_close     = history[-2]["close"]
                actual_gap_pct = round(
                    (today_ohlcv["open"] - prev_close) / prev_close * 100, 2
                )
            else:
                logger.warning(f"Insufficient history for {symbol} — gap_pct will be null")
                actual_gap_pct = None

            open_price  = today_ohlcv["open"]
            close_price = today_ohlcv["close"]
            move_pct    = round((close_price - open_price) / open_price * 100, 2)

            if hypothesis == "bullish":
                hypothesis_correct: bool | None = move_pct > 0
            elif hypothesis == "bearish":
                hypothesis_correct = move_pct < 0
            else:
                hypothesis_correct = None   # neutral or unknown — can't evaluate

            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO outcomes (
                            setup_id, trading_date, symbol,
                            open_price, actual_gap_pct,
                            day_high, day_low, close_price,
                            move_pct, hypothesis_correct
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            setup_id, trading_date, symbol,
                            open_price, actual_gap_pct,
                            today_ohlcv["high"], today_ohlcv["low"], close_price,
                            move_pct, hypothesis_correct,
                        ),
                    )

            n_fetched += 1
            logger.info(
                f"Outcome: {symbol}  open={open_price}  close={close_price}  "
                f"move={move_pct:+.2f}%  hypothesis_correct={hypothesis_correct}"
            )

        except Exception as e:
            logger.error(f"Failed outcome for {symbol} (setup_id={setup_id}): {e}")

        time.sleep(0.1)

    conn.close()
    logger.info(f"Outcomes fetched: {n_fetched} symbols for {trading_date}")
