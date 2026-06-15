"""
pipeline/realtime/radar_events.py — Phase 1 event emission for the intraday radar.

Detects threshold crossings on the SAME enriched snapshot rows the alert engine sees
(or_status/rvol/gap_pct/atr_multiple/…), but on a path completely separate from
Telegram alert throttling:

  * No per-cycle cap, no RVOL-priority sort, no Telegram — events are a data signal.
  * FIRST CROSSING ONLY: one event per (symbol, event_type, trade_date), deduped via a
    Redis setnx in its own keyspace (parallel to, never colliding with, the alert dedup).
  * The poller does NOT write to Postgres here — each event is one fire-and-forget XADD
    to the radar:events stream, drained by persist_worker.py. Phase 0 isolation intact.

event_type == the alert rule name, so alerts and events share ONE rule source of truth
(pipeline/config/radar_alert_rules.json) and the same _matches() condition logic.

DIRECTION CONVENTION (frozen, resolved + stored at emission — NEVER recomputed in the
labeler). forward_return is signed by this so a correct directional call is always
positive:
  * orb_break_volume : or_status broke_up -> "up", broke_down -> "down"
  * gap_momentum     : gap_pct >= 0 -> "up", else "down"
  * range_expansion  : no inherent direction -> None (labeled UNSIGNED / raw move)
The labeler reads trigger["direction"] and applies +1 ("up") / -1 ("down") /
+1-but-raw (None). It must not guess a direction for a None event.

Public API:
    process_events(rows, rules, trade_date, frame_ts) -> int   # events emitted
"""

import uuid

from loguru import logger

import storage.redis_client as _cache
from realtime.radar_alerts import _matches   # reuse the exact alert condition logic


_EVENTS_STREAM = "radar:events"
_DEDUP_TTL     = 86_400   # 24h — one trading day, survives poller restarts


def _resolve_direction(event_type: str, row: dict) -> str | None:
    """
    Frozen direction convention. Returns "up", "down", or None (unsigned).
    None means "no inherent direction" (range_expansion) — the labeler leaves the
    forward return as a raw signed price move. A return of None here is NOT a failure;
    it is a valid, stored value.
    """
    if event_type == "orb_break_volume":
        status = row.get("or_status")
        if status == "broke_up":
            return "up"
        if status == "broke_down":
            return "down"
        return None   # matched on rvol but no latched break direction — treat as unsigned
    if event_type == "gap_momentum":
        gp = row.get("gap_pct")
        if gp is None:
            return None
        return "up" if gp >= 0 else "down"
    if event_type == "range_expansion":
        return None
    return None


def _build_trigger(row: dict, direction: str | None) -> dict:
    """
    The trigger JSONB: the metric values AT the crossing plus the resolved direction.
    Enough to reconstruct which condition fired without joining back to a snapshot.
    price is the LTP at the crossing — also the labeler's entry price for forward return.
    """
    return {
        "direction":    direction,
        "rvol":         row.get("rvol"),
        "gap_pct":      row.get("gap_pct"),
        "atr_multiple": row.get("atr_multiple"),
        "or_status":    row.get("or_status"),
        "or_break_atr": row.get("or_break_atr"),
        "price":        row.get("ltp"),
    }


def _is_first_crossing(event_type: str, symbol: str, trade_date: str) -> bool:
    """
    Claim the (symbol, event_type, trade_date) slot via Redis setnx in the events'
    OWN keyspace. True only for the first crossing today. Falls back to True (emit) when
    Redis is unavailable — the DB unique index is the backstop against duplicates.
    """
    key = f"radar:event_emitted:{trade_date}:{event_type}:{symbol}"
    return _cache.setnx_ex(key, _DEDUP_TTL)


def process_events(
    rows:       list[dict],
    rules:      list[dict],
    trade_date: str,
    frame_ts:   str,
) -> int:
    """
    Detect first-crossing events on enriched rows and XADD each to radar:events.

    rows      — the same enriched snapshot rows process_alerts sees (post tracker.update).
    rules     — the shared alert/event rule list (radar_alert_rules.json).
    frame_ts  — the frame's generated_at ISO string; becomes the event ts so it aligns
                exactly with the radar_snapshots row the persist worker writes.
    Returns the number of events emitted this cycle (for logging). Never raises — event
    emission is a side channel and must not disturb the poll loop.
    """
    if not rules:
        return 0

    emitted = 0
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        for rule in rules:
            event_type = rule["name"]
            if not _matches(row, rule.get("conditions", {})):
                continue
            if not _is_first_crossing(event_type, symbol, trade_date):
                continue   # already emitted today

            direction = _resolve_direction(event_type, row)
            event = {
                "event_id":   str(uuid.uuid4()),
                "ts":         frame_ts,
                "symbol":     symbol,
                "event_type": event_type,
                "trigger":    _build_trigger(row, direction),
                "regime_id":  None,   # daily_regime deferred (Phase 1 scope: NULL link)
            }
            if _cache.xadd_event(_EVENTS_STREAM, event):
                emitted += 1
                logger.info(
                    f"radar_events: emitted [{event_type}] {symbol} "
                    f"dir={direction} price={row.get('ltp')}"
                )

    if emitted:
        logger.info(f"radar_events: {emitted} event(s) emitted this cycle")
    return emitted
