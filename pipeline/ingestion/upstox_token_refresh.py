"""
upstox_token_refresh.py — Weekly extended_token expiry monitor.

There is no Upstox programmatic refresh flow. This module's sole job is to
warn via Telegram when the extended_token is within 30 days of expiry so
the operator knows to re-run upstox_auth.py before it lapses.

Called by scheduler.py every Monday at 06:05 IST (00:35 UTC).
"""

import psycopg2
import telegram
from datetime import datetime, timezone
from loguru import logger

from utils.config import settings


async def _send_token_alert(msg: str) -> None:
    try:
        async with telegram.Bot(token=settings.telegram_bot_token) as bot:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=msg,
                parse_mode=telegram.constants.ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram token alert: {e}")


async def check_extended_token_expiry() -> None:
    """Warn via Telegram if extended_token expires within 30 days."""
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT expires_at FROM upstox_tokens ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.error(f"DB read failed in expiry check: {e}")
        await _send_token_alert(f"⚠️ Upstox expiry check failed — DB error: {e}")
        return

    if not row:
        await _send_token_alert("⚠️ No Upstox token found in DB — run upstox_auth.py")
        return

    days_left = (row[0] - datetime.now(timezone.utc)).days
    if days_left < 30:
        await _send_token_alert(
            f"⚠️ Upstox <b>extended_token</b> expires in <b>{days_left} days</b> — "
            f"re-run <code>python -m ingestion.upstox_auth</code> on your local machine"
        )
        logger.warning(f"Extended token expires in {days_left} days — alert sent")
    else:
        logger.info(f"Extended token healthy — {days_left} days remaining")
