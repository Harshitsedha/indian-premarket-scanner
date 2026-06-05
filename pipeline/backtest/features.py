"""
Feature registry for the candidate recorder.

Public API:
    FEATURES: dict[str, FeatureSpec]    -- all registered features
    parse_features(s) -> list[ResolvedFeature]   -- parse --features CLI string

Feature contract:
    compute(ctx: BarContext, params: dict) -> float | None
    ctx.bars is the visible slice up to and including the decision-point bar.
    A feature NEVER reads beyond ctx.bars.iloc[-1] (= ctx.current).
    Returns None if it cannot be computed at this point (row is left blank in CSV).

--features string format:
    "gap_pct,or_width:n_min=30,vol_ratio:lookback=20"
    Comma-separated names; colon separates optional param overrides (key=value).
    Multiple params per feature: "feat:p1=v1&p2=v2".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from backtest.strategy import BarContext


# ── Registry types ────────────────────────────────────────────────────────────

@dataclass
class ParamSpec:
    name:    str
    type:    type   # int | float | bool
    default: Any    # must be an instance of .type


@dataclass
class FeatureSpec:
    name:        str
    description: str
    params:      list[ParamSpec]
    compute:     Callable[[BarContext, dict], float | None]


@dataclass
class ResolvedFeature:
    col_name: str         # output CSV column name (= feature name)
    spec:     FeatureSpec
    params:   dict        # merged defaults + user overrides


# Registry — populated by _reg() below
FEATURES: dict[str, FeatureSpec] = {}


def _reg(spec: FeatureSpec) -> FeatureSpec:
    FEATURES[spec.name] = spec
    return spec


# ── Feature implementations ───────────────────────────────────────────────────

def _gap_pct(ctx: BarContext, params: dict) -> float | None:
    """Gap % of the day's open vs. prior close."""
    today      = ctx.current["date"]
    today_bars = ctx.bars[ctx.bars["date"] == today]
    if today_bars.empty:
        return None
    first_open = float(today_bars.iloc[0]["open"])
    prev_close = ctx.prev_close()
    if prev_close is None or prev_close <= 0:
        return None
    return round((first_open - prev_close) / prev_close * 100, 4)


def _or_width(ctx: BarContext, params: dict) -> float | None:
    """Absolute price width of the opening range (OR_high - OR_low)."""
    n = params["n_min"]
    h = ctx.opening_range_high(n)
    l = ctx.opening_range_low(n)
    if h is None or l is None:
        return None
    return round(h - l, 4)


def _or_range_pct(ctx: BarContext, params: dict) -> float | None:
    """OR width as a % of OR low: (OR_high - OR_low) / OR_low * 100."""
    n = params["n_min"]
    h = ctx.opening_range_high(n)
    l = ctx.opening_range_low(n)
    if h is None or l is None or l <= 0:
        return None
    return round((h - l) / l * 100, 4)


def _time_of_day_min(ctx: BarContext, params: dict) -> float | None:
    """Minutes elapsed since 09:15 at the decision bar."""
    ts = pd.Timestamp(ctx.current["timestamp"])
    return float(ts.hour * 60 + ts.minute - (9 * 60 + 15))


def _prev_day_range_pct(ctx: BarContext, params: dict) -> float | None:
    """Prior session's full high-low range as a % of its low."""
    today = ctx.current["date"]
    prior = ctx.bars[ctx.bars["date"] < today]
    if prior.empty:
        return None
    prev_day      = prior["date"].max()
    prev_day_bars = prior[prior["date"] == prev_day]
    if prev_day_bars.empty:
        return None
    high = float(prev_day_bars["high"].max())
    low  = float(prev_day_bars["low"].min())
    if low <= 0:
        return None
    return round((high - low) / low * 100, 4)


def _vol_ratio(ctx: BarContext, params: dict) -> float | None:
    """
    Volume accumulated today (up to the decision bar) divided by the average
    FULL daily volume of the most recent `lookback` prior trading days.

    A ratio < 1 means today is pacing below historical average.
    At bar 60 of a 375-bar session, a ratio ~= 60/375 * 1 is neutral pace.
    """
    lookback = params["lookback"]
    today      = ctx.current["date"]
    today_bars = ctx.bars[ctx.bars["date"] == today]
    if today_bars.empty:
        return None
    today_vol = float(today_bars["volume"].sum())

    prior = ctx.bars[ctx.bars["date"] < today]
    if prior.empty:
        return None

    prior_dates = sorted(prior["date"].unique())[-lookback:]   # most recent N
    if not prior_dates:
        return None

    daily_vols = [
        float(prior[prior["date"] == d]["volume"].sum())
        for d in prior_dates
    ]
    avg_vol = sum(daily_vols) / len(daily_vols)
    if avg_vol <= 0:
        return None
    return round(today_vol / avg_vol, 4)


def _day_of_week(ctx: BarContext, params: dict) -> float | None:
    """0 = Monday … 4 = Friday (numeric for ML use)."""
    ts = pd.Timestamp(ctx.current["timestamp"])
    return float(ts.dayofweek)


# ── Register all features ─────────────────────────────────────────────────────

_reg(FeatureSpec(
    name        = "gap_pct",
    description = "Gap % of today's open vs. prior session close",
    params      = [],
    compute     = _gap_pct,
))

_reg(FeatureSpec(
    name        = "or_width",
    description = "Absolute price width of the opening range (OR_high - OR_low)",
    params      = [ParamSpec("n_min", int, 15)],
    compute     = _or_width,
))

_reg(FeatureSpec(
    name        = "or_range_pct",
    description = "OR width as %% of OR low",
    params      = [ParamSpec("n_min", int, 15)],
    compute     = _or_range_pct,
))

_reg(FeatureSpec(
    name        = "time_of_day_min",
    description = "Minutes since 09:15 at the decision bar",
    params      = [],
    compute     = _time_of_day_min,
))

_reg(FeatureSpec(
    name        = "prev_day_range_pct",
    description = "Prior session high-low range as %% of prior low",
    params      = [],
    compute     = _prev_day_range_pct,
))

_reg(FeatureSpec(
    name        = "vol_ratio",
    description = (
        "Today's accumulated volume / avg full-day volume of last N prior sessions. "
        "At the 60-bar decision point, neutral pace ≈ 60/375 × 1."
    ),
    params      = [ParamSpec("lookback", int, 20)],
    compute     = _vol_ratio,
))

_reg(FeatureSpec(
    name        = "day_of_week",
    description = "Day-of-week integer: 0=Mon … 4=Fri",
    params      = [],
    compute     = _day_of_week,
))


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_features(features_str: str) -> list[ResolvedFeature]:
    """
    Parse a --features string into a list of ResolvedFeature.

    Format:  "name[:param=val[&param=val]],name,..."
    Example: "gap_pct,or_width:n_min=30,vol_ratio:lookback=20"

    Raises ValueError on:
      - Unknown feature name
      - Duplicate feature name
      - Unknown parameter name
      - Parameter value that cannot be cast to the declared type
    """
    results: list[ResolvedFeature] = []
    seen:    set[str]              = set()

    for token in features_str.split(","):
        token = token.strip()
        if not token:
            continue

        name, _, param_str = token.partition(":")
        name = name.strip()

        if name not in FEATURES:
            available = ", ".join(sorted(FEATURES.keys()))
            raise ValueError(
                f"Unknown feature {name!r}. Available: {available}"
            )
        if name in seen:
            raise ValueError(
                f"Feature {name!r} appears more than once in --features "
                "(each feature may only be requested once per run)."
            )
        seen.add(name)

        spec   = FEATURES[name]
        params = {p.name: p.default for p in spec.params}

        if param_str:
            for kv in param_str.split("&"):
                kv = kv.strip()
                if "=" not in kv:
                    raise ValueError(
                        f"Invalid param syntax in {name!r}: {kv!r} "
                        "(expected key=value)"
                    )
                k, _, v = kv.partition("=")
                k = k.strip()
                v = v.strip()

                matching = [p for p in spec.params if p.name == k]
                if not matching:
                    available_p = [p.name for p in spec.params] or ["(none)"]
                    raise ValueError(
                        f"Unknown param {k!r} for feature {name!r}. "
                        f"Available: {available_p}"
                    )
                pspec = matching[0]
                try:
                    params[k] = pspec.type(v)
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"Invalid value {v!r} for param {k!r} of feature {name!r} "
                        f"(expected {pspec.type.__name__}): {exc}"
                    ) from exc

        results.append(ResolvedFeature(col_name=name, spec=spec, params=params))

    return results
