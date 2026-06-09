"""
Tests for the quality-threshold filtering added to ranker.rank_stocks().

All tests run with use_live_gaps=False so no network calls are made.
"""
import sys
from pathlib import Path

import pytest

# Make pipeline/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing.ranker import rank_stocks, SALIENCE_THRESHOLD, _GAP_OVERRIDE_PCT, _MENTION_OVERRIDE


# ── fixtures ──────────────────────────────────────────────────────────────────

def _normalised(extra_cues: list | None = None, fii="FII: +1000 cr | DII: +500 cr"):
    return {
        "headlines": [],
        "global_cues": extra_cues or [],
        "fii_dii_summary": fii,
        "trading_date": "2026-06-01",
    }


def _analysis(bias="neutral", confidence=3):
    return {
        "overall_bias": {"direction": bias, "confidence": confidence, "reason": "test"},
        "headlines": [],
    }


def _headline(hid, headline, source="Test", sentiment="neutral", importance=3, symbols=None):
    return {
        "id": hid,
        "headline": headline,
        "source": source,
        "sentiment": sentiment,
        "importance": importance,
        "symbols": symbols or [],
    }


# ── threshold gate ────────────────────────────────────────────────────────────

class TestQualityThreshold:
    def test_zero_score_excluded(self):
        """A stock with score=0 is never included."""
        norm = _normalised()
        result = rank_stocks(norm, _analysis(), use_live_gaps=False)
        # With no headlines and no cues, all stocks score 0 and are excluded
        assert result == []

    def test_single_weak_mention_excluded(self):
        """1 mention with low importance and no gap → below threshold, excluded."""
        # RELIANCE gets exactly 1 mention → mention_score = 0.333
        # importance = 1 → importance_score = 0.2
        # move_score = 0 (no cue data)
        # fii_alignment = 0.5 (unknown sentiment)
        # composite = 0.333*0.35 + 0.2*0.30 + 0 + 0.5*0.15 = 0.116 + 0.060 + 0 + 0.075 = 0.251
        # 0.251 < SALIENCE_THRESHOLD (0.30), abs(gap)=0 < 2%, mentions=1 < 2 → EXCLUDED
        norm = _normalised()
        norm["headlines"] = [_headline(1, "Reliance reports mixed numbers", importance=1)]
        analysis = {
            "overall_bias": {"direction": "neutral", "confidence": 3, "reason": "test"},
            "headlines": [{"id": 1, "sentiment": "neutral", "importance": 1, "reason": "weak"}],
        }
        result = rank_stocks(norm, analysis, use_live_gaps=False)
        symbols = [s["symbol"] for s in result]
        assert "RELIANCE" not in symbols

    def test_two_mentions_override(self):
        """2+ mentions bypass the salience floor regardless of score."""
        norm = _normalised()
        norm["headlines"] = [
            _headline(1, "Reliance Q4 results beat", importance=2),
            _headline(2, "Reliance Jio expansion update", importance=2),
        ]
        analysis = {
            "overall_bias": {"direction": "neutral", "confidence": 3, "reason": "test"},
            "headlines": [
                {"id": 1, "sentiment": "bullish", "importance": 2, "reason": ""},
                {"id": 2, "sentiment": "bullish", "importance": 2, "reason": ""},
            ],
        }
        result = rank_stocks(norm, analysis, use_live_gaps=False)
        symbols = [s["symbol"] for s in result]
        assert "RELIANCE" in symbols

    def test_high_gap_override(self):
        """A stock with abs(gap) >= 2% bypasses the salience floor even with no news."""
        # Inject a high SGX Nifty gap so the market proxy is >= 2%
        norm = _normalised(extra_cues=[
            {"name": "sgx_nifty", "price": 24000.0, "change_pct": 2.5, "direction": "bullish"}
        ])
        result = rank_stocks(norm, _analysis(), use_live_gaps=False)
        # All watchlist stocks get the proxy gap of 2.5% → override applies
        # Their score = 0 + 0 + min(2.5/3,1)*0.20 + 0.5*0.15 = 0.1667 + 0.075 = 0.2417
        # Score < 0.30 but abs(gap)=2.5 >= 2.0 → included
        assert len(result) > 0
        for s in result:
            assert s["signals"]["prior_session_gap_pct"] == pytest.approx(2.5)

    def test_high_salience_score_included(self):
        """A stock with score >= SALIENCE_THRESHOLD is included without other overrides."""
        # RELIANCE: 3+ mentions, high importance, positive FII → score well above 0.30
        norm = _normalised()
        norm["headlines"] = [
            _headline(1, "Reliance Q4 earnings beat estimates sharply", importance=5),
            _headline(2, "Reliance Jio ARPU jumps to record high", importance=4),
            _headline(3, "Reliance retail revenue up 20%", importance=4),
        ]
        analysis = {
            "overall_bias": {"direction": "bullish", "confidence": 4, "reason": "test"},
            "headlines": [
                {"id": 1, "sentiment": "bullish", "importance": 5, "reason": ""},
                {"id": 2, "sentiment": "bullish", "importance": 4, "reason": ""},
                {"id": 3, "sentiment": "bullish", "importance": 4, "reason": ""},
            ],
        }
        result = rank_stocks(norm, analysis, use_live_gaps=False)
        symbols = [s["symbol"] for s in result]
        assert "RELIANCE" in symbols
        rel = next(s for s in result if s["symbol"] == "RELIANCE")
        assert rel["score"] >= SALIENCE_THRESHOLD

    def test_no_hard_cap_at_seven(self):
        """If multiple stocks all clear the threshold, more than 7 may be returned."""
        # Use high SGX cue to push many stocks above threshold via gap override
        norm = _normalised(extra_cues=[
            {"name": "sgx_nifty", "price": 24000.0, "change_pct": 3.0, "direction": "bullish"}
        ])
        result = rank_stocks(norm, _analysis("bullish", 5), use_live_gaps=False)
        # With a 3% gap all 52 watchlist stocks should score > 0 and gap-override applies
        # Previously capped at 7; now all qualifying stocks are returned
        assert len(result) > 7

    def test_rank_starts_at_one(self):
        """Rank field starts at 1 and is sequential."""
        norm = _normalised(extra_cues=[
            {"name": "sgx_nifty", "price": 24000.0, "change_pct": 2.5, "direction": "bullish"}
        ])
        result = rank_stocks(norm, _analysis(), use_live_gaps=False)
        ranks = [s["rank"] for s in result]
        assert ranks == list(range(1, len(result) + 1))

    def test_constants_match_expectations(self):
        """Sanity check threshold constants haven't been silently changed."""
        assert SALIENCE_THRESHOLD == pytest.approx(0.30)
        assert _GAP_OVERRIDE_PCT  == pytest.approx(2.0)
        assert _MENTION_OVERRIDE  == 2
