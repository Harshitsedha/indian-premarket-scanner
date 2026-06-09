"""
Tests for market_snapshot.py:
- Graceful stale tile on yfinance failure
- Correct change_pct arithmetic
- All instruments always returned (never raises)
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.market_snapshot import fetch_snapshot, _INSTRUMENTS


class TestFetchSnapshot:
    def _make_df(self, prev_close: float, last_close: float) -> pd.DataFrame:
        return pd.DataFrame(
            {"Close": [prev_close, last_close]},
            index=pd.date_range("2026-06-09", periods=2, freq="D"),
        )

    def test_returns_all_instruments(self):
        """fetch_snapshot always returns exactly len(_INSTRUMENTS) tiles."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.side_effect = Exception("network error")
            tiles = fetch_snapshot()
        assert len(tiles) == len(_INSTRUMENTS)

    def test_stale_on_yfinance_error(self):
        """Tiles are stale=True when yfinance raises."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.side_effect = RuntimeError("timeout")
            tiles = fetch_snapshot()
        for t in tiles:
            assert t["stale"] is True
            assert t["last"] is None
            assert t["change_pct"] is None

    def test_stale_on_insufficient_rows(self):
        """Tile is stale when fewer than 2 rows returned."""
        with patch("yfinance.Ticker") as mock_ticker:
            single_row = pd.DataFrame(
                {"Close": [24000.0]},
                index=pd.date_range("2026-06-09", periods=1, freq="D"),
            )
            mock_ticker.return_value.history.return_value = single_row
            tiles = fetch_snapshot()
        for t in tiles:
            assert t["stale"] is True

    def test_correct_change_pct(self):
        """change_pct = (last - prev) / prev * 100, rounded to 4dp."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = self._make_df(24000.0, 24240.0)
            tiles = fetch_snapshot()
        for t in tiles:
            assert t["stale"] is False
            assert t["last"] == pytest.approx(24240.0)
            assert t["change_pct"] == pytest.approx(1.0)

    def test_stale_false_on_good_data(self):
        """stale=False when valid data returned."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = self._make_df(100.0, 105.0)
            tiles = fetch_snapshot()
        assert all(not t["stale"] for t in tiles)

    def test_partial_failures_isolated(self):
        """One ticker failing does not make other tiles stale."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first ticker fails")
            return self._make_df(100.0, 102.0)

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.side_effect = side_effect
            tiles = fetch_snapshot()

        stale = [t for t in tiles if t["stale"]]
        ok    = [t for t in tiles if not t["stale"]]
        assert len(stale) == 1
        assert len(ok) == len(_INSTRUMENTS) - 1

    def test_tile_structure(self):
        """Each tile has required fields with correct types."""
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = self._make_df(50.0, 51.0)
            tiles = fetch_snapshot()

        for t in tiles:
            assert "label"      in t and isinstance(t["label"], str)
            assert "category"   in t and t["category"] in {"index", "commodity", "fx"}
            assert "last"       in t
            assert "prev_close" in t
            assert "change_pct" in t
            assert "asof"       in t and isinstance(t["asof"], str)
            assert "stale"      in t and isinstance(t["stale"], bool)
