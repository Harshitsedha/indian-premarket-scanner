"""
Train→Test executor.

Step A: load train CSV, build statistical summary, call Claude → (rules_raw, rules_parsed)
Step B: commit rules to backtest_rulesets in DB — this MUST succeed before any test data
         is fetched. If DB write fails, raises immediately and test data is never touched.
Step C: fetch test-range candles (cached), run strategy, filter candidates through rules.
Step D: write result CSV, return (result_path, summary).

The # LOCKOUT BOUNDARY comment marks the exact line where Step C begins.
Auditors can grep this marker to verify Step B always precedes Step C.

Public API:
    result_path, summary = run_train_test(
        train_csv, test_symbol=None, test_multi=False,
        test_start, test_end, features, strategy="gap_and_go",
        interval="minutes/1", ca_jump_pct=20.0, strict_ca=False,
        ca_ack=True, db_conn=None,
    )

db_conn is injected by tests; production passes None (worker opens its own connection).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import psycopg2
from loguru import logger

from utils.config import settings
from backtest.summarise import build_summary
from backtest.claude_rules import get_rules
from backtest.rules import apply_rules
from backtest.features import parse_features, FEATURES
from backtest.engine import run as engine_run, ClosedTrade
from backtest.record import run_and_record
from backtest.metrics import compute_summary as compute_trade_summary
from backtest.recorder import _RESULTS_DIR, write_csv
from backtest.strategy import GapAndGo
from backtest.data import get_candles, scan_ca_jumps
from ingestion.upstox_instruments import get_instrument_token

_STRATEGIES = {"gap_and_go": GapAndGo}


def _open_db():
    return psycopg2.connect(
        host     = settings.postgres_host,
        port     = settings.postgres_port,
        dbname   = settings.postgres_db,
        user     = settings.postgres_user,
        password = settings.postgres_password,
    )


def _commit_ruleset(
    conn,
    job_id:        str,
    train_csv:     str,
    rules_raw:     str,
    rules_parsed:  dict,
    feature_stats: dict,
) -> str:
    """
    Write a backtest_rulesets row and return the new ruleset UUID.
    Raises on any DB error — caller must NOT proceed to test-range fetch on failure.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_rulesets
                    (job_id, train_csv, rules_raw, rules_parsed, feature_stats)
             VALUES (%s, %s, %s, %s, %s)
          RETURNING id, committed_at
            """,
            (
                job_id,
                train_csv,
                rules_raw,
                json.dumps(rules_parsed),
                json.dumps(feature_stats),
            ),
        )
        row = cur.fetchone()
        conn.commit()
    ruleset_id, committed_at = str(row[0]), row[1]
    logger.info(
        f"Ruleset {ruleset_id[:8]} committed at {committed_at.isoformat()} — "
        "test-range fetch may now begin"
    )
    return ruleset_id


def _human_readable_rules(rules_parsed: dict) -> list[str]:
    """Convert filters to short human-readable strings for the summary."""
    _DOW = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    chips = []
    for f in rules_parsed.get("filters", []):
        feat = f["feature"]
        op   = f["op"]
        if op == "between":
            chips.append(f"{feat} [{f['low']}–{f['high']}]")
        elif op == "lt":
            chips.append(f"{feat} < {f['value']}")
        elif op == "gt":
            chips.append(f"{feat} > {f['value']}")
        elif op == "lte":
            chips.append(f"{feat} ≤ {f['value']}")
        elif op == "gte":
            chips.append(f"{feat} ≥ {f['value']}")
        elif op == "in":
            vals = [_DOW.get(int(v), str(v)) for v in f["values"]] if feat == "day_of_week" else [str(v) for v in f["values"]]
            chips.append(f"{feat} ∈ {{{', '.join(vals)}}}")
        elif op == "not_in":
            vals = [_DOW.get(int(v), str(v)) for v in f["values"]] if feat == "day_of_week" else [str(v) for v in f["values"]]
            chips.append(f"{feat} ∉ {{{', '.join(vals)}}}")
    return chips


def _safe_summary(s: dict) -> dict:
    import math
    return {k: (None if isinstance(v, float) and math.isinf(v) else v) for k, v in s.items()}


def run_train_test(
    train_csv:    str,
    test_start:   str,
    test_end:     str,
    features:     str,
    job_id:       str = "",
    test_symbol:  str | None = None,
    test_multi:   bool = False,
    strategy:     str  = "gap_and_go",
    interval:     str  = "minutes/1",
    ca_jump_pct:  float = 20.0,
    strict_ca:    bool  = False,
    ca_ack:       bool  = True,
    db_conn       = None,      # injected in tests; None → open own connection
) -> tuple[str, dict]:
    """
    Execute a train→test run. Returns (result_path_str, summary_dict).
    Raises ValueError/RuntimeError on any validation or execution failure.
    """
    # ── Validate inputs ──────────────────────────────────────────────────────
    train_path = Path(train_csv)
    if not train_path.exists():
        raise ValueError(f"train_csv not found: {train_csv}")

    if strategy not in _STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}")

    resolved_features = parse_features(features)   # ValueError on bad feature name/param
    start_d = date.fromisoformat(test_start)
    end_d   = date.fromisoformat(test_end)
    if start_d > end_d:
        raise ValueError("test_start must be ≤ test_end")

    if not test_symbol and not test_multi:
        raise ValueError("Provide test_symbol or test_multi=True")
    if test_symbol and test_multi:
        raise ValueError("Provide test_symbol or test_multi=True, not both")

    strategy_cls = _STRATEGIES[strategy]

    # ── Step A: build summary, call Claude ───────────────────────────────────
    logger.info(f"Step A: building summary from {train_path.name}")
    feature_stats = build_summary(train_csv)
    logger.info(
        f"Summary: {feature_stats['dataset']['total_candidates']} candidates, "
        f"features={list(feature_stats['feature_stats'].keys())}"
    )

    rules_raw, rules_parsed = get_rules(feature_stats)
    logger.info(f"Claude returned {len(rules_parsed['filters'])} filters, confidence={rules_parsed['confidence']}")

    # ── Step B: commit ruleset — MUST complete before test-range fetch ───────
    logger.info("Step B: committing ruleset to DB")
    own_conn = db_conn is None
    conn     = db_conn if db_conn is not None else _open_db()
    try:
        ruleset_id = _commit_ruleset(
            conn, job_id, train_csv, rules_raw, rules_parsed, feature_stats
        )
    except Exception as exc:
        if own_conn:
            conn.close()
        raise RuntimeError(f"Ruleset DB commit failed — test range fetch aborted: {exc}") from exc

    # ── LOCKOUT BOUNDARY ─────────────────────────────────────────────────────
    # Step C begins here. The ruleset row (committed_at = NOW()) exists in the
    # DB before any test-range candle is fetched. This line is the auditable
    # proof that rules were locked before the test range was opened.

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # ── Step C: fetch test range, run engine, filter ─────────────────────
        logger.info("Step C: fetching test-range candles and running engine")

        if test_symbol:
            symbols = [test_symbol.upper()]
        else:
            from processing.ranker import SCAN_WATCHLIST
            symbols = list(SCAN_WATCHLIST)

        all_trades:      list[ClosedTrade] = []
        filtered_trades: list[ClosedTrade] = []

        for symbol in symbols:
            ik = get_instrument_token(symbol)
            if not ik:
                logger.warning(f"Symbol not found in instrument master: {symbol}")
                continue
            try:
                candles = get_candles(ik, interval, start_d, end_d, symbol=symbol)
            except Exception as exc:
                logger.error(f"Candle fetch error {symbol}: {exc}")
                continue
            if candles.empty:
                logger.debug(f"No candles for {symbol} {test_start}–{test_end}")
                continue

            ca_events = scan_ca_jumps(candles, symbol=symbol, ca_jump_pct=ca_jump_pct)
            if ca_events and strict_ca:
                logger.info(f"Skipping {symbol}: CA event detected")
                continue

            strat = strategy_cls()
            try:
                # run_and_record calls the engine once internally and returns both
                # the ClosedTrade list and the feature-annotated candidates.
                trades, candidates = run_and_record(candles, strat, symbol, resolved_features)
            except Exception as exc:
                logger.error(f"Engine error {symbol}: {exc}")
                continue

            # Build a date→trade index for O(1) lookup
            trade_by_date = {t.date: t for t in trades}
            all_trades.extend(trades)

            for cand in candidates:
                if not cand.taken:
                    continue    # flat days can't pass rule filters for trade inclusion
                row_dict = dict(cand.feature_values)
                if apply_rules(row_dict, rules_parsed):
                    t = trade_by_date.get(cand.date)
                    if t is not None:
                        filtered_trades.append(t)

        all_trade_count      = len(all_trades)
        filtered_trade_count = len(filtered_trades)

    except Exception as exc:
        if own_conn:
            conn.close()
        raise RuntimeError(f"Step C failed: {exc}") from exc

    # ── Step D: write result CSV ─────────────────────────────────────────────
    logger.info(f"Step D: writing result CSV ({filtered_trade_count} filtered trades)")
    path = _RESULTS_DIR / f"train_test_{strategy}_{test_start}_{test_end}_{run_ts}.csv"
    if filtered_trades:
        write_csv(filtered_trades, path)

    raw_perf = compute_trade_summary(filtered_trades)
    human_rules = _human_readable_rules(rules_parsed)

    summary: dict = {
        "mode":                  "train_test",
        "ruleset_id":            ruleset_id,
        "rules_applied":         human_rules,
        "confidence":            rules_parsed.get("confidence"),
        "claude_reasoning":      rules_parsed.get("claude_reasoning"),
        "unfiltered_trade_count": all_trade_count,
        "filtered_trade_count":  filtered_trade_count,
        **_safe_summary(raw_perf),
    }

    if own_conn:
        conn.close()

    return str(path), summary
