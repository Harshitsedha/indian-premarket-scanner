"""
Async SQLAlchemy engine, session factory, and pipeline persistence helpers.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from storage.models import Base, DailyBriefing, Headline, StockInPlay

try:
    from utils.config import settings
except Exception as exc:
    raise RuntimeError(
        "database.py requires a valid .env — "
        "ensure POSTGRES_PASSWORD and other DB vars are set"
    ) from exc

# ── engine -------------------------------------------------------------------

engine = create_async_engine(
    settings.postgres_dsn,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ── session context manager --------------------------------------------------

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that yields a session, commits on clean exit,
    rolls back on exception, and always closes.

    Usage:
        async with get_session() as session:
            session.add(obj)
            await session.commit()
    """
    session: AsyncSession = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── table creation -----------------------------------------------------------

async def init_db() -> None:
    """Create all tables defined in Base.metadata (safe to call repeatedly)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created / verified")


# ── pipeline persistence -----------------------------------------------------

async def save_briefing(
    bias: dict[str, Any],
    stocks: list[dict[str, Any]],
    normalised: dict[str, Any],
    analysis: dict[str, Any],
) -> int:
    """
    Persist one complete pipeline run to the database.

    Upserts on trading_date: if a briefing for today already exists it is
    updated in-place and its child rows are replaced, so re-running the
    pipeline on the same day overwrites rather than duplicates.

    Args:
        bias:       output of bias_engine.compute_bias()
        stocks:     output of ranker.rank_stocks()
        normalised: output of normaliser.normalise()
        analysis:   output of claude_client.analyse_news()

    Returns:
        briefing_id (int)
    """
    from datetime import date as date_type

    trading_date_raw = normalised.get("trading_date") or str(date_type.today())
    try:
        trading_date = date_type.fromisoformat(str(trading_date_raw))
    except ValueError:
        trading_date = date_type.today()

    fii_dii = normalised.get("fii_dii_summary") or ""
    fii_net = _parse_crore(fii_dii, "FII")
    dii_net = _parse_crore(fii_dii, "DII")
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # Check for existing briefing on this trading date
        result = await session.execute(
            select(DailyBriefing).where(DailyBriefing.trading_date == trading_date)
        )
        briefing = result.scalar_one_or_none()

        # Set all scalar fields first so the INSERT (on flush) is never partial
        bias_direction = bias.get("direction", "neutral")
        bias_score     = float(bias.get("final_score", 0.0))
        bias_strength  = bias.get("strength", "weak")
        bias_summary_  = bias.get("summary", "")
        headline_count = int(normalised.get("headline_count", 0))

        if briefing is None:
            briefing = DailyBriefing(
                trading_date=trading_date,
                created_at=now,
                bias_direction=bias_direction,
                bias_score=bias_score,
                bias_strength=bias_strength,
                bias_summary=bias_summary_,
                fii_net=fii_net,
                dii_net=dii_net,
                headline_count=headline_count,
            )
            session.add(briefing)
        else:
            briefing.bias_direction = bias_direction
            briefing.bias_score     = bias_score
            briefing.bias_strength  = bias_strength
            briefing.bias_summary   = bias_summary_
            briefing.fii_net        = fii_net
            briefing.dii_net        = dii_net
            briefing.headline_count = headline_count

        await session.flush()   # resolves briefing.id for child rows

        # Replace child rows on upsert — use direct DELETE to avoid async lazy-load
        await session.execute(delete(Headline).where(Headline.briefing_id == briefing.id))
        await session.execute(delete(StockInPlay).where(StockInPlay.briefing_id == briefing.id))

        # Build claude analysis map: headline_id -> {sentiment, importance, reason}
        claude_map: dict[int, dict] = {
            int(h["id"]): h
            for h in (analysis.get("headlines") or [])
            if "id" in h
        }

        # Insert Headline rows
        for h in normalised.get("headlines") or []:
            claude_entry = claude_map.get(int(h.get("id") or 0), {})
            scraped_at_raw = h.get("scraped_at")
            try:
                scraped_at = datetime.fromisoformat(str(scraped_at_raw)) if scraped_at_raw else None
            except ValueError:
                scraped_at = None

            raw_symbols = claude_entry.get("symbols")
            symbols = raw_symbols if isinstance(raw_symbols, list) else []

            session.add(Headline(
                briefing_id = briefing.id,
                source      = str(h.get("source") or ""),
                headline    = str(h.get("headline") or ""),
                url         = h.get("url"),
                sentiment   = claude_entry.get("sentiment"),
                importance  = claude_entry.get("importance"),
                reason      = claude_entry.get("reason"),
                symbols     = symbols,
                scraped_at  = scraped_at,
                created_at  = now,
            ))

        # Insert StockInPlay rows
        for s in stocks:
            session.add(StockInPlay(
                briefing_id   = briefing.id,
                rank          = int(s.get("rank", 0)),
                symbol        = str(s.get("symbol", "")),
                score         = float(s.get("score", 0.0)),
                sentiment     = str(s.get("sentiment", "unknown")),
                setup_type    = str(s.get("setup_type", "watchlist")),
                thesis        = str(s.get("thesis", "")),
                mention_count = int(s.get("mention_count", 0)),
                created_at    = now,
            ))

        await session.flush()
        briefing_id: int = briefing.id

    logger.info(
        f"Briefing saved: id={briefing_id}, trading_date={trading_date}, "
        f"headlines={len(normalised.get('headlines') or [])}, "
        f"stocks={len(stocks)}"
    )
    return briefing_id


# ── internal helpers ---------------------------------------------------------

import re as _re

_CRORE_RE = _re.compile(r"(FII|DII):\s*([+-]?\d+(?:\.\d+)?)\s*cr", _re.IGNORECASE)


def _parse_crore(summary: str, label: str) -> float | None:
    """Extract FII or DII net crore from the fii_dii_summary string."""
    for m in _CRORE_RE.finditer(summary or ""):
        if m.group(1).upper() == label.upper():
            try:
                return float(m.group(2))
            except ValueError:
                return None
    return None


# ── standalone init ----------------------------------------------------------

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    from utils.logger import setup_logger
    setup_logger("INFO")

    asyncio.run(init_db())
    print("Tables created")
