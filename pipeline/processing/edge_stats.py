"""
edge_stats.py — Shared DB query + stats builder for edge tracking.
Used by edge_analyser.py (scheduler) and /api/edge/summary (API endpoint).
"""

from datetime import datetime, timedelta, timezone

import psycopg2.extras


def query_rows(conn, lookback_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                s.trading_date, s.symbol, s.setup_type, s.hypothesis,
                s.bias_direction, s.gap_pct, s.score,
                o.actual_gap_pct, o.move_pct, o.hypothesis_correct, o.result
            FROM setups s
            LEFT JOIN outcomes o ON o.setup_id = s.id
            WHERE s.trading_date >= %s
            ORDER BY s.trading_date DESC
            """,
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


def build_stats(rows: list[dict]) -> dict:
    with_outcomes = [r for r in rows if r.get("move_pct") is not None]

    def _setup_stats(setup_type: str) -> dict:
        subset = [r for r in with_outcomes if r.get("setup_type") == setup_type]
        if not subset:
            return {"count": 0, "hypothesis_correct_rate": None,
                    "avg_move_pct": None, "avg_score": None}
        correct = [r for r in subset if r.get("hypothesis_correct") is True]
        return {
            "count": len(subset),
            "hypothesis_correct_rate": round(len(correct) / len(subset) * 100, 1),
            "avg_move_pct": round(sum(float(r["move_pct"]) for r in subset) / len(subset), 2),
            "avg_score": round(
                sum(float(r["score"]) for r in subset if r.get("score")) / len(subset), 4
            ),
        }

    def _bias_stats(direction: str) -> dict:
        subset = [r for r in with_outcomes if r.get("bias_direction") == direction]
        if not subset:
            return {"count": 0, "hypothesis_correct_rate": None}
        correct = [r for r in subset if r.get("hypothesis_correct") is True]
        return {
            "count": len(subset),
            "hypothesis_correct_rate": round(len(correct) / len(subset) * 100, 1),
        }

    symbol_counts: dict[str, int] = {}
    for r in rows:
        sym = r.get("symbol") or ""
        symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
    top_symbols = sorted(symbol_counts, key=lambda s: -symbol_counts[s])[:5]

    return {
        "total_setups":  len(rows),
        "with_outcomes": len(with_outcomes),
        "by_setup_type": {
            st: _setup_stats(st)
            for st in ["news_catalyst", "gap_play", "fii_driven", "watchlist"]
        },
        "by_bias": {
            "bullish": _bias_stats("bullish"),
            "bearish": _bias_stats("bearish"),
        },
        "top_symbols":   top_symbols,
        "recent_setups": rows[:30],
    }
