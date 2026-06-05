"""
CLI entry point for the bar-by-bar backtester.

Single-symbol mode:
    python pipeline/backtest/run.py \\
        --symbol TATASTEEL --start 2026-05-05 --end 2026-06-04
        [--interval minutes/1] [--strategy gap_and_go] [--out path]
        [--ca-jump-pct 20.0] [--strict-ca]
        [--record --features "gap_pct,or_width:n_min=15,vol_ratio:lookback=20"]

Multi-symbol mode (full SCAN_WATCHLIST):
    python pipeline/backtest/run.py \\
        --multi --start 2026-05-01 --end 2026-06-04
        [--interval minutes/1] [--strategy gap_and_go]
        [--ca-jump-pct 20.0] [--ca-ack | --strict-ca]
        [--record --features "gap_pct,or_width:n_min=15"]

--multi runs two phases:
  Phase 1 - CA sweep: fetch+cache all 50 symbols, run scan_ca_jumps on each,
             write results/ca_report_{ts}.csv. If any events are found the run
             stops here unless --ca-ack (proceed with all) or --strict-ca
             (exclude flagged symbols, proceed with rest) is also given.
  Phase 2 - Backtest loop: replay engine over every symbol in the universe,
             accumulate all trades, write a single combined CSV, then print
             an expectancy summary.

--record emits one row per qualifying setup-day with no-look-ahead features
and the engine's realized outcome. Output: results/candidates_{strategy}_...csv
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import date, datetime
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from loguru import logger
from utils.logger import setup_logger

from backtest.data import get_candles, scan_ca_jumps
from backtest.engine import run as engine_run
from backtest.features import parse_features
from backtest.metrics import compute_summary, print_summary, write_summary
from backtest.record import run_and_record, write_candidates
from backtest.recorder import _RESULTS_DIR, write_csv
from backtest.strategy import GapAndGo
from ingestion.upstox_instruments import get_instrument_token

_STRATEGIES = {
    "gap_and_go": GapAndGo,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bar-by-bar backtester -- single-symbol or full watchlist",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode: exactly one of --symbol / --multi required
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--symbol", default=None,
                      help="NSE ticker for single-symbol mode, e.g. TATASTEEL")
    mode.add_argument("--multi",  action="store_true",
                      help="Run over the full SCAN_WATCHLIST (50 symbols)")

    # Date range & interval (shared)
    p.add_argument("--start",    required=True,       help="YYYY-MM-DD")
    p.add_argument("--end",      required=True,       help="YYYY-MM-DD")
    p.add_argument("--interval", default="minutes/1", help="e.g. minutes/1 (default)")
    p.add_argument("--strategy", default="gap_and_go",
                   choices=list(_STRATEGIES.keys()),  help="Strategy name")

    # Single-symbol only
    p.add_argument("--out", default=None, help="Override output CSV path (single mode)")

    # Corporate-action guard (both modes)
    p.add_argument("--ca-jump-pct", type=float, default=20.0, dest="ca_jump_pct",
                   help="Flag transitions with |jump| > this %% (default 20.0)")
    p.add_argument("--strict-ca", action="store_true", dest="strict_ca",
                   help="Single: abort on CA event. Multi: exclude flagged symbols.")
    p.add_argument("--ca-ack", action="store_true", dest="ca_ack",
                   help="[multi] Acknowledge CA events and proceed with full universe.")

    # Candidate recorder (both modes)
    p.add_argument("--record", action="store_true",
                   help="Emit a candidates CSV with features + outcome per qualifying day")
    p.add_argument("--features", default=None, dest="features",
                   help=(
                       'Comma-separated feature names with optional param overrides. '
                       'Required when --record is set. '
                       'Example: "gap_pct,or_width:n_min=30,vol_ratio:lookback=20"'
                   ))

    p.add_argument("--log-level", default="INFO", dest="log_level")
    return p.parse_args(argv)


# ---- Feature validation ------------------------------------------------------

def _resolve_features(args: argparse.Namespace):
    """Validate and parse --features when --record is set. Returns list or None."""
    if not args.record:
        return None
    if not args.features:
        raise SystemExit("--features is required when --record is set.")
    try:
        return parse_features(args.features)
    except ValueError as exc:
        raise SystemExit(f"--features error: {exc}") from exc


# ---- Single-symbol mode ------------------------------------------------------

def _run_single(args: argparse.Namespace) -> None:
    symbol   = args.symbol.upper()
    start    = date.fromisoformat(args.start)
    end      = date.fromisoformat(args.end)
    interval = args.interval

    instrument_key = get_instrument_token(symbol)
    if not instrument_key:
        logger.error(f"Symbol not found in instrument master: {symbol}")
        sys.exit(1)
    logger.info(f"{symbol} instrument key: {instrument_key}")

    candles = get_candles(instrument_key, interval, start, end, symbol=symbol)
    if candles.empty:
        logger.error(f"No candles returned for {symbol} {start} to {end}, aborting")
        sys.exit(1)
    logger.info(f"Loaded {len(candles):,} candles: {start} to {end}")

    ca_events = scan_ca_jumps(candles, symbol=symbol, ca_jump_pct=args.ca_jump_pct)
    if ca_events and args.strict_ca:
        logger.error(
            f"{symbol}: {len(ca_events)} potential corporate-action jump(s) detected. "
            "Aborting due to --strict-ca."
        )
        sys.exit(2)

    strategy_cls      = _STRATEGIES[args.strategy]
    strategy          = strategy_cls()
    resolved_features = _resolve_features(args)
    logger.info(f"Strategy: {args.strategy}")

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if resolved_features is not None:
        # --record mode: emit candidate CSV only
        trades, candidates = run_and_record(candles, strategy, symbol, resolved_features)
        logger.info(f"Engine complete: {len(trades)} trades, {len(candidates)} candidates")

        cand_path = _RESULTS_DIR / f"candidates_{args.strategy}_{start}_{end}_{run_ts}.csv"
        write_candidates(candidates, resolved_features, cand_path)

        taken = sum(1 for c in candidates if c.taken)
        flat  = len(candidates) - taken
        print(f"\n{symbol} | {args.strategy} | {start} to {end} | --record")
        print(f"Qualifying setups: {len(candidates)}  ({taken} taken, {flat} flat)")
        print(f"Candidates CSV: {cand_path}")
        return

    trades = engine_run(candles, strategy, symbol)
    logger.info(f"Engine complete: {len(trades)} closed trades")

    if not trades:
        print(f"\n{symbol} | {args.strategy} | {start} to {end}")
        print("No trades generated.")
        return

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = _RESULTS_DIR / f"{symbol}_{args.strategy}_{run_ts}.csv"
    write_csv(trades, out_path)

    summary = compute_summary(trades)
    label   = f"{symbol} | {args.strategy} | {start} to {end} | {interval}"
    print()
    print_summary(summary, label=label)
    print(f"\nCSV: {out_path}")


# ---- Multi-symbol mode -------------------------------------------------------

def _run_multi(args: argparse.Namespace) -> None:
    from processing.ranker import SCAN_WATCHLIST

    run_ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    start        = date.fromisoformat(args.start)
    end          = date.fromisoformat(args.end)
    interval     = args.interval
    strategy_cls = _STRATEGIES[args.strategy]
    n_sym        = len(SCAN_WATCHLIST)

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Phase 1: CA sweep ---------------------------------------------------
    print(f"\nCA sweep  |  {n_sym} symbols  |  {start} to {end}  "
          f"|  threshold {args.ca_jump_pct:.0f}%")
    print("-" * 62)

    all_ca_events:      list[dict] = []
    ca_flagged_symbols: set[str]   = set()

    for idx, symbol in enumerate(SCAN_WATCHLIST, 1):
        tag = f"  CA [{idx:2d}/{n_sym}] {symbol:12s}"
        instrument_key = get_instrument_token(symbol)
        if not instrument_key:
            print(f"{tag} SKIP (not in instrument master)")
            continue
        try:
            candles = get_candles(instrument_key, interval, start, end, symbol=symbol)
        except Exception as exc:
            logger.error(f"CA fetch error {symbol}: {exc}")
            print(f"{tag} ERROR during fetch")
            continue
        if candles.empty:
            print(f"{tag} no data")
            continue

        events = scan_ca_jumps(candles, symbol=symbol, ca_jump_pct=args.ca_jump_pct)
        if events:
            ca_flagged_symbols.add(symbol)
            all_ca_events.extend(events)
            print(f"{tag} {len(events)} CA event(s) flagged")
        else:
            print(f"{tag} ok")

    # Write CA report CSV (always, even if empty, so there's an artefact)
    ca_path = _RESULTS_DIR / f"ca_report_{run_ts}.csv"
    with open(ca_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["symbol", "date", "prev_close", "curr_open", "jump_pct"],
        )
        w.writeheader()
        w.writerows(all_ca_events)

    print()
    if all_ca_events:
        print(f"CA summary: {len(all_ca_events)} event(s) across "
              f"{len(ca_flagged_symbols)} symbol(s)  ->  {ca_path.name}")
        for ev in all_ca_events:
            print(f"  {ev['symbol']:12s}  {ev['date']}  {ev['jump_pct']:+.1f}%")
    else:
        print(f"CA sweep complete: no jumps above {args.ca_jump_pct:.0f}% threshold.")
        print(f"Report written to: {ca_path.name}")

    # CA gate: stop here unless the caller has explicitly acknowledged
    if all_ca_events and not args.ca_ack and not args.strict_ca:
        print()
        print("Pass --ca-ack to acknowledge and backtest the full universe,")
        print("or   --strict-ca to exclude flagged symbols and proceed.")
        return

    # Determine backtest universe
    if args.strict_ca and ca_flagged_symbols:
        universe = [s for s in SCAN_WATCHLIST if s not in ca_flagged_symbols]
        print(f"\nStrict-CA: {len(ca_flagged_symbols)} symbol(s) excluded: "
              f"{', '.join(sorted(ca_flagged_symbols))}")
    else:
        universe = list(SCAN_WATCHLIST)

    resolved_features = _resolve_features(args)

    # ---- Phase 2: Backtest loop ----------------------------------------------
    n_u = len(universe)
    mode_label = "--record" if resolved_features is not None else "backtest"
    print(f"\n{mode_label.capitalize()}  |  {n_u} symbols  |  {args.strategy}")
    print("-" * 62)

    all_trades:     list = []
    all_candidates: list = []

    for idx, symbol in enumerate(universe, 1):
        tag = f"  [{idx:2d}/{n_u}] {symbol:12s}"
        instrument_key = get_instrument_token(symbol)
        if not instrument_key:
            print(f"{tag} SKIP (not in instrument master)")
            continue
        try:
            candles = get_candles(instrument_key, interval, start, end, symbol=symbol)
        except Exception as exc:
            logger.error(f"Candle load error {symbol}: {exc}")
            print(f"{tag} ERROR")
            continue
        if candles.empty:
            print(f"{tag} no data")
            continue
        try:
            strategy = strategy_cls()      # fresh instance per symbol
            if resolved_features is not None:
                trades, candidates = run_and_record(candles, strategy, symbol, resolved_features)
                all_candidates.extend(candidates)
                print(f"{tag} {len(trades)} trades, {len(candidates)} candidates")
            else:
                trades = engine_run(candles, strategy, symbol)
                print(f"{tag} {len(trades)} trades")
        except Exception as exc:
            logger.error(f"Engine error {symbol}: {exc}")
            print(f"{tag} ENGINE ERROR")
            continue

        all_trades.extend(trades)

    # ---- Record mode output --------------------------------------------------
    if resolved_features is not None:
        if not all_candidates:
            print("\nNo qualifying candidates found.")
            return

        cand_path = _RESULTS_DIR / f"candidates_{args.strategy}_{start}_{end}_{run_ts}.csv"
        write_candidates(all_candidates, resolved_features, cand_path)
        taken = sum(1 for c in all_candidates if c.taken)
        flat  = len(all_candidates) - taken
        print(f"\nQualifying setups: {len(all_candidates)}  ({taken} taken, {flat} flat)")
        print(f"Candidates CSV: {cand_path.name}")
        return

    # ---- Standard backtest output --------------------------------------------
    if not all_trades:
        print("\nNo trades generated across the universe.")
        return

    combined_path = _RESULTS_DIR / f"multi_{args.strategy}_{start}_{end}_{run_ts}.csv"
    write_csv(all_trades, combined_path)
    print(f"\nCombined CSV: {len(all_trades)} trades  ->  {combined_path.name}")

    summary = compute_summary(all_trades)
    label   = f"{args.strategy} | {start} to {end} | {n_u} symbols"
    print()
    print_summary(summary, label=label)

    summary_path = _RESULTS_DIR / f"summary_{run_ts}.txt"
    write_summary(summary, summary_path, label=label)
    print(f"\nSummary: {summary_path.name}")


# ── Importable public functions (worker calls these) ──────────────────────────

def _safe_summary(s: dict) -> dict:
    """Replace math.inf with None so the dict is JSON-serialisable."""
    return {
        k: (None if isinstance(v, float) and math.isinf(v) else v)
        for k, v in s.items()
    }


def run_single(
    symbol:      str,
    start:       str,
    end:         str,
    strategy:    str   = "gap_and_go",
    interval:    str   = "minutes/1",
    ca_jump_pct: float = 20.0,
    strict_ca:   bool  = False,
    features:    str | None = None,
) -> tuple[str, dict]:
    """
    Execute a single-symbol backtest (features=None) or record run (features set).

    Returns:
        (result_path_str, summary_dict)
    Raises:
        ValueError  — unknown strategy/symbol/feature
        RuntimeError — candles empty or strict_ca gate triggered
    """
    symbol = symbol.upper()

    if strategy not in _STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}. Available: {list(_STRATEGIES)}")

    resolved_features = None
    if features is not None:
        resolved_features = parse_features(features)   # raises ValueError on bad input

    start_d = date.fromisoformat(start)
    end_d   = date.fromisoformat(end)

    instrument_key = get_instrument_token(symbol)
    if not instrument_key:
        raise ValueError(f"Symbol not found in instrument master: {symbol}")

    candles = get_candles(instrument_key, interval, start_d, end_d, symbol=symbol)
    if candles.empty:
        raise RuntimeError(f"No candles returned for {symbol} {start_d}–{end_d}")

    ca_events = scan_ca_jumps(candles, symbol=symbol, ca_jump_pct=ca_jump_pct)
    if ca_events and strict_ca:
        raise RuntimeError(
            f"{symbol}: {len(ca_events)} CA event(s) detected — aborted (strict_ca)."
        )

    strategy_obj = _STRATEGIES[strategy]()
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if resolved_features is not None:
        trades, candidates = run_and_record(candles, strategy_obj, symbol, resolved_features)
        path = _RESULTS_DIR / f"candidates_{strategy}_{start}_{end}_{run_ts}.csv"
        write_candidates(candidates, resolved_features, path)
        taken = sum(1 for c in candidates if c.taken)
        summary: dict = {
            "mode":             "record",
            "total_candidates": len(candidates),
            "taken":            taken,
            "flat":             len(candidates) - taken,
            "features":         [rf.col_name for rf in resolved_features],
        }
    else:
        trades = engine_run(candles, strategy_obj, symbol)
        path   = _RESULTS_DIR / f"{symbol}_{strategy}_{run_ts}.csv"
        if trades:
            write_csv(trades, path)
        raw = compute_summary(trades)
        summary = {"mode": "run", **_safe_summary(raw)}

    return str(path), summary


def run_multi(
    start:       str,
    end:         str,
    strategy:    str   = "gap_and_go",
    interval:    str   = "minutes/1",
    ca_jump_pct: float = 20.0,
    strict_ca:   bool  = False,
    ca_ack:      bool  = True,
    features:    str | None = None,
) -> tuple[str, dict]:
    """
    Execute a multi-symbol backtest or record run over the full SCAN_WATCHLIST.

    Returns:
        (result_path_str, summary_dict)
    Raises:
        ValueError  — unknown strategy/feature
        RuntimeError — CA gate triggered without ca_ack
    """
    from processing.ranker import SCAN_WATCHLIST

    if strategy not in _STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}. Available: {list(_STRATEGIES)}")

    resolved_features = None
    if features is not None:
        resolved_features = parse_features(features)

    start_d      = date.fromisoformat(start)
    end_d        = date.fromisoformat(end)
    strategy_cls = _STRATEGIES[strategy]
    run_ts       = datetime.now().strftime("%Y%m%d_%H%M%S")

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # CA sweep (reuses cache; no double-fetch)
    all_ca_events:      list[dict] = []
    ca_flagged_symbols: set[str]   = set()

    for symbol in SCAN_WATCHLIST:
        instrument_key = get_instrument_token(symbol)
        if not instrument_key:
            continue
        try:
            candles = get_candles(instrument_key, interval, start_d, end_d, symbol=symbol)
        except Exception:
            continue
        if candles.empty:
            continue
        events = scan_ca_jumps(candles, symbol=symbol, ca_jump_pct=ca_jump_pct)
        if events:
            ca_flagged_symbols.add(symbol)
            all_ca_events.extend(events)

    ca_path = _RESULTS_DIR / f"ca_report_{run_ts}.csv"
    with open(ca_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["symbol", "date", "prev_close", "curr_open", "jump_pct"],
        )
        w.writeheader()
        w.writerows(all_ca_events)

    if all_ca_events and not ca_ack and not strict_ca:
        raise RuntimeError(
            f"{len(all_ca_events)} CA event(s) across {len(ca_flagged_symbols)} symbol(s). "
            "Pass ca_ack=True to proceed."
        )

    if strict_ca and ca_flagged_symbols:
        universe = [s for s in SCAN_WATCHLIST if s not in ca_flagged_symbols]
    else:
        universe = list(SCAN_WATCHLIST)

    all_trades:     list = []
    all_candidates: list = []

    for symbol in universe:
        instrument_key = get_instrument_token(symbol)
        if not instrument_key:
            continue
        try:
            candles = get_candles(instrument_key, interval, start_d, end_d, symbol=symbol)
        except Exception:
            continue
        if candles.empty:
            continue
        try:
            strat = strategy_cls()
            if resolved_features is not None:
                trades, candidates = run_and_record(candles, strat, symbol, resolved_features)
                all_candidates.extend(candidates)
            else:
                trades = engine_run(candles, strat, symbol)
            all_trades.extend(trades)
        except Exception as exc:
            logger.error(f"Engine error {symbol}: {exc}")

    if resolved_features is not None:
        path = _RESULTS_DIR / f"candidates_{strategy}_{start}_{end}_{run_ts}.csv"
        write_candidates(all_candidates, resolved_features, path)
        taken = sum(1 for c in all_candidates if c.taken)
        summary: dict = {
            "mode":             "record",
            "total_candidates": len(all_candidates),
            "taken":            taken,
            "flat":             len(all_candidates) - taken,
            "features":         [rf.col_name for rf in resolved_features],
        }
    else:
        path = _RESULTS_DIR / f"multi_{strategy}_{start}_{end}_{run_ts}.csv"
        if all_trades:
            write_csv(all_trades, path)
        raw = compute_summary(all_trades)
        summary = {"mode": "run", **_safe_summary(raw)}

    return str(path), summary


# ---- Entry point -------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    setup_logger(args.log_level)

    if args.multi:
        _run_multi(args)
    else:
        _run_single(args)


if __name__ == "__main__":
    main()
