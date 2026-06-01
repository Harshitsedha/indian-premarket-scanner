"""
SQLAlchemy 2.0 ORM models for PreMarket Pro.
Pure schema definition — no settings, no I/O.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── daily_briefings ───────────────────────────────────────────────────────────

class DailyBriefing(Base):
    __tablename__ = "daily_briefings"

    id:             Mapped[int]           = mapped_column(Integer, primary_key=True)
    trading_date:   Mapped[date]          = mapped_column(Date, unique=True, nullable=False)
    bias_direction: Mapped[str]           = mapped_column(String(16), nullable=False)
    bias_score:     Mapped[float]         = mapped_column(Float, nullable=False)
    bias_strength:  Mapped[str]           = mapped_column(String(16), nullable=False)
    bias_summary:   Mapped[str]           = mapped_column(Text, nullable=False)
    fii_net:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dii_net:        Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    headline_count: Mapped[int]           = mapped_column(Integer, nullable=False)
    created_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)

    headlines:     Mapped[list["Headline"]]    = relationship("Headline",    back_populates="briefing", cascade="all, delete-orphan")
    stocks_in_play: Mapped[list["StockInPlay"]] = relationship("StockInPlay", back_populates="briefing", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_daily_briefings_trading_date", "trading_date"),
    )


# ── headlines ─────────────────────────────────────────────────────────────────

class Headline(Base):
    __tablename__ = "headlines"

    id:          Mapped[int]            = mapped_column(Integer, primary_key=True)
    briefing_id: Mapped[int]            = mapped_column(ForeignKey("daily_briefings.id", ondelete="CASCADE"), nullable=False)
    source:      Mapped[str]            = mapped_column(String(64), nullable=False)
    headline:    Mapped[str]            = mapped_column(Text, nullable=False)
    url:         Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    sentiment:   Mapped[Optional[str]]  = mapped_column(String(16), nullable=True)
    importance:  Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    reason:      Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    scraped_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)

    briefing: Mapped["DailyBriefing"] = relationship("DailyBriefing", back_populates="headlines")


# ── stocks_in_play ────────────────────────────────────────────────────────────

class StockInPlay(Base):
    __tablename__ = "stocks_in_play"

    id:            Mapped[int]           = mapped_column(Integer, primary_key=True)
    briefing_id:   Mapped[int]           = mapped_column(ForeignKey("daily_briefings.id", ondelete="CASCADE"), nullable=False)
    rank:          Mapped[int]           = mapped_column(Integer, nullable=False)
    symbol:        Mapped[str]           = mapped_column(String(16), nullable=False)
    score:         Mapped[float]         = mapped_column(Float, nullable=False)
    sentiment:     Mapped[str]           = mapped_column(String(16), nullable=False)
    setup_type:    Mapped[str]           = mapped_column(String(32), nullable=False)
    thesis:        Mapped[str]           = mapped_column(Text, nullable=False)
    mention_count: Mapped[int]           = mapped_column(Integer, nullable=False)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)

    briefing:      Mapped["DailyBriefing"]        = relationship("DailyBriefing", back_populates="stocks_in_play")
    setup_results: Mapped[list["SetupResult"]]    = relationship("SetupResult", back_populates="stock_in_play", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_stocks_in_play_symbol", "symbol"),
    )


# ── setup_results ─────────────────────────────────────────────────────────────

class SetupResult(Base):
    __tablename__ = "setup_results"

    id:               Mapped[int]            = mapped_column(Integer, primary_key=True)
    stock_in_play_id: Mapped[int]            = mapped_column(ForeignKey("stocks_in_play.id", ondelete="CASCADE"), nullable=False)
    open_price:       Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high:             Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low:              Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close:            Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gap_pct:          Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    result:           Mapped[str]             = mapped_column(String(16), nullable=False, default="pending")
    notes:            Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    recorded_at:      Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    stock_in_play: Mapped["StockInPlay"] = relationship("StockInPlay", back_populates="setup_results")
