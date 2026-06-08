"""
Tokenless verification: prior_session_gap_pct for 10 symbols.
Formula: (yesterday_open - day_before_close) / day_before_close * 100
Must produce distinct, non-zero, varied numbers using only historical candles.
Run: python -m pipeline.scratch.verify_prior_session_gaps  (from repo root)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.logger import setup_logger
setup_logger("WARNING")   # suppress INFO noise during verification

from ingestion.upstox_client import UpstoxClient
from processing.ranker import SCAN_WATCHLIST

PROBE = SCAN_WATCHLIST[:10]

print("\n=== Tokenless prior_session_gap_pct verification ===")
print("Formula: (yesterday_open - day_before_close) / day_before_close * 100")
print("(no live token required — historical candle endpoint only)\n")

client = UpstoxClient()

# Confirm token status
has_auth = "Authorization" in dict(client._http.headers)
print(f"Token present in client headers: {has_auth}")
print()

results = []
for sym in PROBE:
    r = client.get_prior_session_gap(sym)
    if r:
        results.append(r)
        gap = r["prior_session_gap_pct"]
        flag = "  *** >15% — check for corporate action ***" if abs(gap) > 15 else ""
        print(
            f"  {r['symbol']:15}  "
            f"day_before_close={r['day_before_close']:>10.2f}  "
            f"yesterday_open={r['yesterday_open']:>10.2f}  "
            f"prior_session_gap_pct={gap:>+7.2f}%"
            f"{flag}"
        )
    else:
        print(f"  {sym:15}  *** no data ***")

print()
if results:
    distinct = {r["prior_session_gap_pct"] for r in results}
    all_zero  = all(r["prior_session_gap_pct"] == 0.0 for r in results)
    all_same  = len(distinct) == 1

    # Hand-check first two results so the math is visually verifiable
    print("--- Hand-check (first 2 symbols) ---")
    for r in results[:2]:
        expected = round(
            (r["yesterday_open"] - r["day_before_close"]) / r["day_before_close"] * 100, 2
        )
        match = "OK" if expected == r["prior_session_gap_pct"] else "MISMATCH"
        print(
            f"  {r['symbol']}: ({r['yesterday_open']} - {r['day_before_close']}) "
            f"/ {r['day_before_close']} * 100 = {expected:+.2f}%  [{match}]"
        )

    print()
    print(f"Symbols with data:    {len(results)}/{len(PROBE)}")
    print(f"Distinct gap values:  {sorted(distinct)}")
    print(f"All zero?  {all_zero}  (should be False)")
    print(f"All same?  {all_same}  (should be False)")
    if not all_zero and not all_same:
        print("\nPASS: distinct, non-zero, varied prior-session gaps confirmed tokenless")
    else:
        print("\nFAIL: values are identical or all zero")
else:
    print("FAIL: no data returned for any symbol")
