"""
NSE data scraper — two public endpoints, no auth required.

Bhavcopy (previous day OHLCV):
  https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<DDMMYYYY>_F_0000.csv
  Note: NSE archives uses Akamai CDN. Requests succeed from cloud/VPS IPs;
        consumer/residential IPs may receive 404 due to bot-protection policies.

FII/DII activity:
  https://www.nseindia.com/api/fiidiiTradeReact
  Requires a valid NSE session cookie — seeded automatically by visiting the homepage.
"""

import io
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from loguru import logger

# ── constants ─────────────────────────────────────────────────────────────────

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv"
)
FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_HOME = "https://www.nseindia.com/market-data/live-equity-market"

# NSE requires a browser-like User-Agent and Referer on all requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

_BHAVCOPY_RENAME = {
    "TckrSymb": "symbol",
    "OpnPric": "open",
    "HghPric": "high",
    "LwPric": "low",
    "ClsPric": "close",
    "TtlTradgVol": "volume",
    "TtlTrfVal": "turnover",
}

MAX_ROWS = 2000


# ── date helpers ──────────────────────────────────────────────────────────────

def _prev_trading_day(from_date: date) -> date:
    """Step back one calendar day, then skip over Saturday/Sunday."""
    d = from_date - timedelta(days=1)
    while d.weekday() in (5, 6):   # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


# ── bhavcopy ──────────────────────────────────────────────────────────────────

def _parse_bhavcopy(csv_text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(csv_text))
    missing = [c for c in _BHAVCOPY_RENAME if c not in df.columns]
    if missing:
        raise ValueError(
            f"Unexpected Bhavcopy schema — missing: {missing}. "
            f"Got: {list(df.columns)}"
        )
    df = df[list(_BHAVCOPY_RENAME)].rename(columns=_BHAVCOPY_RENAME)
    # Keep only plain equity symbols: no hyphen, 10 chars or fewer
    df = df[df["symbol"].str.len() <= 10]
    df = df[~df["symbol"].str.contains("-", na=False)]
    return df.head(MAX_ROWS).reset_index(drop=True)


def _fetch_bhavcopy(
    start_date: date, client: httpx.Client
) -> tuple[list[dict[str, Any]], date]:
    """
    Try start_date; on failure fall back up to 2 earlier trading days
    to handle NSE holidays transparently.
    """
    current = start_date
    for attempt in range(3):
        url = BHAVCOPY_URL.format(date=current.strftime("%d%m%Y"))
        logger.info(f"Bhavcopy attempt {attempt + 1}: {current} -> {url}")
        try:
            resp = client.get(
                url,
                timeout=30.0,
                headers={**_HEADERS, "Referer": "https://www.nseindia.com"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                f"Bhavcopy {current} -> HTTP {exc.response.status_code}, "
                "trying earlier trading day"
            )
            current = _prev_trading_day(current)
            continue
        except Exception as exc:
            logger.error(f"Bhavcopy {current} fetch error: {exc}")
            current = _prev_trading_day(current)
            continue

        try:
            df = _parse_bhavcopy(resp.text)
            logger.info(f"Bhavcopy: {len(df)} equity rows for {current}")
            return df.to_dict(orient="records"), current
        except Exception as exc:
            logger.error(f"Bhavcopy parse error for {current}: {exc}")
            return [], current

    logger.error("Bhavcopy: all 3 attempts exhausted — check network/IP access to NSE archives")
    return [], current


# ── FII / DII ─────────────────────────────────────────────────────────────────

def _parse_crore(value: Any) -> float | None:
    """Parse NSE crore values — may be strings like '1,234.56' or '-358.02'."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _fetch_fiidii(client: httpx.Client) -> dict[str, Any]:
    """
    NSE's /api/ endpoints require a valid browser session cookie.
    Seeding is done by the caller before this is invoked.
    """
    empty: dict[str, Any] = {"fii_net": None, "dii_net": None}
    try:
        resp = client.get(
            FIIDII_URL,
            timeout=15.0,
            headers={**_HEADERS, "Accept": "application/json, text/plain, */*"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error(f"FII/DII fetch failed: {exc}")
        return empty

    try:
        fii_net: float | None = None
        dii_net: float | None = None

        rows: list = data if isinstance(data, list) else (
            data.get("data") or data.get("fiidiiData") or []
        )
        for row in rows:
            cat = str(row.get("category", "")).upper()
            net = row.get("netValue") or row.get("net") or row.get("NET")
            if "FII" in cat or "FPI" in cat:
                fii_net = _parse_crore(net)
            elif "DII" in cat:
                dii_net = _parse_crore(net)

        logger.info(f"FII/DII: FII net={fii_net} cr, DII net={dii_net} cr")
        return {"fii_net": fii_net, "dii_net": dii_net}

    except Exception as exc:
        logger.error(f"FII/DII parse failed: {exc}")
        return empty


# ── public API ────────────────────────────────────────────────────────────────

def fetch_nse_data() -> dict[str, Any]:
    """
    Fetch FII/DII activity from NSE and derive trading_date from Upstox.

    NSE bhavcopy (archives CDN) is skipped — it returns HTTP 404 from cloud
    VPS IPs due to Akamai bot-protection.  Previous-close per stock is sourced
    directly from Upstox in the ranker (get_bulk_quotes / get_prev_close).
    trading_date is now derived from the most recent Upstox candle date.

    Returns:
        {
            "bhavcopy":     [],            # disabled — use Upstox in ranker
            "fii_dii":      {"fii_net": 423.5, "dii_net": -210.3},
            "trading_date": "2026-06-01",  # from Upstox, not NSE archives
        }
    """
    # ── trading_date from Upstox ──────────────────────────────────────────────
    try:
        from ingestion.upstox_client import fetch_upstox_trading_date
        upstox_date = fetch_upstox_trading_date()
    except Exception as exc:
        logger.warning(f"fetch_upstox_trading_date import/call failed: {exc}")
        upstox_date = None

    if upstox_date:
        trading_date = date.fromisoformat(upstox_date)
    else:
        trading_date = _prev_trading_day(date.today())
        logger.warning(
            f"Upstox trading date unavailable — falling back to calendar estimate: {trading_date}"
        )

    logger.info(f"Trading date: {trading_date}")

    # ── FII/DII from NSE (this endpoint still works) ──────────────────────────
    with httpx.Client(
        headers=_HEADERS, follow_redirects=True, timeout=20.0
    ) as client:
        try:
            client.get(NSE_HOME)
            logger.debug(f"NSE session seeded (cookies: {list(client.cookies.keys())})")
        except Exception as exc:
            logger.warning(f"NSE session seed failed (continuing): {exc}")
        fii_dii = _fetch_fiidii(client)

    return {
        "bhavcopy": [],
        "fii_dii": fii_dii,
        "trading_date": trading_date.isoformat(),
    }


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    result = fetch_nse_data()

    print(f"\nTrading date  : {result['trading_date']}")
    print(f"Bhavcopy rows : {len(result['bhavcopy'])}")
    print(f"FII net (cr)  : {result['fii_dii']['fii_net']}")
    print(f"DII net (cr)  : {result['fii_dii']['dii_net']}")

    if result["bhavcopy"]:
        print("\nSample (first 3 rows):")
        for row in result["bhavcopy"][:3]:
            print(f"  {row}")
    else:
        print("\n[Bhavcopy] 0 rows -- NSE archives may be Akamai-blocked from this IP.")
        print("  This is expected on consumer/residential connections.")
        print("  Run from a VPS/cloud IP to get full bhavcopy data.")
