"""
Pulse by Zerodha scraper — aggregates ~20 Indian financial news sources.
Single replacement for the five per-site scrapers (ET, Mint, Hindu, Moneycontrol, NDTV).
URL: https://pulse.zerodha.com/
"""

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from loguru import logger

PULSE_URL = "https://pulse.zerodha.com/"
_SOURCE_FALLBACK = "Pulse"
_MIN_HEADLINE_LEN = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _parse(html: str, max_count: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    scraped_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in soup.select("li.box.item"):
        if len(results) >= max_count:
            break

        title_el = item.select_one(".title")
        if not title_el:
            continue

        anchor = title_el if title_el.name == "a" else title_el.find("a")
        if not anchor:
            continue

        headline = anchor.get_text(strip=True)
        if len(headline) < _MIN_HEADLINE_LEN:
            continue

        key = headline[:60].lower().strip()
        if key in seen:
            continue
        seen.add(key)

        url: str = anchor.get("href", "") or ""
        if url and not url.startswith("http"):
            url = "https://pulse.zerodha.com" + url

        feed_el = item.select_one(".feed")
        raw_source = feed_el.get_text(strip=True) if feed_el else _SOURCE_FALLBACK
        # strip leading non-alphabetic chars (bullet/icon that Pulse prepends)
        source = re.sub(r"^[^A-Za-z]+", "", raw_source).strip() or _SOURCE_FALLBACK

        results.append({
            "source": source,
            "headline": headline,
            "url": url,
            "scraped_at": scraped_at,
        })

    return results


async def scrape_pulse(max_headlines: int = 40) -> list[dict[str, Any]]:
    try:
        # Bind to 0.0.0.0 to force IPv4 — the server defaults to IPv6 which
        # causes connectivity issues with some endpoints.
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        async with httpx.AsyncClient(
            transport=transport,
            headers=_HEADERS,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            logger.info(f"Scraping {PULSE_URL}")
            response = await client.get(PULSE_URL)
            response.raise_for_status()
            logger.debug(f"Pulse response: {response.status_code}, {len(response.text):,} chars")
    except httpx.TimeoutException:
        logger.error("Pulse request timed out after 20s")
        return []
    except httpx.HTTPStatusError as exc:
        logger.error(f"Pulse HTTP {exc.response.status_code}")
        return []
    except Exception as exc:
        logger.error(f"Pulse fetch failed: {exc}")
        return []

    try:
        headlines = _parse(response.text, max_headlines)
        unique_sources = len({h["source"] for h in headlines})
        logger.info(f"Scraped {len(headlines)} headlines from Pulse ({unique_sources} sources)")
        return headlines
    except Exception as exc:
        logger.error(f"Pulse parse failed: {exc}")
        return []


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    async def _test() -> None:
        headlines = await scrape_pulse(max_headlines=40)
        if not headlines:
            logger.warning("No headlines returned — check selectors or network")
            return
        sources = sorted({h["source"] for h in headlines})
        print(f"\n{'-' * 70}")
        print(f"  Pulse — {len(headlines)} headline(s) from {len(sources)} sources")
        print(f"  Sources: {', '.join(sources)}")
        print(f"{'-' * 70}")
        for i, item in enumerate(headlines, 1):
            print(f"{i:>2}. [{item['source']}] {item['headline']}")
            print(f"    {item['url']}\n")

    asyncio.run(_test())
