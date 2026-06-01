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
      "reason": "5-8 words"
    }
  ]
}"""

# confidence: 1-5  (5 = very high conviction)
# importance: 1-5  (5 = market-moving, 4 = significant, 3 = moderate, 2 = minor, 1 = noise)

_SYSTEM_EDGE = (
    "You are a pre-market edge analyst. "
    "Identify high-probability intraday setups from the provided data. "
    "Write in plain text — no markdown, no JSON."
)


# ── internal helpers ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers Claude sometimes adds."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


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
        response = _call_claude(client, _SYSTEM_NEWS, prompt, max_tokens=1024)
    except anthropic.APIError as exc:
        raise ClaudeClientError(f"analyse_news: API failed after retries: {exc}") from exc

    _log_usage("analyse_news", response)
    raw = response.content[0].text

    try:
        result: dict[str, Any] = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        logger.error(f"Claude returned non-JSON:\n{raw[:300]}")
        raise ClaudeClientError(
            f"JSON parse failed: {exc} | raw snippet: {raw[:200]}"
        ) from exc

    return result


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
        print(
            f"  [{h.get('id'):>2}] {h.get('sentiment'):8}  "
            f"importance={h.get('importance')}  {h.get('reason', '')}"
        )
