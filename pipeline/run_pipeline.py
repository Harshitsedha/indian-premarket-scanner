"""
Manual end-to-end pipeline run.
Chains all five modules: ingest -> normalise -> analyse -> bias -> rank.
Use this to verify the full pipeline before the scheduler is wired.
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root before any module that reads Settings is imported
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Ensure pipeline/ is on sys.path so sub-packages resolve correctly
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger                                       # noqa: E402
from utils.logger import setup_logger                          # noqa: E402
from ingestion.pulse_scraper import scrape_pulse               # noqa: E402
from ingestion.nse_scraper import fetch_nse_data               # noqa: E402
from ingestion.global_cues import fetch_global_cues, bias_summary  # noqa: E402
from processing.normaliser import normalise                    # noqa: E402
from processing.claude_client import analyse_news, ClaudeClientError  # noqa: E402
from processing.bias_engine import compute_bias                # noqa: E402
from processing.ranker import rank_stocks                      # noqa: E402
from processing.setup_logger import log_setups                 # noqa: E402
from storage.database import save_briefing                     # noqa: E402
from delivery.telegram_bot import send_briefing as telegram_send  # noqa: E402


async def run(save: bool = False, notify: bool = False) -> dict:
    setup_logger("INFO")
    logger.info("=== PreMarket Pro pipeline starting ===")

    # Step 1 -- Ingestion
    logger.info("Step 1/5 -- Ingestion")
    all_headlines = await scrape_pulse(max_headlines=40)
    seen: set[str] = set()
    news: list[dict] = []
    for h in all_headlines:
        key = h["headline"][:60].lower().strip()
        if key not in seen:
            seen.add(key)
            news.append(h)
    logger.info(f"Total headlines after dedup: {len(news)} (from Pulse)")
    nse = fetch_nse_data()
    cues = fetch_global_cues()
    global_bias = bias_summary(cues)
    logger.info(
        f"  news={len(news)} headlines, cues={len(cues)} tickers, "
        f"global_bias={global_bias['global_bias']}"
    )

    # Step 2 -- Normalise
    logger.info("Step 2/5 -- Normalising")
    normalised = normalise(news, cues, nse)
    logger.info(f"  {normalised['headline_count']} headlines after clean")

    # Step 3 -- Claude analysis
    logger.info("Step 3/5 -- Claude API analysis")
    if normalised["headline_count"] == 0:
        raise RuntimeError(
            "No headlines available after normalisation — "
            "likely ET Markets scraper blocked or all headlines too short/duplicate"
        )
    try:
        analysis = analyse_news(normalised["headlines"])
    except ClaudeClientError as exc:
        logger.error(f"Claude analysis failed: {exc}")
        raise
    logger.info(
        f"  bias={analysis['overall_bias']['direction']}, "
        f"confidence={analysis['overall_bias']['confidence']}"
    )

    # Step 4 -- Bias engine
    logger.info("Step 4/5 -- Computing bias score")
    bias = compute_bias(normalised, analysis)
    logger.info(
        f"  score={bias['final_score']}, direction={bias['direction']}, "
        f"strength={bias['strength']}"
    )

    # Step 5 -- Rank stocks
    logger.info("Step 5/5 -- Ranking stocks in play")
    stocks = rank_stocks(normalised, analysis)
    logger.info(f"  {len(stocks)} stocks in play")

    # Step 5b -- Log setups for edge tracking (non-fatal if DB unavailable)
    try:
        setup_ids = log_setups(stocks, analysis)
        logger.info(f"  Logged {len(setup_ids)} setups for today")
    except Exception as exc:
        logger.error(f"  Setup logging failed (non-fatal): {exc}")

    # Print full briefing summary
    print("\n" + "=" * 60)
    print("PRE-MARKET BRIEFING")
    print("=" * 60)
    print(f"Date          : {normalised['trading_date']}")
    print(f"Bias          : {bias['direction'].upper()} ({bias['strength']})")
    print(f"Score         : {bias['final_score']}")
    print(f"Summary       : {bias['summary']}")
    print(f"FII/DII       : {normalised['fii_dii_summary']}")
    print(
        f"Global        : {global_bias['global_bias'].upper()} "
        f"({global_bias['bullish_count']}B / "
        f"{global_bias['bearish_count']}Be / "
        f"{global_bias['neutral_count']}N)"
    )
    print(f"\nStocks in play ({len(stocks)}):")
    for s in stocks:
        print(
            f"  [{s['rank']}] {s['symbol']:<15} score={s['score']:.3f}  "
            f"{s['sentiment']:<8}  {s['setup_type']:<15}  {s['thesis']}"
        )
    print("=" * 60)

    if save:
        logger.info("Saving briefing to database...")
        briefing_id = await save_briefing(bias, stocks, normalised, analysis)
        print(f"\nBriefing saved  : id={briefing_id}")

    if notify:
        logger.info("Sending Telegram notification...")
        await telegram_send(bias, stocks, normalised)
        print("Telegram sent   : ok")

    return {
        "bias": bias,
        "stocks": stocks,
        "normalised": normalised,
        "analysis": analysis,
        "global_bias": global_bias,
    }


if __name__ == "__main__":
    save_flag   = "--save"   in sys.argv
    notify_flag = "--notify" in sys.argv
    result = asyncio.run(run(save=save_flag, notify=notify_flag))
    sys.exit(0)
