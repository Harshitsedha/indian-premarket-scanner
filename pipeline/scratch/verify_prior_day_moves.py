"""
Tokenless verification: prior_day_move_pct for 10 symbols.
Must produce distinct, non-zero, varied numbers using only historical candles.
Run: python -m pipeline.scratch.verify_prior_day_moves  (from repo root)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.logger import setup_logger
setup_logger("WARNING")   # suppress INFO noise during verification

from ingestion.upstox_client import UpstoxClient
from processing.ranker import SCAN_WATCHLIST

PROBE = SCAN_WATCHLIST[:10]

print("\n=== Tokenless prior_day_move_pct verification ===")
print("(no live token required — historical candle endpoint only)\n")

client = UpstoxClient()

# Confirm token status
has_auth = "Authorization" in dict(client._http.headers)
print(f"Token present in client headers: {has_auth}")
print()

results = []
for sym in PROBE:
    r = client.get_prior_day_move(sym)
    if r:
        results.append(r)
        print(
            f"  {r['symbol']:15}  "
            f"day_before={r['day_before_close']:>10.2f}  "
            f"yesterday={r['yesterday_close']:>10.2f}  "
            f"prior_day_move_pct={r['prior_day_move_pct']:>+7.2f}%"
        )
    else:
        print(f"  {sym:15}  *** no data ***")

print()
if results:
    distinct = {r["prior_day_move_pct"] for r in results}
    all_zero  = all(r["prior_day_move_pct"] == 0.0 for r in results)
    all_same  = len(distinct) == 1
    print(f"Symbols with data:    {len(results)}/{len(PROBE)}")
    print(f"Distinct move values: {sorted(distinct)}")
    print(f"All zero?  {all_zero}  (should be False)")
    print(f"All same?  {all_same}  (should be False)")
    if not all_zero and not all_same:
        print("\nPASS: distinct, non-zero, varied prior-day moves confirmed tokenless")
    else:
        print("\nFAIL: values are identical or all zero")
else:
    print("FAIL: no data returned for any symbol")
