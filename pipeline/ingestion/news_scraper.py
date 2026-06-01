import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger

ET_MARKETS_URL = "https://economictimes.indiatimes.com/markets"
ET_BASE = "https://economictimes.indiatimes.com"
SOURCE = "Economic Times"

# Minimum character length to accept a headline — filters out icon/button text
_MIN_HEADLINE_LEN = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://economictimes.indiatimes.com/",
}


def _abs_url(href: str) -> str:
    return href if href.startswith("http") else ET_BASE + href


def _best_text(anchor: Tag) -> str:
    """
    ET embeds the headline in the anchor itself (React layout).
    Falls back to the nearest heading ancestor if anchor text is too short.
    """
    text = anchor.get_text(strip=True)
    if len(text) >= _MIN_HEADLINE_LEN:
        return text
    for parent in anchor.parents:
        if parent.name in ("h1", "h2", "h3", "h4"):
            t = parent.get_text(strip=True)
            if len(t) >= _MIN_HEADLINE_LEN:
                return t
        if parent.name in ("div", "li", "article", "section"):
            break
    return text


def _parse(html: str, max_count: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_headlines: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    # Find every anchor whose href contains 'articleshow' — the stable ET article marker
    for anchor in soup.find_all("a", href=True):
        if len(results) >= max_count:
            break
        href: str = anchor["href"]
        if "articleshow" not in href:
            continue
        url = _abs_url(href)
        if url in seen_urls:
            continue
        headline = _best_text(anchor)
        if len(headline) < _MIN_HEADLINE_LEN or headline in seen_headlines:
            continue
        seen_urls.add(url)
        seen_headlines.add(headline)
        results.append({
            "source": SOURCE,
            "headline": headline,
            "url": url,
            "scraped_at": scraped_at,
        })

    return results


async def scrape_et_markets(max_headlines: int = 10) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=15.0
        ) as client:
            logger.info(f"Scraping {ET_MARKETS_URL}")
            response = await client.get(ET_MARKETS_URL)
            response.raise_for_status()
            logger.debug(
                f"ET Markets response: {response.status_code}, "
                f"{len(response.text):,} chars"
            )
    except httpx.TimeoutException:
        logger.error("ET Markets request timed out after 15s")
        return []
    except httpx.HTTPStatusError as exc:
        logger.error(f"ET Markets HTTP {exc.response.status_code}")
        return []
    except Exception as exc:
        logger.error(f"ET Markets fetch failed: {exc}")
        return []

    try:
        headlines = _parse(response.text, max_headlines)
        logger.info(f"Scraped {len(headlines)} headlines from {SOURCE}")
        return headlines
    except Exception as exc:
        logger.error(f"ET Markets parse failed: {exc}")
        return []


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    async def _test() -> None:
        headlines = await scrape_et_markets(max_headlines=10)
        if not headlines:
            logger.warning("No headlines returned - check selectors or network")
            return
        print(f"\n{'-' * 70}")
        print(f"  {SOURCE} - {len(headlines)} headline(s)")
        print(f"{'-' * 70}")
        for i, item in enumerate(headlines, 1):
            print(f"{i:>2}. {item['headline']}")
            print(f"    {item['url']}")
            print(f"    scraped_at: {item['scraped_at']}\n")

    asyncio.run(_test())
