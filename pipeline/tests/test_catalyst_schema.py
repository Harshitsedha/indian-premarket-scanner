"""
Tests for claude_client.generate_stock_catalysts():
- Enum validation: bad direction/setup_type fall back to neutral/other
- Missing symbol from Claude response gets the fallback values
- Empty input returns empty dict without calling Claude
- API error returns graceful fallback, no exception raised
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing.claude_client import (
    generate_stock_catalysts,
    _VALID_DIRECTIONS,
    _VALID_SETUP_TYPES,
)


def _make_stock(symbol: str, thesis="test thesis", sentiment="bullish", gap=1.5, mentions=1):
    return {
        "symbol": symbol,
        "thesis": thesis,
        "sentiment": sentiment,
        "mention_count": mentions,
        "signals": {"prior_session_gap_pct": gap},
    }


def _claude_response(stocks_list: list) -> MagicMock:
    import json
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps({"stocks": stocks_list}))]
    msg.usage   = MagicMock(input_tokens=100, output_tokens=50)
    return msg


class TestGenerateStockCatalysts:
    def test_empty_input_no_api_call(self):
        """Empty ranked_stocks returns {} without touching the Anthropic API."""
        with patch("anthropic.Anthropic") as mock_client:
            result = generate_stock_catalysts([], [])
        mock_client.assert_not_called()
        assert result == {}

    def test_valid_direction_and_setup_preserved(self):
        """Valid direction and setup_type pass through unchanged."""
        stock = _make_stock("RELIANCE")
        claude_output = [
            {
                "symbol": "RELIANCE",
                "catalyst_line": "Q4 profit beats; Jio ARPU expansion drives gap-up",
                "direction": "bullish",
                "setup_type": "news_momentum",
            }
        ]
        with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
            result = generate_stock_catalysts([stock], [])

        assert result["RELIANCE"]["direction"]  == "bullish"
        assert result["RELIANCE"]["setup_type"] == "news_momentum"
        assert "Q4 profit beats" in result["RELIANCE"]["catalyst_line"]

    def test_invalid_direction_falls_back_to_neutral(self):
        """Unknown direction string → 'neutral'."""
        stock = _make_stock("INFY")
        claude_output = [
            {
                "symbol": "INFY",
                "catalyst_line": "Guidance cut weighs on IT sentiment",
                "direction": "VERY_BEARISH",       # invalid
                "setup_type": "news_momentum",
            }
        ]
        with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
            result = generate_stock_catalysts([stock], [])

        assert result["INFY"]["direction"] == "neutral"

    def test_invalid_setup_type_falls_back_to_other(self):
        """Unknown setup_type → 'other'."""
        stock = _make_stock("TCS")
        claude_output = [
            {
                "symbol": "TCS",
                "catalyst_line": "IT sector rotation play",
                "direction": "neutral",
                "setup_type": "random_invented_type",   # invalid
            }
        ]
        with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
            result = generate_stock_catalysts([stock], [])

        assert result["TCS"]["setup_type"] == "other"

    def test_missing_symbol_uses_fallback(self):
        """Symbol omitted from Claude response gets thesis/sentiment fallback."""
        stocks = [_make_stock("SBIN", thesis="SBI watchlist play", sentiment="bearish")]
        claude_output = []   # Claude returns nothing
        with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
            result = generate_stock_catalysts(stocks, [])

        assert "SBIN" in result
        assert result["SBIN"]["catalyst_line"] == "SBI watchlist play"
        assert result["SBIN"]["direction"]     == "bearish"
        assert result["SBIN"]["setup_type"]    == "other"

    def test_api_error_returns_full_fallback(self):
        """API error → graceful fallback for all stocks, no exception raised."""
        import anthropic
        stocks = [
            _make_stock("RELIANCE", thesis="Reliance thesis", sentiment="bullish"),
            _make_stock("TCS",      thesis="TCS thesis",      sentiment="neutral"),
        ]
        with patch("processing.claude_client._call_claude", side_effect=anthropic.APIError("fail", request=MagicMock(), body=None)):
            result = generate_stock_catalysts(stocks, [])   # must not raise

        assert "RELIANCE" in result and "TCS" in result
        assert result["RELIANCE"]["catalyst_line"] == "Reliance thesis"
        assert result["TCS"]["setup_type"]         == "other"

    def test_json_parse_error_returns_fallback(self):
        """Malformed Claude JSON → fallback, no exception."""
        bad_response = MagicMock()
        bad_response.content = [MagicMock(text="not json at all { garbage")]
        bad_response.usage   = MagicMock(input_tokens=10, output_tokens=5)

        stocks = [_make_stock("WIPRO", thesis="Wipro thesis", sentiment="neutral")]
        with patch("processing.claude_client._call_claude", return_value=bad_response):
            result = generate_stock_catalysts(stocks, [])

        assert "WIPRO" in result
        assert result["WIPRO"]["catalyst_line"] == "Wipro thesis"

    def test_catalyst_line_truncated_at_150_chars(self):
        """catalyst_line longer than 150 chars is truncated."""
        stock = _make_stock("LT")
        long_line = "A" * 200
        claude_output = [
            {
                "symbol": "LT",
                "catalyst_line": long_line,
                "direction": "bullish",
                "setup_type": "news_momentum",
            }
        ]
        with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
            result = generate_stock_catalysts([stock], [])

        assert len(result["LT"]["catalyst_line"]) <= 150

    def test_all_valid_directions_accepted(self):
        """All enum members in _VALID_DIRECTIONS pass through unchanged."""
        for direction in _VALID_DIRECTIONS:
            stock = _make_stock("AXISBANK")
            claude_output = [
                {"symbol": "AXISBANK", "catalyst_line": "test", "direction": direction, "setup_type": "other"}
            ]
            with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
                result = generate_stock_catalysts([stock], [])
            assert result["AXISBANK"]["direction"] == direction

    def test_all_valid_setup_types_accepted(self):
        """All enum members in _VALID_SETUP_TYPES pass through unchanged."""
        for stype in _VALID_SETUP_TYPES:
            stock = _make_stock("KOTAKBANK")
            claude_output = [
                {"symbol": "KOTAKBANK", "catalyst_line": "test", "direction": "neutral", "setup_type": stype}
            ]
            with patch("processing.claude_client._call_claude", return_value=_claude_response(claude_output)):
                result = generate_stock_catalysts([stock], [])
            assert result["KOTAKBANK"]["setup_type"] == stype
