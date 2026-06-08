"""
Diagnostic: trace raw per-symbol gap values from the live path.

Two modes:
  1. With a valid Upstox token: calls the real API and prints per-symbol output
  2. Without a token: runs a mock to verify the URL construction fix

Run: python -m pipeline.scratch.diag_gaps   (from repo root)
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.logger import setup_logger
setup_logger("INFO")

from processing.ranker import SCAN_WATCHLIST

PROBE = SCAN_WATCHLIST[:10]

# ── 1. Mock test: verify URL uses raw commas and per-symbol gaps are distinct ─

print("\n=== MOCK TEST: URL construction and per-symbol gap isolation ===")

# Fake prev_close and LTP data — each stock has a DIFFERENT gap
FAKE_PREV = {
    "RELIANCE": 1200.00, "TCS": 3000.00, "HDFCBANK": 1800.00, "INFY": 1400.00,
    "ICICIBANK": 700.00, "HINDUNILVR": 2500.00, "ITC": 430.00, "SBIN": 600.00,
    "BHARTIARTL": 1100.00, "KOTAKBANK": 1750.00,
}
FAKE_LTP_PCT = {  # deliberate distinct gaps: +2.0% down to -1.5%
    "RELIANCE": 2.00, "TCS": 1.50, "HDFCBANK": 1.00, "INFY": 0.50,
    "ICICIBANK": 0.00, "HINDUNILVR": -0.50, "ITC": -1.00, "SBIN": -1.50,
    "BHARTIARTL": 0.75, "KOTAKBANK": -0.25,
}

def fake_prev_close(sym):
    close = FAKE_PREV.get(sym.upper(), 1000.0)
    return {"symbol": sym.upper(), "close": close, "open": close, "high": close, "low": close, "volume": 0, "date": "2026-06-03"}

def fake_get_response(url):
    # Verify URL uses raw commas (not %2C)
    assert "%2C" not in url, f"BUG: URL still uses %2C as separator: {url}"
    print(f"  URL check passed — no %2C in URL")
    print(f"  URL (first 120 chars): {url[:120]}...")

    # Build fake response keyed by pipe-format instrument key
    from ingestion.upstox_instruments import get_instrument_token
    data = {}
    for sym, pct in FAKE_LTP_PCT.items():
        ikey = get_instrument_token(sym)
        if ikey:
            ltp = FAKE_PREV[sym] * (1 + pct / 100)
            data[ikey] = {"last_price": round(ltp, 2)}
    return {"status": "success", "data": data}

from ingestion.upstox_client import UpstoxClient
from ingestion.upstox_instruments import get_instrument_token

client = UpstoxClient.__new__(UpstoxClient)
client._http = MagicMock()

with patch.object(client, "get_prev_close", side_effect=fake_prev_close), \
     patch.object(client, "_get", side_effect=fake_get_response):
    raw = client.get_bulk_quotes(PROBE)

print(f"\n  Raw per-symbol output ({len(raw)} stocks):")
print(f"  {'Symbol':15}  {'prev_close':>12}  {'ltp':>10}  {'gap_pct':>8}  {'is_premarket'}")
for q in sorted(raw, key=lambda x: x["symbol"]):
    print(f"  {q['symbol']:15}  {q.get('prev_close', 'N/A'):>12}  {q.get('ltp', 'N/A'):>10}  {q.get('gap_pct', 'N/A'):>8}  {q.get('is_premarket')}")

distinct_gaps = {q["gap_pct"] for q in raw}
all_same = len(distinct_gaps) == 1
print(f"\n  Distinct gap_pct values: {sorted(distinct_gaps)}")
print(f"  All identical? {all_same}  (should be False = FIXED)")

if all_same:
    print("  FAIL: gaps are still identical -- bug not fully fixed")
else:
    print("  PASS: each stock has its own distinct gap value")

# ── 2. Live API test (only if token is configured) ────────────────────────────

print("\n=== LIVE API TEST (requires valid Upstox token) ===")
try:
    live_client = UpstoxClient()
    live_raw = live_client.get_bulk_quotes(PROBE)
    if not live_raw:
        print("  No data returned — token may be missing or market closed")
    else:
        print(f"\n  Raw per-symbol output ({len(live_raw)} stocks):")
        print(f"  {'Symbol':15}  {'prev_close':>12}  {'ltp':>10}  {'gap_pct':>8}  {'is_premarket'}")
        for q in sorted(live_raw, key=lambda x: x["symbol"]):
            print(f"  {q['symbol']:15}  {q.get('prev_close', 'N/A'):>12}  {q.get('ltp', 'N/A'):>10}  {q.get('gap_pct', 'N/A'):>8}  {q.get('is_premarket')}")
        live_distinct = {q["gap_pct"] for q in live_raw}
        print(f"\n  Distinct gap_pct values: {sorted(live_distinct)}")
        print(f"  All identical? {len(live_distinct) == 1}  (should be False)")
except Exception as e:
    print(f"  Skipped (no valid token): {e}")
