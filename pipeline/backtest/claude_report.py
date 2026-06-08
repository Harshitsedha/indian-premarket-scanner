"""
Send a computed stats dict to Claude and return a markdown report string.

Public API:
    markdown = generate_report(stats: dict) -> str

Uses the project-standard CLAUDE_MODEL and anthropic_api_key pattern,
identical to claude_rules.py. Claude receives only computed numbers —
no raw trade rows are sent.
"""
from __future__ import annotations

import json

import anthropic
from loguru import logger

try:
    from utils.config import CLAUDE_MODEL, settings
except Exception:
    CLAUDE_MODEL = "claude-sonnet-4-6"
    settings = None


_SYSTEM = """\
You are a skeptical, direct quantitative trading analyst reviewing a backtest result.
Write a structured markdown report. Be direct — no cheerleading.

## Instructions

Produce exactly these sections in order:

## Edge Quality
Assess whether this strategy has a genuine edge. Lead with expectancy and profit factor together.
State the confidence level explicitly based on sample size:
  - < 30 trades: "very low confidence — statistically meaningless"
  - 30–100 trades: "low confidence — treat as exploratory"
  - 100–300 trades: "moderate confidence"
  - > 300 trades: "reasonable sample"
Call out curve-fitting or overfitting risk if sample is small or win rate is unusually high (> 65%).

## Behavioral / Risk Patterns
Analyze these specifically (only if data is present):
  - Give-back behavior: what fraction of trades had meaningful MFE then closed negative?
  - EOD exit drag: are EOD exits profitable or dragging overall returns down?
  - Stop placement: does avg MAE on losers imply stops are too tight (stopped out then price reverses)?
  - Streak risk: can a trader sustain the max consecutive loss streak emotionally and financially?
  - Bars-held pattern: are quick trades or long holds contributing most of the edge?

## Stats Breakdown
Concise readout of the numbers. Use a clean list format. Show: trades/win rate/losses, expectancy,
profit factor, payoff ratio, avg win/avg loss, exit breakdown (% of trades and avg PnL each),
bars held avg/median, consecutive win/loss streaks, gap-pct bucket performance if non-trivial.

## Pros
Bulleted list. Only include genuine strengths that the numbers actually support. Be specific.

## Cons
Bulleted list. Flag every concern clearly — small sample, poor payoff ratio, give-back, etc.
Do not soften these.

## Verdict
One or two sentences. Direct assessment: is this edge real, marginal, or insufficient to trade?
Do not hedge with "might" or "could" — make a call.

---

Rules:
- Never invent, round, or extrapolate numbers not present in the stats JSON.
- Use exact figures from the stats block in your commentary.
- If profit_factor is null, state it means no losing trades — high overfitting risk on small sample.
- Flag small sample size prominently in EVERY section where it matters.
- Do not end with encouragement or "keep backtesting" platitudes.
"""


def generate_report(stats: dict) -> str:
    """
    Send the computed stats dict to Claude and return the markdown report.

    Args:
        stats: dict produced by report_stats.load_and_compute()

    Returns:
        Markdown string.

    Raises:
        ValueError: on Anthropic API error.
    """
    stats_json = json.dumps(stats, indent=2, default=str)
    prompt = (
        "Here are the computed statistics for a backtest result on NSE equities "
        "(1-minute candles, gap-and-go style strategy):\n\n"
        f"```json\n{stats_json}\n```\n\n"
        "Write the analysis report following the instructions exactly."
    )

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key if settings else None
    )
    try:
        response = client.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 1800,
            system     = _SYSTEM,
            messages   = [{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise ValueError(f"Anthropic API error: {exc}") from exc

    text = response.content[0].text
    logger.info(
        f"claude_report: input={response.usage.input_tokens} "
        f"output={response.usage.output_tokens} tokens"
    )
    return text
