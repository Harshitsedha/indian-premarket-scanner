"""
APScheduler-based scheduler for the PreMarket Pro pipeline.

Jobs (IST):
  08:45 Mon-Fri  morning_briefing          — full pipeline: scrape, analyse, save, notify, healthcheck
  15:35 Mon-Fri  orb_eod                   — persist ORB range outcomes, clean Redis
  15:45 Mon-Fri  outcome_fetcher           — EOD outcome fetch (10:15 UTC)
  16:00 Fri      edge_analyser             — weekly edge pattern analysis (10:30 UTC Fri)
  05:00 Mon      instrument_master_refresh — NSE instrument master weekly refresh (23:30 UTC Sun)
  06:05 Mon      upstox_token_expiry_check — Upstox extended_token expiry warning (00:35 UTC Mon)
"""

import asyncio
import html
import sys
from pathlib import Path

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
from run_pipeline import run
from ingestion.upstox_token_refresh import check_extended_token_expiry
from ingestion.upstox_instruments import refresh_instrument_master
from processing.outcome_fetcher import fetch_and_log_outcomes
from processing.edge_analyser import run_edge_analysis
from processing.orb_eod import run_orb_eod


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

async def _morning_briefing_async() -> None:
    logger.info("Job [morning_briefing] starting")
    try:
        result = await run(save=True, notify=True)
        logger.info(
            f"Job [morning_briefing] finished OK — "
            f"bias={result['bias']['direction']} ({result['bias']['strength']}), "
            f"stocks={len(result['stocks'])}"
        )
        url = settings.healthcheck_scraper_url
        if url:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, timeout=10.0)
                logger.info(f"Job [morning_briefing] healthcheck pinged — HTTP {resp.status_code}")
            except Exception as ping_exc:
                logger.error(f"Job [morning_briefing] healthcheck ping failed: {ping_exc}")
    except Exception as exc:
        logger.error(f"Job [morning_briefing] FAILED: {exc}")
        await _send_failure_alert(exc)


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


async def orb_eod_job() -> None:
    logger.info("Job [orb_eod] starting")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_orb_eod)
        logger.info("Job [orb_eod] done")
    except Exception as exc:
        logger.error(f"Job [orb_eod] FAILED: {exc}")
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


# ── scheduler loop ────────────────────────────────────────────────────────────

async def main() -> None:
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    scheduler.add_job(
        _morning_briefing_async,
        trigger=CronTrigger(hour=8, minute=45, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="morning_briefing",
        name="Morning pipeline run",
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        orb_eod_job,
        CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone="Asia/Kolkata"),
        id="orb_eod",
        name="ORB range EOD persistence",
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.add_job(
        outcome_fetcher_job,
        CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone="UTC"),
        id="outcome_fetcher",
        name="EOD outcome fetch",
        replace_existing=True,
    )
    scheduler.add_job(
        edge_analyser_job,
        CronTrigger(day_of_week="fri", hour=10, minute=30, timezone="UTC"),
        id="edge_analyser",
        name="Weekly edge pattern analysis",
        replace_existing=True,
    )
    scheduler.add_job(
        instrument_master_refresh_job,
        CronTrigger(day_of_week="sun", hour=23, minute=30, timezone="UTC"),
        id="instrument_master_refresh",
        name="NSE instrument master weekly refresh",
        replace_existing=True,
    )
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
    next_run = scheduler.get_job("morning_briefing").next_run_time
    logger.info(f"Scheduler armed ✅ — next run: {next_run}")
    for job in scheduler.get_jobs():
        nxt = job.next_run_time
        ts = nxt.strftime("%Y-%m-%d %H:%M:%S %Z") if nxt else "N/A"
        logger.info(f"  [{job.id}] next run: {ts}")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logger("INFO")

    if "--now" in sys.argv:
        logger.info("--now: running morning_briefing immediately")
        asyncio.run(_morning_briefing_async())
        logger.info("--now run complete")
    else:
        asyncio.run(main())
