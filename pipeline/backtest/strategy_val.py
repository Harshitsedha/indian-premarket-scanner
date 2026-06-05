"""
pipeline/backtest/strategy_val.py — Claude-powered strategy validator.

validate_strategy(code: str) -> dict

Asks Claude to perform five semantic safety checks and returns a structured
ValidationReport. Static linting cannot catch look-ahead bugs (reading
ctx.bars.iloc[-1] on a stale full array looks syntactically fine — Claude
reads intent). Claude is the authoritative validator.

Retries once on malformed JSON before raising.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import anthropic
from loguru import logger

from utils.config import CLAUDE_MODEL


# ── JSON extraction (inlined to avoid claude_client dependency chain) ─────────

def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fence:
        text = fence.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not extract JSON from Claude response:\n{text[:300]}")


# ── Validator prompt ──────────────────────────────────────────────────────────

_SYSTEM = """\
You are a security auditor for a no-look-ahead backtesting engine.
Validate the provided Python strategy class against five checks.

═══════════════════════════════════════════════════════
The five checks — perform ALL five, in order
═══════════════════════════════════════════════════════

1. no_look_ahead
   The strategy may ONLY access past and current bars via:
     ctx.bars.iloc[-N]  (negative index)
     ctx.bars.tail(N)
     ctx.bars["col"]    (the whole visible series, which is past+current only)
     ctx.current        (alias for ctx.bars.iloc[-1])
     ctx.opening_range_high(n) / ctx.opening_range_low(n)  (self-guarded)
     ctx.prev_close()   (safe — filters bars before today)
   FORBIDDEN:
     ctx.bars.iloc[i] with i >= 0   (positive index = future bar)
     ctx.bars.iloc[len(ctx.bars)]   (off-end index)
     Any peek at tomorrow's open/high/low/close
   Be conservative: if a positive index appears anywhere, fail this check and
   quote the exact offending line.

2. no_future_imports
   The code must contain NO import statements of any kind.
   Pre-imported names (Action, Signal, BarContext, pd, np, math, deque) cover
   all legitimate needs. Any `import`, `from ... import`, or `__import__` call
   fails this check.

3. protocol_compliance
   The class must define:
     - reset_day(self) -> None
     - on_bar(self, ctx: BarContext) -> Signal | None  (or Signal or None return)
     - __init__ must set self.eod_exit (a bool)
   Missing any of these fails this check.

4. reset_day_clears_state
   Every per-day mutable attribute set in __init__ (e.g. self._tracking = False,
   self._entered = False) must also be reset in reset_day(). Attributes that
   accumulate across days without being reset will produce wrong backtest results.
   List any attributes missing from reset_day() in the detail field.

5. on_bar_no_side_effects
   on_bar() must not: write files, make network calls, modify global variables,
   call print(), use random without a seed, or do anything that would produce
   different results on re-run with the same inputs.

═══════════════════════════════════════════════════════
Response format — return ONLY this JSON, no other text
═══════════════════════════════════════════════════════

{
  "passed": true,
  "checks": [
    { "name": "no_look_ahead",          "passed": true, "detail": null },
    { "name": "no_future_imports",       "passed": true, "detail": null },
    { "name": "protocol_compliance",     "passed": true, "detail": null },
    { "name": "reset_day_clears_state",  "passed": true, "detail": null },
    { "name": "on_bar_no_side_effects",  "passed": true, "detail": null }
  ],
  "verdict": "Safe to register.",
  "risk_notes": []
}

Rules:
- top-level "passed" = logical AND of all five check.passed values
- For a failing check: set detail to the exact offending line(s) quoted from the code
- For a passing check: detail may be a one-line explanation or null
- "verdict": one sentence explaining the overall decision
- "risk_notes": list any concerns that don't constitute a hard failure (style issues, etc.)
- Never approve (passed=true) if any check is false
"""


# ── Public API ────────────────────────────────────────────────────────────────

def validate_strategy(code: str) -> dict:
    """
    Ask Claude to validate a strategy class definition.

    Returns:
        ValidationReport dict with keys: passed, checks, verdict, risk_notes.

    Raises:
        ValueError: if Claude returns unparseable JSON after one retry,
                    or if the API call fails.
    """
    user_msg = f"Validate this strategy class:\n\n```python\n{code}\n```"

    client = anthropic.Anthropic()
    logger.info(f"validate_strategy: calling {CLAUDE_MODEL}")

    for attempt in range(2):
        try:
            response = client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 1024,
                system     = _SYSTEM,
                messages   = [{"role": "user", "content": user_msg}],
            )
        except anthropic.APIError as exc:
            raise ValueError(f"Claude API error in validate_strategy: {exc}") from exc

        logger.info(
            f"validate_strategy attempt {attempt + 1}: "
            f"in={response.usage.input_tokens} out={response.usage.output_tokens}"
        )
        raw = response.content[0].text
        try:
            report = _extract_json(raw)
            _normalise(report)
            return report
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(f"validate_strategy: parse error attempt {attempt + 1}: {exc}")
            if attempt == 1:
                raise ValueError(
                    f"Claude returned unparseable validation JSON after 2 attempts: {exc}"
                ) from exc

    raise ValueError("validate_strategy: unreachable")


def _normalise(report: dict) -> None:
    """Enforce that top-level passed = AND of all checks; add missing fields."""
    checks = report.get("checks", [])
    all_passed = all(bool(c.get("passed", False)) for c in checks)
    report["passed"] = all_passed
    report.setdefault("risk_notes", [])
    report.setdefault("verdict", "Safe to register." if all_passed else "One or more checks failed.")
