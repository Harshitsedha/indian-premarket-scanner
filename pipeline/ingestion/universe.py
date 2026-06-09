"""
pipeline/ingestion/universe.py — Radar universe management.

Sources the radar universe from pipeline/data/tagging_universe.txt — the same
curated symbol list already used by the backtester and headline-tagging system.
Maps symbols to Upstox instrument_keys and persists in DB table `radar_universe`.
Refreshed weekly (or on force=True) so instrument-key changes are picked up.

Manual additions: pipeline/config/universe_override.json (set _enabled: true).

Public API:
    refresh_universe(force=False)  -> None
    get_universe()                 -> list[dict]   # [{symbol, instrument_key, index_membership}, ...]
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from loguru import logger

from utils.config import settings
from ingestion.upstox_instruments import get_instrument_token

_OVERRIDE_PATH = Path(__file__).resolve().parents[1] / "config" / "universe_override.json"
_REFRESH_AFTER = timedelta(days=7)

_MEMBERSHIP = "BACKTESTER"   # all symbols sourced from the backtester tagging list


# ── helpers ───────────────────────────────────────────────────────────────────

def _db():
    return psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        dbname=settings.postgres_db, user=settings.postgres_user,
        password=settings.postgres_password,
    )


def _load_tagging_universe() -> list[str]:
    """
    Read pipeline/data/tagging_universe.txt — the single source of truth for tradeable
    NSE symbols shared by the backtester, headline tagger, and radar screener.
    Returns a deduplicated, sorted list of uppercase symbols.
    """
    from processing.tagging_universe import TAGGING_UNIVERSE_SET
    symbols = sorted(TAGGING_UNIVERSE_SET)
    logger.info(f"universe: loaded {len(symbols)} symbols from tagging_universe.txt")
    return symbols


def _load_override() -> list[str]:
    """Return extra symbols from universe_override.json. Returns [] if disabled or missing."""
    if not _OVERRIDE_PATH.exists():
        return []
    try:
        data = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
        if not data.get("_enabled"):
            return []
        extras = [str(s).strip().upper() for s in data.get("symbols", []) if str(s).strip()]
        if extras:
            logger.info(f"universe: override enabled — {len(extras)} extra symbols")
        return extras
    except Exception as exc:
        logger.warning(f"universe: override JSON error ({_OVERRIDE_PATH}): {exc}")
        return []


def _needs_refresh() -> bool:
    """True if radar_universe is empty or every row is older than _REFRESH_AFTER."""
    try:
        conn = _db()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT MIN(updated_at) FROM radar_universe")
            row = cur.fetchone()
        conn.close()
        if not row or row[0] is None:
            return True
        oldest = row[0]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - oldest) > _REFRESH_AFTER
    except Exception as exc:
        logger.warning(f"universe: DB staleness check failed: {exc}")
        return True


def _get_db_universe() -> list[dict]:
    """Read all rows from radar_universe. Returns [] on any DB error."""
    try:
        conn = _db()
        rows = []
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, instrument_key, index_membership "
                "FROM radar_universe ORDER BY symbol"
            )
            for symbol, instrument_key, membership in cur.fetchall():
                rows.append({
                    "symbol":           symbol,
                    "instrument_key":   instrument_key,
                    "index_membership": membership,
                })
        conn.close()
        return rows
    except Exception as exc:
        logger.error(f"universe: DB read failed: {exc}")
        return []


def _upsert_universe(rows: list[dict]) -> None:
    if not rows:
        return
    conn = _db()
    try:
        with conn, conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO radar_universe
                        (symbol, instrument_key, index_membership, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (symbol) DO UPDATE
                      SET instrument_key   = EXCLUDED.instrument_key,
                          index_membership = EXCLUDED.index_membership,
                          updated_at       = NOW()
                    """,
                    (r["symbol"], r["instrument_key"], r["index_membership"]),
                )
    finally:
        conn.close()
    logger.info(f"universe: upserted {len(rows)} rows into radar_universe")


# ── public API ────────────────────────────────────────────────────────────────

def refresh_universe(force: bool = False) -> None:
    """
    Populate radar_universe from the backtester tagging list (tagging_universe.txt).
    Maps each symbol to an Upstox instrument_key; logs and skips unmappable symbols.
    Skips the DB write if the cache is < 7 days old (override with force=True).
    """
    if not force and not _needs_refresh():
        logger.info("universe: cache is fresh (< 7 days old), skipping refresh")
        return

    symbols = _load_tagging_universe()

    # Apply manual override (additive — does not remove existing symbols)
    override_syms = _load_override()
    all_symbols: dict[str, str] = {sym: _MEMBERSHIP for sym in symbols}
    for sym in override_syms:
        if sym not in all_symbols:
            all_symbols[sym] = "OVERRIDE"

    # Map every symbol to an Upstox instrument_key
    mapped: list[dict] = []
    skipped: list[str] = []
    for sym, membership in all_symbols.items():
        key = get_instrument_token(sym)
        if key:
            mapped.append({"symbol": sym, "instrument_key": key, "index_membership": membership})
        else:
            skipped.append(sym)

    if skipped:
        logger.warning(
            f"universe: {len(skipped)} symbols not found in NSE instrument master "
            f"(skipped): {skipped}"
        )

    _upsert_universe(mapped)
    logger.info(
        f"universe: refresh complete — {len(mapped)} mapped, {len(skipped)} skipped"
    )


def get_universe() -> list[dict]:
    """Return all rows from radar_universe as [{symbol, instrument_key, index_membership}]."""
    return _get_db_universe()
