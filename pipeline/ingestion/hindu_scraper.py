import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from loguru import logger

URL = "https://www.thehindubusinessline.com/markets/"
SOURCE = "Hindu Business Line"
_BASE = "https://www.thehindubusinessline.com"

_MIN_HEADLINE_LEN = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.thehindubusinessline.com/",
}

_PATH_KEYWORDS = ("/markets/", "/economy/", "/companies/")


def _is_article(href: str) -> bool:
    h = href.lower()
    return any(kw in h for kw in _PATH_KEYWORDS)


def _abs_url(href: str) -> str:
    return href if href.startswith("http") else _BASE + href


def _parse(html: str, max_count: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_headlines: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat()

    for anchor in soup.find_all("a", href=True):
        if len(results) >= max_count:
            break
        href: str = anchor["href"]
        if not _is_article(href):
            continue
        url = _abs_url(href)
        if url in seen_urls:
            continue
        headline = anchor.get_text(strip=True)
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


async def scrape_hindu(max_headlines: int = 10) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=15.0
        ) as client:
            logger.info(f"Scraping {URL}")
            response = await client.get(URL)
            response.raise_for_status()
            logger.debug(
                f"Hindu Business Line response: {response.status_code}, "
                f"{len(response.text):,} chars"
            )
    except httpx.TimeoutException:
        logger.error("Hindu Business Line request timed out after 15s")
        return []
    except httpx.HTTPStatusError as exc:
        logger.error(f"Hindu Business Line HTTP {exc.response.status_code}")
        return []
    except Exception as exc:
        logger.error(f"Hindu Business Line fetch failed: {exc}")
        return []

    try:
        headlines = _parse(response.text, max_headlines)
        logger.info(f"Scraped {len(headlines)} headlines from {SOURCE}")
        return headlines
    except Exception as exc:
        logger.error(f"Hindu Business Line parse failed: {exc}")
        return []


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    async def _test() -> None:
        headlines = await scrape_hindu(max_headlines=10)
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
