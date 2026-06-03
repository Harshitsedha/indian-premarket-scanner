"""
Stock ranking layer for pre-market briefing.
Scores SCAN_WATCHLIST stocks from four independent signals and returns
the top-7 candidates with setup classification.
No API calls, no DB calls -- deterministic computation only.
"""

import re
from pathlib import Path
from typing import Any

from loguru import logger

SCAN_WATCHLIST: list[str] = [
    # Large cap — original 20
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
    "BAJFINANCE", "WIPRO", "ULTRACEMCO", "NESTLEIND", "POWERGRID",
    # Mid cap additions
    "ADANIENT", "ADANIPORTS", "TMPV", "TATASTEEL", "JSWSTEEL",
    "HINDALCO", "COALINDIA", "ONGC", "NTPC", "BPCL",
    "HCLTECH", "TECHM", "DIVISLAB", "DRREDDY", "CIPLA",
    "SUNPHARMA", "APOLLOHOSP", "BAJAJFINSV", "INDUSINDBK", "GRASIM",
    "VEDL", "PIDILITIND", "HAVELLS", "BERGEPAINT", "MARICO",
    "DABUR", "COLPAL", "BRITANNIA", "TATACONSUM", "MUTHOOTFIN",
]

# Keyword aliases for mention detection.
# Each alias uses case-insensitive negative-lookbehind/lookahead so short
# terms ("itc", "sbi") don't false-match inside longer words ("critical").
_NAME_MAP: dict[str, list[str]] = {
    "RELIANCE":    ["reliance", "ril"],
    "TCS":         ["tcs", "tata consultancy"],
    "HDFCBANK":    ["hdfc bank", "hdfcbank", "hdfc"],
    "INFY":        ["infosys", "infy"],
    "ICICIBANK":   ["icici bank", "icicibank", "icici"],
    "HINDUNILVR":  ["hindustan unilever", "hul"],
    "ITC":         ["itc"],
    "SBIN":        ["sbi", "state bank of india", "sbin"],
    "BHARTIARTL":  ["airtel", "bharti airtel"],
    "KOTAKBANK":   ["kotak bank", "kotak mahindra", "kotakbank", "kotak"],
    "LT":          ["l&t", "larsen & toubro", "larsen and toubro"],
    "AXISBANK":    ["axis bank", "axisbank"],
    "ASIANPAINT":  ["asian paints", "asian paint"],
    "MARUTI":      ["maruti", "maruti suzuki"],
    "TITAN":       ["titan"],
    "BAJFINANCE":  ["bajaj finance", "bajfinance"],
    "WIPRO":       ["wipro"],
    "ULTRACEMCO":  ["ultratech cement", "ultratech"],
    "NESTLEIND":   ["nestle"],
    "POWERGRID":   ["power grid", "powergrid"],
    "ADANIENT":    ["adani enterprises", "adani ent"],
    "ADANIPORTS":  ["adani ports", "adaniports"],
    "TMPV":        ["tata motors", "tatamotors", "tata motors pv", "tmpv"],
    "TATASTEEL":   ["tata steel", "tatasteel"],
    "JSWSTEEL":    ["jsw steel", "jswsteel"],
    "HINDALCO":    ["hindalco", "hindalco industries"],
    "COALINDIA":   ["coal india", "coalindia"],
    "ONGC":        ["ongc", "oil and natural gas"],
    "NTPC":        ["ntpc"],
    "BPCL":        ["bpcl", "bharat petroleum"],
    "HCLTECH":     ["hcl tech", "hcltech", "hcl technologies"],
    "TECHM":       ["tech mahindra", "techm"],
    "DIVISLAB":    ["divi's labs", "divis lab", "divislab"],
    "DRREDDY":     ["dr reddy", "dr. reddy", "drreddy"],
    "CIPLA":       ["cipla"],
    "SUNPHARMA":   ["sun pharma", "sunpharma", "sun pharmaceutical"],
    "APOLLOHOSP":  ["apollo hospital", "apollohosp", "apollo hospitals"],
    "BAJAJFINSV":  ["bajaj finserv", "bajajfinsv"],
    "INDUSINDBK":  ["indusind bank", "indusindbk", "indusind"],
    "GRASIM":      ["grasim", "grasim industries"],
    "VEDL":        ["vedanta", "vedl"],
    "PIDILITIND":  ["pidilite", "pidilitind", "fevicol"],
    "HAVELLS":     ["havells"],
    "BERGEPAINT":  ["berger paints", "bergepaint"],
    "MARICO":      ["marico"],
    "DABUR":       ["dabur"],
    "COLPAL":      ["colgate", "colpal", "colgate palmolive"],
    "BRITANNIA":   ["britannia"],
    "TATACONSUM":  ["tata consumer", "tataconsum", "tata consumer products"],
    "MUTHOOTFIN":  ["muthoot finance", "muthootfin", "muthoot"],
}

# Human-readable names for thesis strings
_DISPLAY: dict[str, str] = {
    "RELIANCE":   "Reliance",       "TCS":        "TCS",
    "HDFCBANK":   "HDFC Bank",      "INFY":       "Infosys",
    "ICICIBANK":  "ICICI Bank",     "HINDUNILVR": "HUL",
    "ITC":        "ITC",            "SBIN":       "SBI",
    "BHARTIARTL": "Airtel",         "KOTAKBANK":  "Kotak Bank",
    "LT":         "L&T",            "AXISBANK":   "Axis Bank",
    "ASIANPAINT": "Asian Paints",   "MARUTI":     "Maruti",
    "TITAN":      "Titan",          "BAJFINANCE": "Bajaj Finance",
    "WIPRO":      "Wipro",          "ULTRACEMCO": "UltraTech",
    "NESTLEIND":  "Nestle",         "POWERGRID":  "Power Grid",
    "ADANIENT":   "Adani Ent",      "ADANIPORTS": "Adani Ports",
    "TMPV":       "Tata Motors PV", "TATASTEEL":  "Tata Steel",
    "JSWSTEEL":   "JSW Steel",      "HINDALCO":   "Hindalco",
    "COALINDIA":  "Coal India",     "ONGC":       "ONGC",
    "NTPC":       "NTPC",           "BPCL":       "BPCL",
    "HCLTECH":    "HCL Tech",       "TECHM":      "Tech Mahindra",
    "DIVISLAB":   "Divi's Labs",    "DRREDDY":    "Dr Reddy",
    "CIPLA":      "Cipla",          "SUNPHARMA":  "Sun Pharma",
    "APOLLOHOSP": "Apollo Hosp",    "BAJAJFINSV": "Bajaj Finserv",
    "INDUSINDBK": "IndusInd Bank",  "GRASIM":     "Grasim",
    "VEDL":       "Vedanta",        "PIDILITIND": "Pidilite",
    "HAVELLS":    "Havells",        "BERGEPAINT": "Berger Paints",
    "MARICO":     "Marico",         "DABUR":      "Dabur",
    "COLPAL":     "Colgate",        "BRITANNIA":  "Britannia",
    "TATACONSUM": "Tata Consumer",  "MUTHOOTFIN": "Muthoot Fin",
}

_FII_RE = re.compile(r"FII:\s*([+-]?\d+(?:\.\d+)?)\s*cr", re.IGNORECASE)


# -- helpers ------------------------------------------------------------------

def _mentions_stock(symbol: str, text: str) -> bool:
    """
    True if any alias for symbol appears in text (case-insensitive).
    Uses negative-lookbehind/lookahead on [a-z] so short aliases like
    'itc' or 'sbi' don't match inside longer words.
    """
    lower = text.lower()
    for alias in _NAME_MAP.get(symbol, [symbol.lower()]):
        pattern = r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])"
        if re.search(pattern, lower):
            return True
    return False


def _build_claude_map(claude_analysis: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map headline id -> {sentiment, importance, reason} from Claude output."""
    return {
        int(h["id"]): h
        for h in (claude_analysis.get("headlines") or [])
        if "id" in h
    }


def _market_gap_pct(normalised: dict[str, Any]) -> float:
    """
    Market gap proxy from global_cues.
    Tries sgx_nifty first, then nifty, then falls back to first equity index.
    """
    cues = normalised.get("global_cues") or []
    cue_map = {c.get("name"): c for c in cues if c.get("name")}
    for name in ("sgx_nifty", "nifty", "nikkei", "hangseng"):
        if name in cue_map:
            try:
                return float(cue_map[name].get("change_pct") or 0)
            except (TypeError, ValueError):
                pass
    for cue in cues:
        try:
            return float(cue.get("change_pct") or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _fii_direction(fii_summary: str) -> str:
    """Parse FII net from summary string. Returns 'bullish', 'bearish', or 'unknown'."""
    m = _FII_RE.search(fii_summary or "")
    if not m:
        return "unknown"
    try:
        return "bullish" if float(m.group(1)) >= 0 else "bearish"
    except (ValueError, TypeError):
        return "unknown"


def _dominant_sentiment(claude_entries: list[dict[str, Any]]) -> str:
    """Sentiment of the highest-importance Claude entry among matching headlines."""
    if not claude_entries:
        return "unknown"
    top = max(claude_entries, key=lambda h: h.get("importance") or 0)
    return str(top.get("sentiment") or "neutral").lower()


def _setup_type(
    mention_score: float, fii_score: float, gap_score: float
) -> str:
    if mention_score > 0.3:
        return "news_catalyst"
    if fii_score == 1.0 and mention_score == 0.0:
        return "fii_driven"
    if gap_score > 0.5 and mention_score == 0.0:
        return "gap_play"
    return "watchlist"


def _build_thesis(
    symbol: str,
    setup_type: str,
    mention_count: int,
    top_reason: str | None,
    gap_pct: float,
    fii_dir: str,
    sentiment: str,
) -> str:
    display = _DISPLAY.get(symbol, symbol)
    if setup_type == "news_catalyst":
        noun = "headline" if mention_count == 1 else "headlines"
        if top_reason:
            return f"{mention_count} {noun} mention {display}; {top_reason}"
        return f"{mention_count} {noun} mention {display}"
    if setup_type == "fii_driven":
        return (
            f"No headline mention; FII {fii_dir} flow supports "
            f"a {sentiment} lean on {display}"
        )
    if setup_type == "gap_play":
        return (
            f"No headline mention; {abs(gap_pct):.1f}% market gap "
            f"creates breakout setup for {display}"
        )
    return f"{display} on watchlist; no strong catalyst identified"


# -- public API ---------------------------------------------------------------

def _fetch_stock_gaps(symbols: list[str]) -> dict[str, float]:
    """
    Fetch real per-stock gap % from Upstox pre-market quotes.
    Returns dict: symbol -> gap_pct (float).
    Falls back to empty dict on any error — caller handles the fallback.
    """
    try:
        from ingestion.upstox_client import UpstoxClient
        client = UpstoxClient()
        quotes = client.get_bulk_quotes(symbols)
        return {q["symbol"]: q["gap_pct"] for q in quotes if q is not None}
    except Exception as e:
        logger.warning(f"Upstox gap fetch failed, falling back to market proxy: {e}")
        return {}


def rank_stocks(
    normalised: dict[str, Any],
    claude_analysis: dict[str, Any],
    use_live_gaps: bool = True,
) -> list[dict[str, Any]]:
    """
    Score and rank SCAN_WATCHLIST stocks from four independent signals.

    Args:
        normalised:      output of normaliser.normalise()
        claude_analysis: output of claude_client.analyse_news()

    Returns:
        Top-7 stocks with score > 0, sorted by score descending.
        Each entry: {rank, symbol, score, mention_count, sentiment,
                     setup_type, thesis, signals{...}}
    """
    headlines   = normalised.get("headlines") or []
    claude_map  = _build_claude_map(claude_analysis)
    gap_pct     = _market_gap_pct(normalised)
    fii_dir     = _fii_direction(str(normalised.get("fii_dii_summary") or ""))

    # Attempt live per-stock gaps from Upstox — fall back to market proxy
    stock_gaps: dict[str, float] = {}
    if use_live_gaps:
        stock_gaps = _fetch_stock_gaps(SCAN_WATCHLIST)
        if stock_gaps:
            logger.info(f"Live stock gaps loaded for {len(stock_gaps)} symbols")
        else:
            logger.warning("Live gaps unavailable — using market-wide SGX proxy for all stocks")

    scored: list[dict[str, Any]] = []

    for symbol in SCAN_WATCHLIST:
        # Headlines that mention this stock
        matching = [h for h in headlines if _mentions_stock(symbol, h.get("headline", ""))]
        mention_count = len(matching)

        # Claude entries for those headlines (by id cross-reference)
        claude_entries = [
            claude_map[h["id"]]
            for h in matching
            if h.get("id") in claude_map
        ]

        # Per-stock gap: use real Upstox data if available, else fall back to market proxy
        stock_gap_pct   = stock_gaps.get(symbol, gap_pct)
        stock_gap_score = min(abs(stock_gap_pct) / 3.0, 1.0)

        # Signal: news mention (0-1)
        mention_score = min(mention_count / 3.0, 1.0)

        # Signal: Claude importance (0-1)
        importance_score = (
            max((e.get("importance") or 0) for e in claude_entries) / 5.0
            if claude_entries else 0.0
        )

        # Stock sentiment from highest-importance headline
        sentiment = _dominant_sentiment(claude_entries)

        # Signal: FII alignment (0, 0.5, or 1.0)
        if sentiment == "unknown" or fii_dir == "unknown":
            fii_score = 0.5
        elif sentiment == fii_dir:
            fii_score = 1.0
        else:
            fii_score = 0.0

        # Composite score
        final = round(
            mention_score      * 0.35
            + importance_score * 0.30
            + stock_gap_score  * 0.20
            + fii_score        * 0.15,
            4,
        )

        if final <= 0:
            continue

        setup = _setup_type(mention_score, fii_score, stock_gap_score)

        top_reason: str | None = None
        if claude_entries:
            best = max(claude_entries, key=lambda e: e.get("importance") or 0)
            top_reason = best.get("reason")

        thesis = _build_thesis(
            symbol, setup, mention_count, top_reason, stock_gap_pct, fii_dir, sentiment
        )

        scored.append({
            "symbol":        symbol,
            "score":         final,
            "mention_count": mention_count,
            "sentiment":     sentiment,
            "setup_type":    setup,
            "thesis":        thesis,
            "signals": {
                "news_mention":  round(mention_score,    4),
                "importance":    round(importance_score, 4),
                "gap_potential": round(stock_gap_score,  4),
                "fii_alignment": round(fii_score,        4),
                "gap_pct":       round(stock_gap_pct,    2),
                "gap_source":    "live" if symbol in stock_gaps else "proxy",
            },
        })

    scored.sort(key=lambda s: s["score"], reverse=True)
    top = scored[:7]

    # Assign rank and reorder keys so rank comes first
    result = [{"rank": idx, **entry} for idx, entry in enumerate(top, 1)]

    top_symbols = [e["symbol"] for e in result[:3]]
    gap_source = "live" if stock_gaps else "proxy"
    logger.info(
        f"Ranker: {len(scored)}/{len(SCAN_WATCHLIST)} stocks scored > 0, "
        f"top 3: {top_symbols}  "
        f"(market_gap={gap_pct:+.2f}%, fii={fii_dir}, gap_source={gap_source})"
    )
    return result


# -- standalone test ----------------------------------------------------------
if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from utils.logger import setup_logger

    setup_logger("DEBUG")

    # 6 headlines — 4 stocks mentioned: RELIANCE, INFY, SBIN, BHARTIARTL
    _NORMALISED = {
        "headlines": [
            {"id": 1, "headline": "Reliance Industries Q4 profit beats estimates, up 18% YoY on retail and Jio strength", "source": "Business Standard"},
            {"id": 2, "headline": "Infosys lowers FY25 revenue guidance to 1-3% citing weak demand from BFSI clients", "source": "Mint"},
            {"id": 3, "headline": "SBI Q4 profit jumps 24% on lower provisions; gross NPA ratio improves to 2.2%", "source": "Economic Times"},
            {"id": 4, "headline": "FII selling continues for fourth consecutive session; DII buying limits index losses", "source": "Economic Times"},
            {"id": 5, "headline": "Airtel adds 4.2 million subscribers in March quarter; ARPU rises to record 208 rupees", "source": "Mint"},
            {"id": 6, "headline": "RBI keeps repo rate unchanged at 6.5%; inflation outlook remains within target band", "source": "Business Standard"},
        ],
        "global_cues": [
            {"name": "dow_futures",    "price": 51077.0,  "change_pct":  0.66, "direction": "bullish"},
            {"name": "nasdaq_futures", "price": 30405.25, "change_pct":  0.32, "direction": "bullish"},
            {"name": "sgx_nifty",      "price": 23547.75, "change_pct": -1.50, "direction": "bearish"},
            {"name": "crude_oil",      "price": 87.36,    "change_pct": -1.73, "direction": "bearish"},
            {"name": "gold",           "price": 4593.0,   "change_pct":  2.08, "direction": "bullish"},
            {"name": "usd_inr",        "price": 94.99,    "change_pct":  0.10, "direction": "neutral"},
        ],
        "fii_dii_summary": "FII: -21106 cr | DII: +16764 cr",
        "trading_date": "2026-05-29",
    }

    _CLAUDE = {
        "overall_bias": {
            "direction": "bearish",
            "confidence": 3,
            "reason": "FII outflows and Infosys guidance cut dominate sentiment",
        },
        "headlines": [
            {"id": 1, "sentiment": "bullish", "importance": 5, "reason": "Strong Q4 earnings beat for Reliance"},
            {"id": 2, "sentiment": "bearish", "importance": 4, "reason": "Infosys guidance cut signals IT slowdown"},
            {"id": 3, "sentiment": "bullish", "importance": 4, "reason": "SBI asset quality improvement is positive"},
            {"id": 4, "sentiment": "bearish", "importance": 3, "reason": "Sustained FII outflows pressure indices"},
            {"id": 5, "sentiment": "bullish", "importance": 3, "reason": "Airtel subscriber growth shows momentum"},
            {"id": 6, "sentiment": "neutral", "importance": 2, "reason": "Rate hold was widely expected"},
        ],
    }

    ranked = rank_stocks(_NORMALISED, _CLAUDE, use_live_gaps=False)

    print(f"\n{'- ' * 35}")
    print(f"  Ranked stocks in play  ({len(ranked)} returned)")
    print(f"{'- ' * 35}")
    for s in ranked:
        sig = s["signals"]
        print(
            f"\n  [{s['rank']}] {s['symbol']:12}  score={s['score']:.4f}  "
            f"{s['sentiment']:8}  {s['setup_type']}"
        )
        print(f"      thesis   : {s['thesis']}")
        print(
            f"      signals  : mention={sig['news_mention']:.2f}  "
            f"importance={sig['importance']:.2f}  "
            f"gap={sig['gap_potential']:.2f}  "
            f"fii={sig['fii_alignment']:.2f}"
        )

    # Edge case: no headlines, no cues, no FII
    print(f"\n{'- ' * 35}")
    print(f"  Edge case: empty normalised data")
    print(f"{'- ' * 35}")
    sparse = rank_stocks(
        {"headlines": [], "global_cues": [], "fii_dii_summary": "FII/DII: unavailable"},
        {"overall_bias": {}, "headlines": []},
        use_live_gaps=False,
    )
    print(f"  Stocks returned : {len(sparse)}")
    if sparse:
        print(f"  All scores      : {[s['score'] for s in sparse]}")
        print(f"  All setups      : {list(dict.fromkeys(s['setup_type'] for s in sparse))}")
