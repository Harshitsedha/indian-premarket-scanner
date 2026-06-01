"""
APScheduler-based scheduler for the PreMarket Pro pipeline.

Jobs (IST):
  08:45 Mon-Fri  morning_briefing  — full pipeline: scrape, analyse, save, notify
  08:50 Mon-Fri  health_ping       — ping Healthchecks.io only if pipeline succeeded
"""

import asyncio
import html
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure pipeline/ root is importable when run directly
_PIPELINE_ROOT = Path(__file__).resolve().parent
if str(_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx
import telegram
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from utils.config import settings
from utils.logger import setup_logger
from run_pipeline import run as _run_pipeline
from ingestion.upstox_token_refresh import check_extended_token_expiry
from ingestion.upstox_instruments import refresh_instrument_master
from processing.outcome_fetcher import fetch_and_log_outcomes
from processing.edge_analyser import run_edge_analysis

_IST = ZoneInfo("Asia/Kolkata")

# Set True by morning_briefing on success; read by health_ping to gate the ping.
_pipeline_ok: bool = False


# ── failure alert ─────────────────────────────────────────────────────────────

async def _send_failure_alert(exc: Exception) -> None:
    msg = (
        "<b>❌ PreMarket Pro — Pipeline FAILED</b>\n\n"
        f"<code>{html.escape(str(exc))}</code>"
    )
    try:
        async with telegram.Bot(token=settings.telegram_bot_token) as bot:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=msg,
                parse_mode=telegram.constants.ParseMode.HTML,
            )
        logger.info("Failure alert sent to Telegram")
    except Exception as tg_exc:
        logger.error(f"Failed to send Telegram failure alert: {tg_exc}")


# ── jobs ──────────────────────────────────────────────────────────────────────

async def outcome_fetcher_job() -> None:
    logger.info("Job [outcome_fetcher] starting")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fetch_and_log_outcomes)
        logger.info("Job [outcome_fetcher] done")
    except Exception as exc:
        logger.error(f"Job [outcome_fetcher] FAILED: {exc}")
        await _send_failure_alert(exc)


async def edge_analyser_job() -> None:
    logger.info("Job [edge_analyser] starting")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_edge_analysis)
        logger.info("Job [edge_analyser] done")
    except Exception as exc:
        logger.error(f"Job [edge_analyser] FAILED: {exc}")
        await _send_failure_alert(exc)


async def instrument_master_refresh_job() -> None:
    logger.info("Job [instrument_master_refresh] starting")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, refresh_instrument_master)
        logger.info("Job [instrument_master_refresh] done")
    except Exception as exc:
        logger.error(f"Job [instrument_master_refresh] FAILED: {exc}")
        await _send_failure_alert(exc)


async def morning_briefing_job() -> None:
    global _pipeline_ok
    _pipeline_ok = False
    logger.info("Job [morning_briefing] starting")

    try:
        # _run_pipeline is sync and calls asyncio.run() internally.
        # Run it in a thread executor so it gets its own event loop.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _run_pipeline(save=True, notify=True),
        )
        _pipeline_ok = True
        logger.info(
            f"Job [morning_briefing] finished OK -- "
            f"bias={result['bias']['direction']} ({result['bias']['strength']}), "
            f"stocks={len(result['stocks'])}"
        )
    except Exception as exc:
        logger.error(f"Job [morning_briefing] FAILED: {exc}")
        await _send_failure_alert(exc)
        # Do not re-raise — scheduler must survive job failures


async def health_ping_job() -> None:
    if not _pipeline_ok:
        logger.warning("Job [health_ping] skipping -- pipeline did not succeed this morning")
        return

    url = settings.healthcheck_scraper_url
    if not url:
        logger.debug("Job [health_ping] skipping -- HEALTHCHECK_SCRAPER_URL not configured")
        return

    logger.info(f"Job [health_ping] pinging {url}")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
        logger.info(f"Job [health_ping] ok -- HTTP {resp.status_code}")
    except Exception as exc:
        logger.error(f"Job [health_ping] ping failed: {exc}")


# ── scheduler loop ────────────────────────────────────────────────────────────

async def _scheduler_loop() -> None:
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    scheduler.add_job(
        morning_briefing_job,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=45,
        id="morning_briefing",
        name="Morning pipeline run",
    )
    scheduler.add_job(
        health_ping_job,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=50,
        id="health_ping",
        name="Healthchecks.io ping",
    )
    # Outcome fetcher — 15:45 IST = 10:15 UTC, weekdays
    scheduler.add_job(
        outcome_fetcher_job,
        CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone="UTC"),
        id="outcome_fetcher",
        name="EOD outcome fetch",
        replace_existing=True,
    )
    # Edge analyser — every Friday 16:00 IST = 10:30 UTC
    scheduler.add_job(
        edge_analyser_job,
        CronTrigger(day_of_week="fri", hour=10, minute=30, timezone="UTC"),
        id="edge_analyser",
        name="Weekly edge pattern analysis",
        replace_existing=True,
    )
    # Instrument master refresh — every Sunday 23:30 UTC (Monday 05:00 IST)
    scheduler.add_job(
        instrument_master_refresh_job,
        CronTrigger(day_of_week="sun", hour=23, minute=30, timezone="UTC"),
        id="instrument_master_refresh",
        name="NSE instrument master weekly refresh",
        replace_existing=True,
    )
    # Extended token expiry warning — every Monday 06:05 IST (00:35 UTC)
    scheduler.add_job(
        check_extended_token_expiry,
        "cron",
        day_of_week="mon",
        hour=0,
        minute=35,
        timezone="UTC",
        id="upstox_token_expiry_check",
        name="Upstox extended_token expiry check",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started -- 2 jobs armed (Asia/Kolkata)")
    for job in scheduler.get_jobs():
        nxt = job.next_run_time
        ts = nxt.strftime("%Y-%m-%d %H:%M:%S %Z") if nxt else "N/A"
        logger.info(f"  [{job.id}] next run: {ts}")

    try:
        await asyncio.Event().wait()   # block until Ctrl-C / SIGTERM
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logger("INFO")

    if "--now" in sys.argv:
        logger.info("--now: running morning_briefing immediately")
        asyncio.run(morning_briefing_job())
        logger.info("--now: running health_ping immediately")
        asyncio.run(health_ping_job())
        logger.info("--now run complete")
    else:
        asyncio.run(_scheduler_loop())
