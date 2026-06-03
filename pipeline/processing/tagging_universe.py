"""
Loads the headline symbol-tagging universe from pipeline/data/tagging_universe.txt.

TAGGING_UNIVERSE_SET is a frozenset of uppercase NSE trading symbols used to
hard-validate whatever symbols Claude returns before they are persisted.
Symbols not in this set are silently dropped.

To expand the universe: edit pipeline/data/tagging_universe.txt (one symbol
per line, # comments allowed) and restart the pipeline process — no code change needed.
"""

from pathlib import Path

_UNIVERSE_FILE = Path(__file__).resolve().parents[1] / "data" / "tagging_universe.txt"


def _load() -> frozenset[str]:
    if not _UNIVERSE_FILE.exists():
        raise FileNotFoundError(
            f"Tagging universe file not found: {_UNIVERSE_FILE}\n"
            "Create pipeline/data/tagging_universe.txt with one NSE symbol per line."
        )
    symbols: set[str] = set()
    for line in _UNIVERSE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            symbols.add(line.upper())
    return frozenset(symbols)


TAGGING_UNIVERSE_SET: frozenset[str] = _load()
