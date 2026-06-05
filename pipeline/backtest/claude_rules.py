"""
Send a candidate-dataset summary to Claude and parse the returned rule set.

Public API:
    rules_raw, rules_parsed = get_rules(feature_stats_summary: dict) -> tuple[str, dict]

Rules:
  - Never sends raw candle data or raw candidate rows (the caller owns that).
  - Uses the project's standard claude_client pattern: anthropic.Anthropic(),
    CLAUDE_MODEL from utils.config, _extract_json from claude_client.
  - Retries once on malformed JSON. Raises ValueError on second failure.
  - Validates every filter in rules_parsed before returning.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
from loguru import logger

try:
    from utils.config import CLAUDE_MODEL, settings
except Exception:
    CLAUDE_MODEL = "claude-sonnet-4-6"
    settings = None

# Reuse the project-standard JSON extractor — same robustness, no duplication.
from processing.claude_client import _extract_json


# ── Supported filter ops ──────────────────────────────────────────────────────

_UNARY_SCALAR_OPS = {"lt", "gt", "lte", "gte"}       # require "value" key
_INTERVAL_OPS     = {"between"}                        # require "low" + "high" keys
_SET_OPS          = {"in", "not_in"}                   # require "values" key (list)
_ALL_OPS          = _UNARY_SCALAR_OPS | _INTERVAL_OPS | _SET_OPS

_RULES_SCHEMA = """{
  "filters": [
    { "feature": "gap_pct",      "op": "between", "low": 1.0,  "high": 3.0  },
    { "feature": "or_range_pct", "op": "lt",      "value": 2.5              },
    { "feature": "day_of_week",  "op": "not_in",  "values": [4]             }
  ],
  "confidence": "low|medium|high",
  "claude_reasoning": "one paragraph summary"
}"""

_SYSTEM = (
    "You are a systematic quantitative analyst. "
    "Given statistical feature data from a labelled trading dataset, "
    "return a compact rule set that filters candidates to improve win-rate. "
    "Return ONLY valid JSON matching the schema exactly — no markdown, no prose outside JSON."
)


def _build_prompt(summary: dict) -> str:
    summary_json = json.dumps(summary, indent=2, default=str)
    return (
        f"{summary['header']}\n\n"
        "## Statistical Summary\n\n"
        f"{summary_json}\n\n"
        "## Output schema (return ONLY this JSON, no other text):\n\n"
        f"{_RULES_SCHEMA}\n\n"
        "Constraints:\n"
        "- 1 to 6 filters maximum. Only filter on features that appear in feature_stats.\n"
        "- confidence: 'low' if n<50 candidates, 'medium' if 50–200, 'high' if >200.\n"
        "- claude_reasoning: one paragraph, plain text, explain why these filters "
        "are expected to improve win-rate given the statistics above."
    )


def _validate_filters(rules_parsed: dict, known_features: set[str]) -> None:
    """
    Validate every filter in rules_parsed. Raises ValueError on the first bad filter.
    """
    filters = rules_parsed.get("filters")
    if not isinstance(filters, list):
        raise ValueError("rules_parsed['filters'] must be a list")
    if not filters:
        raise ValueError("rules_parsed['filters'] is empty — must have at least one filter")
    if len(filters) > 6:
        raise ValueError(f"Too many filters ({len(filters)}); maximum is 6")

    seen_features: set[str] = set()
    for i, f in enumerate(filters):
        feat = f.get("feature")
        op   = f.get("op")
        if not isinstance(feat, str) or not feat:
            raise ValueError(f"Filter {i}: missing or invalid 'feature': {f!r}")
        if known_features and feat not in known_features:
            raise ValueError(
                f"Filter {i}: unknown feature {feat!r}. "
                f"Known: {sorted(known_features)}"
            )
        if feat in seen_features:
            raise ValueError(f"Filter {i}: duplicate filter on feature {feat!r}")
        seen_features.add(feat)

        if op not in _ALL_OPS:
            raise ValueError(f"Filter {i}: unknown op {op!r}. Supported: {sorted(_ALL_OPS)}")

        if op in _UNARY_SCALAR_OPS:
            if "value" not in f:
                raise ValueError(f"Filter {i}: op={op!r} requires 'value' key: {f!r}")
            if not isinstance(f["value"], (int, float)):
                raise ValueError(f"Filter {i}: 'value' must be numeric, got {f['value']!r}")
        elif op in _INTERVAL_OPS:
            for key in ("low", "high"):
                if key not in f:
                    raise ValueError(f"Filter {i}: op='between' requires '{key}' key: {f!r}")
                if not isinstance(f[key], (int, float)):
                    raise ValueError(f"Filter {i}: '{key}' must be numeric, got {f[key]!r}")
            if f["low"] >= f["high"]:
                raise ValueError(f"Filter {i}: 'low' must be < 'high': {f!r}")
        elif op in _SET_OPS:
            if "values" not in f or not isinstance(f.get("values"), list):
                raise ValueError(f"Filter {i}: op={op!r} requires 'values' (list): {f!r}")
            if not f["values"]:
                raise ValueError(f"Filter {i}: 'values' list is empty: {f!r}")

    conf = rules_parsed.get("confidence")
    if conf not in ("low", "medium", "high"):
        raise ValueError(f"rules_parsed['confidence'] must be 'low', 'medium', or 'high'; got {conf!r}")

    if not isinstance(rules_parsed.get("claude_reasoning"), str):
        raise ValueError("rules_parsed['claude_reasoning'] must be a string")


def get_rules(feature_stats_summary: dict) -> tuple[str, dict]:
    """
    Send the compact statistical summary to Claude and return the validated rule set.

    Args:
        feature_stats_summary: dict produced by summarise.build_summary()

    Returns:
        (rules_raw, rules_parsed)
        - rules_raw: Claude's full response text (audit trail)
        - rules_parsed: validated dict matching the rules schema

    Raises:
        ValueError: if Claude returns malformed JSON on both attempts, or
                    if the parsed filters reference unknown features/ops.
    """
    known_features = set(feature_stats_summary.get("feature_stats", {}).keys())
    prompt         = _build_prompt(feature_stats_summary)
    client         = anthropic.Anthropic(api_key=settings.anthropic_api_key if settings else None)

    rules_raw:    str  | None = None
    rules_parsed: dict | None = None
    last_err: Exception | None = None

    for attempt in range(1, 3):
        try:
            response = client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 1024,
                system     = _SYSTEM,
                messages   = [{"role": "user", "content": prompt}],
            )
            rules_raw = response.content[0].text
            logger.info(
                f"claude_rules attempt {attempt}: "
                f"input={response.usage.input_tokens} "
                f"output={response.usage.output_tokens} tokens"
            )
            parsed = _extract_json(rules_raw)
            _validate_filters(parsed, known_features)
            rules_parsed = parsed
            break
        except (ValueError, KeyError) as exc:
            logger.warning(f"claude_rules attempt {attempt} parse/validate error: {exc}")
            last_err = exc
            if attempt == 1:
                logger.info("Retrying claude_rules once…")
                continue
        except anthropic.APIError as exc:
            raise ValueError(f"Anthropic API error: {exc}") from exc

    if rules_parsed is None:
        raise ValueError(
            f"Claude returned malformed or invalid JSON on both attempts. "
            f"Last error: {last_err}. "
            f"Raw response:\n{rules_raw}"
        )

    logger.info(
        f"claude_rules: {len(rules_parsed['filters'])} filter(s) committed, "
        f"confidence={rules_parsed['confidence']}"
    )
    return rules_raw, rules_parsed
