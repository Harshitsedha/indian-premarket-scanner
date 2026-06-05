"""
pipeline/backtest/strategy_gen.py — Claude-powered strategy generator.

generate_strategy(description, existing_code) → str (Python class source)

The prompt embeds the complete Strategy protocol, BarContext API, Signal/Action
types, and all look-ahead constraints so Claude can produce correct code without
any additional context. If existing_code is provided, Claude refines it instead
of regenerating from scratch.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import anthropic
from loguru import logger

from utils.config import CLAUDE_MODEL, settings


_SYSTEM = """\
You are an expert Python trading strategy developer for NSE equity intraday strategies.
Your task is to implement a strategy class for a bar-by-bar backtesting engine that enforces
strict no-look-ahead guarantees.

═══════════════════════════════════════════════════════
STRATEGY PROTOCOL — implement exactly this interface
═══════════════════════════════════════════════════════

class MyStrategy:
    eod_exit: bool = True   # set in __init__; engine reads this to force EOD flat

    def __init__(
        self,
        param1: float = default1,   # EXPLICIT named params — NO **kwargs
        param2: int   = default2,
        ...
        eod_exit: bool = True,
    ) -> None:
        # Store all params as self.xxx.
        # Initialise per-day mutable tracking state here too (e.g. self._entered = False).
        ...

    def reset_day(self) -> None:
        # Called by the engine before the first bar of every new trading day.
        # Reset ALL per-day mutable attributes to their initial values.
        # A strategy that forgets to reset state will produce wrong results.
        ...

    def on_bar(self, ctx: BarContext) -> Signal | None:
        # Called once per bar in strict chronological order.
        # Return a Signal to act, or None to do nothing.
        # The engine owns fills, stop/target checks, MFE/MAE, and P&L.
        ...

═══════════════════════════════════════════════════════
BarContext — the ONLY data source available to on_bar()
═══════════════════════════════════════════════════════

ctx.bars                        pd.DataFrame  candles.iloc[:i+1]  CURRENT + PAST ONLY
ctx.current                     pd.Series     ctx.bars.iloc[-1]   the bar being processed
ctx.position                    object|None   truthy = in a trade; opaque, no attributes
ctx.prev_close()                float|None    prior session close; None on day 1
ctx.opening_range_high(n_min)   float|None    max high of first n_min bars today; None if not enough bars yet
ctx.opening_range_low(n_min)    float|None    min low of first n_min bars today; None if not enough bars yet

Bar columns:  open, high, low, close, volume, date (YYYY-MM-DD str), timestamp

═══════════════════════════════════════════════════════
Signals — how to emit trade decisions
═══════════════════════════════════════════════════════

Signal(action=Action.ENTER_LONG, stop_price=float, target_r=2.0, gap_pct=0.0, reason="")
Signal(action=Action.EXIT, reason="my reason")

Engine fills ENTER_LONG at the NEXT bar's open. Stop/target/EOD exits are engine-owned.
One pending ENTER_LONG per day — signal is ignored if already in a trade or pending.

═══════════════════════════════════════════════════════
Pre-imported names — do NOT write import statements
═══════════════════════════════════════════════════════

Available without import: Action, Signal, BarContext, pd, np, math, deque

═══════════════════════════════════════════════════════
CRITICAL: look-ahead rules — ANY violation fails validation
═══════════════════════════════════════════════════════

1. ctx.bars contains ONLY bars up to and including the current bar i.
   SAFE (negative indices / tail):
       ctx.bars.iloc[-1]        ← current bar (same as ctx.current)
       ctx.bars.iloc[-2]        ← one bar ago
       ctx.bars.tail(N)         ← last N bars
       ctx.bars["close"].values ← all visible closes (past + current)
   UNSAFE (positive indices or arithmetic on length):
       ctx.bars.iloc[i]         ← FORBIDDEN (i>=0 is a future bar)
       ctx.bars.iloc[len(ctx.bars)]   ← index error AND look-ahead intent

2. ctx.opening_range_high/low guard themselves (return None when < n_min bars
   of today are visible). Always check for None before using the result.

3. Never access tomorrow's open, high, low, or close in any form.

4. self._is_first_bar patterns are safe — use them to detect day boundaries.

═══════════════════════════════════════════════════════
__init__ requirement
═══════════════════════════════════════════════════════

Use explicit named parameters with sensible defaults — no **kwargs.
Example: def __init__(self, fast_ema: int = 9, slow_ema: int = 21, target_r: float = 2.0,
                      eod_exit: bool = True) -> None:

This is required so the UI can introspect parameters later.

═══════════════════════════════════════════════════════
Output format — STRICT
═══════════════════════════════════════════════════════

Return ONLY the Python class definition.
- Start with `class `
- No import statements
- No module-level code
- No markdown fences
- No explanation text
"""


def generate_strategy(description: str, existing_code: str | None = None) -> str:
    """
    Call Claude to generate or refine a strategy class.

    Args:
        description:   Plain-English strategy description from the user.
        existing_code: If provided, Claude refines this code rather than
                       generating from scratch (used for re-generate after edits).

    Returns:
        Python source string containing only the class definition.

    Raises:
        RuntimeError: if the Claude API call fails.
    """
    if existing_code and existing_code.strip():
        user_msg = (
            f"Refine the following strategy implementation to match this updated description.\n\n"
            f"Description:\n{description}\n\n"
            f"Existing code to refine:\n```python\n{existing_code}\n```\n\n"
            "Return ONLY the refined Python class definition. "
            "No imports, no markdown, no explanation."
        )
    else:
        user_msg = (
            f"Implement this strategy:\n\n{description}\n\n"
            "Return ONLY the Python class definition. "
            "No imports, no markdown, no explanation."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    logger.info(f"generate_strategy: calling {CLAUDE_MODEL}")
    try:
        response = client.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 4096,
            system     = _SYSTEM,
            messages   = [{"role": "user", "content": user_msg}],
        )
    except anthropic.APIError as exc:
        raise RuntimeError(f"Claude API error in generate_strategy: {exc}") from exc

    logger.info(
        f"generate_strategy: done  "
        f"in={response.usage.input_tokens} out={response.usage.output_tokens}"
    )
    raw = response.content[0].text.strip()

    # Strip markdown fences if Claude added them despite instructions
    if raw.startswith("```"):
        lines = raw.split("\n")
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:end]).strip()

    return raw
