"""
pipeline/realtime/radar_alerts.py — Telegram alert engine for the intraday radar.

Evaluates alert rules (pipeline/config/radar_alert_rules.json) against each completed
snapshot. Each (rule, symbol) fires at most once per trading day, deduped via Redis
setnx — survives poller restarts. Sends via httpx (same pattern as the poller's 401
alert, no separate bot client).

Public API:
    load_rules()                                               -> list[dict]
    process_alerts(rows, rules, trade_date, market_open, stale) -> None
"""

import html
import json
from pathlib import Path

import httpx
from loguru import logger

from utils.config import settings
import storage.redis_client as _cache

_RULES_PATH           = Path(__file__).resolve().parents[1] / "config" / "radar_alert_rules.json"
_ALERT_TTL            = 86_400   # 24h — full trading day
_MAX_ALERTS_PER_CYCLE = 15


# ── rule loading ──────────────────────────────────────────────────────────────

def load_rules() -> list[dict]:
    """Load alert rules from JSON config. Returns [] on any read/parse error."""
    try:
        return json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"radar_alerts: failed to load rules ({_RULES_PATH}): {exc}")
        return []


# ── rule evaluation ───────────────────────────────────────────────────────────

def _matches(row: dict, conds: dict) -> bool:
    """Return True if a snapshot row satisfies all conditions in a rule."""
    if "or_status" in conds:
        if row.get("or_status") not in conds["or_status"]:
            return False
    if "rvol_min" in conds:
        rv = row.get("rvol")
        if rv is None or rv < conds["rvol_min"]:
            return False
    if "abs_gap_pct_min" in conds:
        gp = row.get("gap_pct")
        if gp is None or abs(gp) < conds["abs_gap_pct_min"]:
            return False
    if "atr_multiple_min" in conds:
        atm = row.get("atr_multiple")
        if atm is None or atm < conds["atr_multiple_min"]:
            return False
    return True


# ── dedup ─────────────────────────────────────────────────────────────────────

def _is_new_alert(rule_name: str, symbol: str, trade_date: str) -> bool:
    """
    Return True (and claim the slot) if this (rule, symbol) has not been alerted today.
    Uses Redis setnx so the answer is consistent across poller restarts.
    Falls back to True (fire) when Redis is unavailable — never silently drops an alert.
    """
    key = f"radar:alerted:{trade_date}:{rule_name}:{symbol}"
    return _cache.setnx_ex(key, _ALERT_TTL)


# ── message formatting ────────────────────────────────────────────────────────

def _rule_title(rule_name: str, row: dict) -> str:
    or_status = row.get("or_status") or ""
    if rule_name == "orb_break_volume":
        direction = "UP" if or_status == "broke_up" else "DOWN"
        return f"ORB BREAK {direction}"
    if rule_name == "gap_momentum":
        gp = row.get("gap_pct") or 0
        return "GAP UP MOMENTUM" if gp >= 0 else "GAP DOWN MOMENTUM"
    if rule_name == "range_expansion":
        return "RANGE EXPANSION"
    return rule_name.upper().replace("_", " ")


def _format_message(row: dict, rule_name: str) -> str:
    sym          = html.escape(str(row.get("symbol", "")))
    title        = _rule_title(rule_name, row)
    ltp          = row.get("ltp")
    change_pct   = row.get("change_pct")
    gap_pct      = row.get("gap_pct")
    rvol         = row.get("rvol")
    atr_mult     = row.get("atr_multiple")
    or_high      = row.get("or_high")
    or_low       = row.get("or_low")
    or_break_atr = row.get("or_break_atr")
    catalyst     = row.get("catalyst_line")

    ltp_str  = f"{ltp:.2f}"      if ltp        is not None else "—"
    chg_str  = f"{change_pct:+.2f}%" if change_pct is not None else "—"
    gap_str  = f"{gap_pct:+.2f}%" if gap_pct   is not None else "—"
    rvol_str = f"{rvol:.1f}"     if rvol       is not None else "—"
    atr_str  = f"{atr_mult:.1f}" if atr_mult   is not None else "—"

    lines = [f"⚡ <b>{sym}</b> — {title}"]
    lines.append(f"LTP {ltp_str} ({chg_str}) | Gap {gap_str} | RVOL {rvol_str} | ATRx {atr_str}")

    if or_high is not None and or_low is not None:
        or_line = f"OR {or_low:.2f}–{or_high:.2f}"
        if or_break_atr is not None:
            or_line += f" | broke {or_break_atr:.1f} ATR"
        lines.append(or_line)

    if catalyst:
        lines.append(f"📰 {html.escape(str(catalyst))}")

    return "\n".join(lines)


# ── send ──────────────────────────────────────────────────────────────────────

def _send_alert(text: str) -> None:
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id":    settings.telegram_chat_id,
                "text":       text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as exc:
        logger.error(f"radar_alerts: Telegram send failed: {exc}")


# ── public API ────────────────────────────────────────────────────────────────

def process_alerts(
    rows:        list[dict],
    rules:       list[dict],
    trade_date:  str,
    market_open: bool,
    stale:       bool = False,
) -> None:
    """
    Evaluate rules against snapshot rows, dedup via Redis, send Telegram alerts.
    No-ops immediately if market is not open, snapshot is stale, or rules list is empty.
    Sends at most _MAX_ALERTS_PER_CYCLE messages per call, prioritised by RVOL descending.
    Any qualifying (rule, symbol) pairs beyond the cap are logged and skipped.
    """
    if not market_open or stale or not rules:
        return

    # Collect all qualifying (rule_name, row) pairs
    candidates: list[tuple[str, dict]] = []
    for row in rows:
        for rule in rules:
            if _matches(row, rule.get("conditions", {})):
                candidates.append((rule["name"], row))

    if not candidates:
        return

    # Sort by RVOL descending so strongest signals fire when cap is hit
    candidates.sort(key=lambda x: x[1].get("rvol") or 0.0, reverse=True)

    sent    = 0
    skipped = 0

    for rule_name, row in candidates:
        symbol = row.get("symbol", "")
        if not _is_new_alert(rule_name, symbol, trade_date):
            continue   # already fired today

        if sent >= _MAX_ALERTS_PER_CYCLE:
            skipped += 1
            continue

        _send_alert(_format_message(row, rule_name))
        sent += 1
        logger.info(f"radar_alerts: sent [{rule_name}] {symbol}")

    if skipped:
        logger.warning(
            f"radar_alerts: {skipped} alert(s) suppressed (cap={_MAX_ALERTS_PER_CYCLE}/cycle)"
        )
