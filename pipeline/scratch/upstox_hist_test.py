"""
Standalone test: Upstox historical daily candles for RELIANCE, INFY, TCS.
Confirms Upstox can return previous-day close prices as a bhavcopy alternative.

Run on the server:
    cd /opt/premarket/pipeline
    source .venv/bin/activate
    python scratch/upstox_hist_test.py
"""

import sys
from pathlib import Path

# pipeline/ must be on sys.path so ingestion.* and utils.* resolve
_PIPELINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PIPELINE))

from dotenv import load_dotenv
load_dotenv(_PIPELINE.parent / ".env")

from loguru import logger  # noqa: E402
from utils.logger import setup_logger  # noqa: E402
from ingestion.upstox_client import UpstoxClient  # noqa: E402

SYMBOLS = ["RELIANCE", "INFY", "TCS"]


def main() -> None:
    setup_logger("INFO")

    logger.info("Initialising UpstoxClient (reads token from DB or env)...")
    client = UpstoxClient()

    print(f"\n{'─' * 55}")
    print(f"{'Symbol':<15} {'Date':<12} {'Prev Close (₹)':>14}")
    print(f"{'─' * 55}")

    for symbol in SYMBOLS:
        try:
            result = client.get_prev_close(symbol)
            if result:
                print(f"{result['symbol']:<15} {result['date']:<12} {result['close']:>14.2f}")
            else:
                print(f"{symbol:<15} {'—':<12} {'None returned':>14}")
        except Exception as exc:
            print(f"{symbol:<15} ERROR: {exc}")

    print(f"{'─' * 55}\n")


if __name__ == "__main__":
    main()
