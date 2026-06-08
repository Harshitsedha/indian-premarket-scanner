"""
Schema verification script — synthetic candles, no DB or Upstox needed.
Uses GapAndGo(opening_range_min=3, entry_window_min=10) so we only need a
handful of bars per day while still exercising the real engine signal path.

Verifies the new CSV columns:
  symbol, entry_date, entry_time, entry_price,
  exit_date, exit_time, exit_price,
  side, gap_pct, bars_held, pnl_abs, pnl_pct, mfe_pct, mae_pct, exit_reason
"""
import csv
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import pandas as pd
from backtest.strategy import GapAndGo, Action, Signal
from backtest.engine import run as engine_run
from backtest.recorder import write_csv

EXPECTED_COLS = [
    "symbol", "entry_date", "entry_time", "entry_price",
    "exit_date", "exit_time", "exit_price", "side", "gap_pct",
    "bars_held", "pnl_abs", "pnl_pct", "mfe_pct", "mae_pct", "exit_reason",
]

BASE_MIN = 9 * 60 + 15   # 09:15 as minutes from midnight


def make_candles(days_data):
    """days_data: list of (date_str, list_of_(o,h,l,c,v))"""
    rows = []
    for day_str, bars in days_data:
        for i, (o, h, l, c, v) in enumerate(bars):
            m = BASE_MIN + i
            ts = pd.Timestamp(f"{day_str} {m // 60:02d}:{m % 60:02d}:00")
            rows.append({
                "timestamp": ts,
                "date":      day_str,
                "open": o, "high": h, "low": l, "close": c, "volume": v,
            })
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


# GapAndGo with opening_range_min=3 so we need >=3 bars for OR to lock.
# After bar[2] the OR is established; bar[3] can trigger the breakout.
# Fill happens at bar[4]'s open.  stop = OR low, target = fill + 2*(fill-stop).
STRAT_PARAMS = dict(opening_range_min=3, entry_window_min=10, eod_exit=True)

# Prev-day close = 100.0
day_prev = ("2026-05-11", [(99.5, 100.5, 99.0, 100.0, 8000)])

# Day 1 — target_hit intraday
# OR bars[0-2]: high max = 103.5, low min = 102.8 (OR high / OR low)
# bar[3]: high=104.0 > 103.5 -> SIGNAL; fill at bar[4] open=103.8
# fill=103.8, stop=OR_low=102.8, target=103.8+2*(103.8-102.8)=105.8
# bar[5]: high=106.0 > 105.8 -> target_hit
day1 = ("2026-05-12", [
    (103.0, 103.4, 102.8, 103.2, 10000),  # bar0: gap open, OR bar1
    (103.2, 103.5, 103.0, 103.3, 9000),   # bar1: OR bar2
    (103.3, 103.5, 103.0, 103.4, 8500),   # bar2: OR bar3 (OR locked: high=103.5 low=102.8)
    (103.4, 104.0, 103.3, 103.9, 11000),  # bar3: high=104.0 > 103.5 -> SIGNAL
    (103.8, 104.2, 103.7, 104.0, 12000),  # bar4: fill@open=103.8
    (104.0, 106.0, 103.9, 105.8, 15000),  # bar5: high=106.0 > target=105.8 -> target_hit
])

# Day 2 — eod_exit; prev_close = day1 last close = 105.8
# gap = (110.0 - 105.8) / 105.8 * 100 = 3.97%
# OR: bars[0-2] high=110.6 low=109.8
# bar[3]: high=111.0 > 110.6 -> SIGNAL; fill bar[4] open=110.9
# fill=110.9, stop=109.8, target=110.9+2*(110.9-109.8)=113.1
# bar[4] is last bar of day -> eod_exit (target not reached)
day2 = ("2026-05-13", [
    (110.0, 110.3, 109.8, 110.2, 11000),  # bar0: gap open, OR bar1
    (110.2, 110.5, 110.0, 110.3, 9500),   # bar1: OR bar2
    (110.3, 110.6, 110.1, 110.4, 8800),   # bar2: OR bar3 (high=110.6 low=109.8)
    (110.4, 111.0, 110.3, 110.8, 12000),  # bar3: high=111.0 > 110.6 -> SIGNAL
    (110.9, 111.3, 110.8, 111.0, 13000),  # bar4: fill@110.9; last bar -> eod_exit
])

# Day 3 — target_hit intraday (second day); prev_close = 111.0
# gap = (114.0 - 111.0) / 111.0 = 2.7%
day3 = ("2026-05-14", [
    (114.0, 114.3, 113.8, 114.2, 10000),  # bar0: OR bar1
    (114.2, 114.5, 114.0, 114.3, 9000),   # bar1: OR bar2
    (114.3, 114.5, 114.1, 114.4, 8500),   # bar2: OR bar3 (high=114.5 low=113.8)
    (114.4, 115.0, 114.3, 114.9, 11000),  # bar3: high=115.0 > 114.5 -> SIGNAL
    (114.8, 115.2, 114.7, 115.0, 12000),  # bar4: fill@114.8; stop=113.8; tgt=116.8
    (115.0, 117.0, 114.9, 116.8, 14000),  # bar5: high=117.0 > target=116.8 -> target_hit
])

candles_eod = make_candles([day_prev, day1, day2, day3])
strat_eod = GapAndGo(**STRAT_PARAMS)
trades_eod = engine_run(candles_eod, strat_eod, "TATASTEEL")
print(f"eod_exit=True  -> {len(trades_eod)} trades")
for t in trades_eod:
    print(f"  entry={t.entry_date} {t.entry_time}  exit={t.exit_date} {t.exit_time}  reason={t.exit_reason}  bars={t.bars_held}")

# --- Cross-day trade: eod_exit=False, entry day2, stop_hit day3 ---
# fill day2-bar4 = 110.9, stop = OR_low = 109.8
# day3: low on bar1 = 109.6 < 109.8 -> stop_hit on day3
day2_x = ("2026-05-13", [
    (110.0, 110.3, 109.8, 110.2, 11000),  # bar0 OR1
    (110.2, 110.5, 110.0, 110.3, 9500),   # bar1 OR2
    (110.3, 110.6, 110.1, 110.4, 8800),   # bar2 OR3 (high=110.6 low=109.8)
    (110.4, 111.0, 110.3, 110.8, 12000),  # bar3 signal
    (110.9, 111.3, 110.8, 111.0, 13000),  # bar4 fill@110.9; last bar — eod_exit=False stays open
])
day3_x = ("2026-05-14", [
    (111.0, 111.4, 110.8, 111.2, 10000),  # bar0 no exit
    (111.1, 111.5, 109.6, 111.3, 9000),   # bar1 low=109.6 < stop=109.8 -> stop_hit
])

candles_xday = make_candles([day_prev, day2_x, day3_x])
strat_noeod = GapAndGo(**{**STRAT_PARAMS, "eod_exit": False})
trades_noeod = engine_run(candles_xday, strat_noeod, "TATASTEEL")
print(f"\neod_exit=False -> {len(trades_noeod)} trades")
for t in trades_noeod:
    cross = t.entry_date != t.exit_date
    print(f"  entry={t.entry_date} {t.entry_time}  exit={t.exit_date} {t.exit_time}  reason={t.exit_reason}  bars={t.bars_held}  cross_day={cross}")

# --- Write combined CSV and inspect ---
all_trades = trades_eod + trades_noeod
if not all_trades:
    print("\nERROR: no trades generated — check bar construction above")
    sys.exit(1)

out = pathlib.Path(__file__).parent.parent / "results" / "schema_verify.csv"
out.parent.mkdir(exist_ok=True)
write_csv(all_trades, out)
print(f"\nCSV written: {out}")

with open(out, newline="", encoding="utf-8") as fh:
    reader = csv.reader(fh)
    header = next(reader)
    data_rows = [next(reader, None) for _ in range(3)]

date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
time_re = re.compile(r"^\d{2}:\d{2}:\d{2}$")

print("\n-- Header --")
print(",".join(header))

match = header == EXPECTED_COLS
print(f"\nSchema match: {'PASS' if match else 'FAIL'}")
if not match:
    print(f"  expected: {EXPECTED_COLS}")
    print(f"  got:      {header}")

print("\n-- First 3 data rows --")
for r in data_rows:
    if r:
        print(",".join(r))

print("\n-- Date/time format checks --")
for r in data_rows:
    if r is None:
        continue
    sym, e_d, e_t, e_p, x_d, x_t = r[0], r[1], r[2], r[3], r[4], r[5]
    ok = (date_re.match(e_d) and time_re.match(e_t)
          and date_re.match(x_d) and time_re.match(x_t))
    print(f"  {sym}  entry={e_d} {e_t}  exit={x_d} {x_t}  {'OK' if ok else 'BAD FORMAT'}")

# Cross-day check
print("\n-- Cross-day trade check --")
cross_trades = [t for t in all_trades if t.entry_date != t.exit_date]
if cross_trades:
    for t in cross_trades:
        print(f"  FOUND cross-day: entry={t.entry_date} exit={t.exit_date}  "
              f"exit_reason={t.exit_reason}  bars_held={t.bars_held}")
else:
    print("  NONE (all trades same-day) -- check eod_exit=False candles")
