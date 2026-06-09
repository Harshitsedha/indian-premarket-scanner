"""
Single entry point for all Anthropic Claude API calls in the pipeline.
Never call the Anthropic SDK directly elsewhere — go through this module.
"""

import json
import re
from pathlib import Path
from typing import Any

import anthropic
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from processing.tagging_universe import TAGGING_UNIVERSE_SET

# CLAUDE_MODEL is a plain constant in config — safe to import without a .env
try:
    from utils.config import CLAUDE_MODEL
except Exception:
    CLAUDE_MODEL = "claude-sonnet-4-6"


# ── exceptions ────────────────────────────────────────────────────────────────

class ClaudeClientError(Exception):
    """Raised when all retry attempts are exhausted or a fatal parse error occurs."""


# ── prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_NEWS = (
    "You are a pre-market analyst for Indian equity markets (NSE/BSE). "
    "Analyse the provided headlines and return structured JSON only. "
    "No explanation, no markdown, no preamble. Valid JSON only."
)

_JSON_SCHEMA = """{
  "overall_bias": {
    "direction": "bullish|bearish|neutral|mixed",
    "confidence": 3,
    "reason": "one sentence"
  },
  "headlines": [
    {
      "id": 1,
      "sentiment": "bullish|bearish|neutral",
      "importance": 3,
      "reason": "5-8 words",
      "symbols": ["RELIANCE"]
    }
  ]
}"""

# confidence: 1-5  (5 = very high conviction)
# importance: 1-5  (5 = market-moving, 4 = significant, 3 = moderate, 2 = minor, 1 = noise)
# symbols: NSE trading symbols explicitly mentioned in the headline; [] if none
#          (validated against TAGGING_UNIVERSE_SET after parsing — unknowns are dropped)

_SYSTEM_EDGE = (
    "You are a pre-market edge analyst. "
    "Identify high-probability intraday setups from the provided data. "
    "Write in plain text — no markdown, no JSON."
)

# ── catalyst schema ───────────────────────────────────────────────────────────

_CATALYST_SCHEMA = """{
  "stocks": [
    {
      "symbol": "RELIANCE",
      "catalyst_line": "Q4 profit beats by 8%; Jio ARPU expansion drives bullish open",
      "direction": "bullish",
      "setup_type": "news_momentum"
    }
  ]
}"""

# direction must be one of these (others fall back to "neutral")
_VALID_DIRECTIONS: frozenset[str] = frozenset({"bullish", "bearish", "neutral"})
# setup_type must be one of these (others fall back to "other")
_VALID_SETUP_TYPES: frozenset[str] = frozenset(
    {"gap_up_continuation", "gap_fade", "news_momentum", "sympathy", "gap_play", "other"}
)


# ── internal helpers ──────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """
    Robustly extract JSON from Claude's response.
    Handles: raw JSON, ```json fences, ``` fences, truncated/unterminated fences,
    leading/trailing whitespace.
    Raises: ValueError if parsing fails completely.
    """
    text = text.strip()

    # Complete fence pair: ```json ... ```
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fence_match:
        text = fence_match.group(1).strip()
    elif text.startswith('```'):
        # Truncated response: opening fence but no closing fence
        text = re.sub(r'^```(?:json)?\s*\n?', '', text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find first { ... } block in the string
        brace_match = re.search(r'\{[\s\S]*\}', text)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

    raise ValueError(f"Could not extract valid JSON from Claude response:\n{text}")


def _build_news_prompt(headlines: list[dict[str, Any]]) -> str:
    lines = [
        f"{i}. {h.get('headline', '').strip()} [{h.get('source', '')}]"
        for i, h in enumerate(headlines, 1)
    ]
    body = "\n".join(lines)
    return (
        f"Analyse these {len(headlines)} Indian market headlines:\n\n"
        f"{body}\n\n"
        "importance scale: 5=market-moving, 4=significant, 3=moderate, 2=minor, 1=noise\n"
        "confidence scale: 1-5 (5=very high conviction)\n\n"
        "For 'symbols': return NSE trading symbols (e.g. RELIANCE, TCS, INFY) explicitly "
        "mentioned in the headline by company name or ticker. Use exact NSE format — "
        "uppercase, no exchange prefix. Return [] if no specific company is mentioned.\n\n"
        f"Return ONLY this JSON (no other text):\n{_JSON_SCHEMA}"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(anthropic.APIError),
    reraise=True,
)
def _call_claude(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    max_tokens: int,
) -> anthropic.types.Message:
    return client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


def _log_usage(label: str, response: anthropic.types.Message) -> None:
    logger.info(
        f"Claude [{label}] usage: "
        f"input_tokens={response.usage.input_tokens}, "
        f"output_tokens={response.usage.output_tokens}"
    )


# ── public API ────────────────────────────────────────────────────────────────

def analyse_news(headlines: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Analyse a list of headline dicts and return a structured bias + per-headline
    sentiment JSON.

    Args:
        headlines: list of dicts with at least "headline" and "source" keys.

    Returns:
        Parsed JSON dict matching the schema defined in _JSON_SCHEMA.

    Raises:
        ValueError: if headlines is empty.
        ClaudeClientError: if all retry attempts fail or Claude returns non-JSON.
    """
    if not headlines:
        raise ValueError("headlines list must not be empty")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment
    prompt = _build_news_prompt(headlines)

    try:
        response = _call_claude(client, _SYSTEM_NEWS, prompt, max_tokens=2048)
    except anthropic.APIError as exc:
        raise ClaudeClientError(f"analyse_news: API failed after retries: {exc}") from exc

    _log_usage("analyse_news", response)
    raw = response.content[0].text

    try:
        result: dict[str, Any] = _extract_json(raw)
    except (ValueError, KeyError) as exc:
        logger.error(f"Claude response parse error: {exc}")
        raise ClaudeClientError(f"JSON parse failed: {exc}") from exc

    # Hard-validate symbols: drop anything not in the tagging universe.
    # This is the authoritative filter — prompt alone cannot prevent hallucinations.
    for h in result.get("headlines") or []:
        raw_syms = h.get("symbols")
        if isinstance(raw_syms, list):
            h["symbols"] = [
                s.upper() for s in raw_syms
                if isinstance(s, str) and s.upper() in TAGGING_UNIVERSE_SET
            ]
        else:
            h["symbols"] = []

    return result


def generate_stock_catalysts(
    ranked_stocks: list[dict[str, Any]],
    headlines: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    For each ranked stock generate: catalyst_line, direction, setup_type.

    Returns a dict keyed by UPPER symbol:
        {"RELIANCE": {"catalyst_line": "...", "direction": "bullish", "setup_type": "news_momentum"}}

    On any failure (API error, parse error) the symbol's entry falls back to:
        catalyst_line = existing thesis text
        direction     = stock sentiment (already computed by ranker)
        setup_type    = "other"

    Never raises — all errors are logged and swallowed.
    """
    if not ranked_stocks:
        return {}

    # Pre-compute fallback from whatever the ranker already knows
    fallback: dict[str, dict[str, Any]] = {
        s["symbol"].upper(): {
            "catalyst_line": str(s.get("thesis") or "")[:150],
            "direction":     str(s.get("sentiment") or "neutral").lower(),
            "setup_type":    "other",
        }
        for s in ranked_stocks
    }

    # Build a lookup: symbol -> list of matching headline texts (from Claude symbol tags)
    sym_headlines: dict[str, list[str]] = {s["symbol"]: [] for s in ranked_stocks}
    for h in headlines:
        for sym in h.get("symbols") or []:
            if sym in sym_headlines:
                sym_headlines[sym].append(h.get("headline", "")[:120])

    # Compose the user prompt
    stock_lines: list[str] = ["Stocks to analyse (pre-market, NSE India):"]
    for s in ranked_stocks:
        sym  = s["symbol"]
        gap  = float((s.get("signals") or {}).get("prior_session_gap_pct") or 0)
        cnt  = int(s.get("mention_count") or 0)
        sent = str(s.get("sentiment") or "neutral")
        stock_lines.append(f"- {sym}: gap={gap:+.1f}%, mentions={cnt}, sentiment={sent}")
        for hl in sym_headlines.get(sym, [])[:3]:
            stock_lines.append(f"    * {hl}")

    prompt = (
        "\n".join(stock_lines) + "\n\n"
        "Rules:\n"
        "  catalyst_line: ≤120 chars, trader-focused, states the actual catalyst.\n"
        "    If gap with no news: write '{gap:+.1f}% prior-session gap, no news — "
        "momentum continuation watch' (fill in real number).\n"
        "    NOT 'X headline mention' — that is banned.\n"
        "  direction: expected price direction at open (bullish|bearish|neutral).\n"
        "  setup_type: gap_up_continuation|gap_fade|news_momentum|sympathy|gap_play|other\n\n"
        f"Return ONLY this JSON (no other text):\n{_CATALYST_SCHEMA}"
    )

    client = anthropic.Anthropic()
    try:
        response = _call_claude(client, _SYSTEM_NEWS, prompt, max_tokens=1024)
    except anthropic.APIError as exc:
        logger.warning(f"generate_stock_catalysts: API failed: {exc} — using fallback")
        return fallback

    _log_usage("generate_stock_catalysts", response)

    try:
        parsed = _extract_json(response.content[0].text)
    except (ValueError, KeyError) as exc:
        logger.warning(f"generate_stock_catalysts: parse failed: {exc} — using fallback")
        return fallback

    out: dict[str, dict[str, Any]] = {}
    for entry in parsed.get("stocks") or []:
        sym = str(entry.get("symbol") or "").upper()
        if not sym:
            continue
        direction  = str(entry.get("direction")  or "neutral").lower()
        setup_type = str(entry.get("setup_type") or "other").lower()
        out[sym] = {
            "catalyst_line": str(entry.get("catalyst_line") or "")[:150],
            "direction":     direction  if direction  in _VALID_DIRECTIONS  else "neutral",
            "setup_type":    setup_type if setup_type in _VALID_SETUP_TYPES else "other",
        }

    # Fill any symbols Claude omitted with the fallback
    for sym, fb in fallback.items():
        if sym not in out:
            logger.debug(f"generate_stock_catalysts: {sym} missing from response — fallback")
            out[sym] = fb

    return out


def generate_edge_report(setups: list[dict[str, Any]]) -> str:
    """
    Generate a plain-text pre-market edge report from ranked setup results.

    # TODO: implement when setup_results table has data
    """
    raise NotImplementedError(
        "generate_edge_report: waiting for setup_results table to be populated"
    )


# ── standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    # Load .env from project root so ANTHROPIC_API_KEY reaches os.environ
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    _TEST_HEADLINES = [
        {
            "headline": "Nifty futures slip 200 points as US markets see sharp selloff overnight",
            "source": "Economic Times",
        },
        {
            "headline": "RBI keeps repo rate unchanged at 6.5%; maintains withdrawal of accommodation stance",
            "source": "Mint",
        },
        {
            "headline": "Reliance Industries Q4 profit beats estimates, up 18% YoY on retail and Jio strength",
            "source": "Business Standard",
        },
        {
            "headline": "FII selling continues for fourth consecutive session; DII support limits losses",
            "source": "Economic Times",
        },
        {
            "headline": "Crude oil surges 3% on Middle East tensions; analysts flag inflation risk for India",
            "source": "Mint",
        },
    ]

    print("\nSending 5 test headlines to Claude...\n")
    try:
        result = analyse_news(_TEST_HEADLINES)
    except ClaudeClientError as exc:
        logger.error(f"Test failed: {exc}")
        sys.exit(1)

    bias = result.get("overall_bias", {})
    print(f"Overall bias : {bias.get('direction', '?').upper()}")
    print(f"Confidence   : {bias.get('confidence', '?')}/5")
    print(f"Reason       : {bias.get('reason', '?')}")
    print(f"\nPer-headline breakdown:")
    for h in result.get("headlines", []):
        syms = h.get("symbols") or []
        print(
            f"  [{h.get('id'):>2}] {h.get('sentiment'):8}  "
            f"importance={h.get('importance')}  {h.get('reason', '')}  "
            f"symbols={syms}"
        )
