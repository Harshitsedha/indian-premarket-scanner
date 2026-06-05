"""
Candidate CSV → compact statistical summary for Claude.

The summary contains ONLY statistics — no raw candidate rows except 10
illustrative best/worst examples that include feature values and label only
(no symbol, date, or any timestamp that could reveal the test range).

Public API:
    summary = build_summary(csv_path)   -> dict (JSON-serialisable)
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

# Columns that are always present regardless of which features were recorded.
_META_COLS    = {"symbol", "date", "decision_time", "taken"}
_OUTCOME_COLS = {"pnl_pct", "exit_reason", "mfe_pct", "mae_pct", "label"}
_FIXED_COLS   = _META_COLS | _OUTCOME_COLS


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = lo + 1
    if hi >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + (k - lo) * (sorted_vals[hi] - sorted_vals[lo])


def _point_biserial(feature_vals: list[float | None], labels: list[str]) -> float | None:
    """
    Point-biserial correlation between feature_vals and the binary label==win.
    Returns None when fewer than 2 valid pairs are found.
    """
    pairs = [
        (v, 1.0 if lbl == "win" else 0.0)
        for v, lbl in zip(feature_vals, labels)
        if v is not None
    ]
    if len(pairs) < 2:
        return None
    n   = len(pairs)
    xs  = [p[0] for p in pairs]
    ys  = [p[1] for p in pairs]
    mx  = sum(xs) / n
    my  = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx  = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy  = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx < 1e-12 or sy < 1e-12:
        return None
    return round(cov / (sx * sy), 4)


def _feat_stats(vals: list[float | None]) -> dict:
    finite = sorted(v for v in vals if v is not None)
    if not finite:
        return {"count": 0, "mean": None, "std": None,
                "min": None, "p25": None, "p50": None, "p75": None, "max": None}
    n    = len(finite)
    mean = sum(finite) / n
    std  = math.sqrt(sum((x - mean) ** 2 for x in finite) / n)
    return {
        "count": n,
        "mean":  round(mean, 4),
        "std":   round(std, 4),
        "min":   round(finite[0], 4),
        "p25":   round(_percentile(finite, 25), 4),
        "p50":   round(_percentile(finite, 50), 4),
        "p75":   round(_percentile(finite, 75), 4),
        "max":   round(finite[-1], 4),
    }


def build_summary(csv_path: str | Path) -> dict:
    """
    Read a candidate CSV produced by write_candidates() and return a compact
    statistical summary suitable for sending to Claude.

    The returned dict is JSON-serialisable and contains no raw rows except 10
    illustrative best/worst examples (feature columns + label only — no symbol,
    date, or decision_time).
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Candidate CSV not found: {path}")

    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append(row)

    if not rows:
        raise ValueError(f"Candidate CSV is empty: {path}")

    # Detect feature columns dynamically
    feat_cols = [c for c in headers if c not in _FIXED_COLS]
    if not feat_cols:
        raise ValueError("No feature columns found in CSV — was this produced by write_candidates()?")

    all_labels  = [r["label"] for r in rows]
    label_set   = {"win", "loss", "flat"}
    label_counts = {lbl: sum(1 for l in all_labels if l == lbl) for lbl in label_set}

    total  = len(rows)
    taken  = sum(1 for r in rows if r.get("taken", "").lower() in ("true", "1"))
    flat   = total - taken

    # Per-feature statistics broken down by label
    feature_stats: dict[str, dict] = {}
    for feat in feat_cols:
        feat_data: dict[str, list[float | None]] = {lbl: [] for lbl in label_set}
        feat_data["all"] = []
        for row in rows:
            v = _safe_float(row.get(feat))
            lbl = row.get("label", "flat")
            feat_data.setdefault(lbl, []).append(v)
            feat_data["all"].append(v)

        all_feat_vals = [_safe_float(r.get(feat)) for r in rows]
        corr_win = _point_biserial(all_feat_vals, all_labels)

        feature_stats[feat] = {
            "by_label": {
                lbl: _feat_stats(feat_data.get(lbl, []))
                for lbl in ("win", "loss", "flat", "all")
            },
            "corr_with_win": corr_win,
        }

    # Top-5 wins and bottom-5 losses by pnl_pct (feature cols + label only)
    taken_rows = [r for r in rows if r.get("taken", "").lower() in ("true", "1")]
    def pnl(r):
        return _safe_float(r.get("pnl_pct")) or 0.0
    top5    = sorted(taken_rows, key=pnl, reverse=True)[:5]
    bottom5 = sorted(taken_rows, key=pnl)[:5]

    def _example(r: dict) -> dict:
        return {fc: _safe_float(r.get(fc)) for fc in feat_cols} | {"label": r.get("label"), "pnl_pct": pnl(r)}

    return {
        "header": (
            "You are analyzing a labelled dataset of gap-and-go trade candidates on "
            "Indian NSE equities (1-minute candles, Upstox data). Each row is one "
            "qualifying setup day. Your job is to find rules that separate wins from "
            "losses and flats. Return ONLY valid JSON matching the schema provided."
        ),
        "dataset": {
            "total_candidates": total,
            "taken":            taken,
            "flat":             flat,
            "label_breakdown": {
                lbl: {
                    "count":   label_counts.get(lbl, 0),
                    "pct":     round(label_counts.get(lbl, 0) / total * 100, 1) if total else 0,
                }
                for lbl in ("win", "loss", "flat")
            },
        },
        "feature_stats": feature_stats,
        "illustrative_examples": {
            "best_5_by_pnl":  [_example(r) for r in top5],
            "worst_5_by_pnl": [_example(r) for r in bottom5],
        },
    }
