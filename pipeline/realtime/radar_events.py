"""
pipeline/realtime/radar_events.py — Phase 1 event emission for the intraday radar,
extended in Phase 3 to per-OR-range ORB events.

Detects threshold crossings on the SAME enriched snapshot rows the alert engine sees
(or_status/rvol/gap_pct/atr_multiple/…), but on a path completely separate from
Telegram alert throttling:

  * No per-cycle cap, no RVOL-priority sort, no Telegram — events are a data signal.
  * FIRST CROSSING ONLY: deduped via a Redis setnx in its own keyspace (parallel to,
    never colliding with, the alert dedup).
  * The poller does NOT write to Postgres here — each event is one fire-and-forget XADD
    to the radar:events stream, drained by persist_worker.py. Phase 0 isolation intact.

event_type == the alert rule name, so alerts and events share ONE rule source of truth
(pipeline/config/radar_alert_rules.json).

PHASE 3 — PER-RANGE ORB EVENTS. The OR tracker (orb_ranges.py) tracks EVERY defined OR
window per symbol in row["ranges"][label] = {status, break_atr}, but only the default
09:15–09:30 window is copied into the flat row["or_status"]/or_break_atr (a legacy-compat
shim for the alert engine). Phase 1 read only that flat field, so custom windows
(e.g. 09:45–10:30) were tracked but never emitted. Phase 3 emits ONE orb_break_volume
event per OR window that broke, tagged with which window fired:

  * orb_break_volume is now evaluated PER RANGE off row["ranges"] (incl. the default,
    which flows through the identical path — its behaviour is unchanged plus a tag).
  * gap_momentum / range_expansion are NOT OR-window-specific and stay on the row-level
    path with range_label = None (their dedup is unchanged).
  * The alert engine (radar_alerts.py) still reads the flat row["or_status"] — untouched.

SLICE-KEY SEMANTICS (load-bearing). The canonical slice dimension is range_label — the
TIME-WINDOW string "HH:MM-HH:MM", NOT the human range name. Redefining a range's window
produces a NEW label and therefore starts a NEW sample; events recorded under the old
window stay under the old label and must not be pooled with the new one. A "09:45 ORB"
break and a "09:15 ORB" break are different setups. range_id is stored for provenance
only (a deleted+recreated range gets a fresh id); never slice on it.

DIRECTION CONVENTION (frozen at emission, never recomputed in the labeler):
  * orb_break_volume : the broken range's status broke_up -> "up", broke_down -> "down"
  * gap_momentum     : gap_pct >= 0 -> "up", else "down"
  * range_expansion  : no inherent direction -> None (labeled UNSIGNED / raw move)

Public API:
    process_events(rows, rules, trade_date, frame_ts, range_defs=None) -> int
"""

import uuid

from loguru import logger

import storage.redis_client as _cache
from realtime.radar_alerts import _matches   # reuse the exact alert condition logic

_ORB_EVENT     = "orb_break_volume"
_EVENTS_STREAM = "radar:events"
_DEDUP_TTL     = 86_400   # 24h — one trading day, survives poller restarts
_BREAK_STATES  = ("broke_up", "broke_down")


def _resolve_direction(event_type: str, row: dict) -> str | None:
    """
    Frozen direction convention. Returns "up", "down", or None (unsigned).
    None means "no inherent direction" (range_expansion) — the labeler leaves the
    forward return as a raw signed price move. A return of None here is NOT a failure;
    it is a valid, stored value. For orb_break_volume the caller passes a row carrying
    the BROKEN RANGE'S status in "or_status" (per-range, not the flat default field).
    """
    if event_type == _ORB_EVENT:
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


def _build_trigger(
    row:          dict,
    direction:    str | None,
    *,
    or_status:    str | None = None,
    or_break_atr: float | None = None,
    range_label:  str | None = None,
    range_id:     int | None = None,
) -> dict:
    """
    The trigger JSONB: the metric values AT the crossing plus the resolved direction.
    Enough to reconstruct which condition fired without joining back to a snapshot.
    price is the LTP at the crossing — also the labeler's entry price for forward return.

    For orb_break_volume the caller supplies the BROKEN RANGE'S or_status / or_break_atr
    (from row["ranges"][label]), plus range_label (canonical slice key) and range_id
    (provenance only). For gap_momentum / range_expansion these default to the flat row
    values and range_label/range_id stay None — unchanged from Phase 1.
    range_label is the single source the persist worker reads into the radar_events
    column; range_id lives only here.
    """
    return {
        "direction":    direction,
        "rvol":         row.get("rvol"),
        "gap_pct":      row.get("gap_pct"),
        "atr_multiple": row.get("atr_multiple"),
        "or_status":    or_status if or_status is not None else row.get("or_status"),
        "or_break_atr": or_break_atr if or_break_atr is not None else row.get("or_break_atr"),
        "price":        row.get("ltp"),
        "range_label":  range_label,
        "range_id":     range_id,
    }


def _is_first_crossing(event_type: str, symbol: str, trade_date: str,
                       range_label: str | None = None) -> bool:
    """
    Claim the (symbol, event_type, range, trade_date) slot via Redis setnx in the events'
    OWN keyspace. True only for the first crossing of THIS window today, so a symbol that
    breaks both its default and a custom range emits two distinct events. Non-OR events
    pass range_label=None -> a constant token, preserving one-per-(symbol,type,day).
    Falls back to True (emit) when Redis is unavailable — the DB unique index is the
    backstop. The key gained a range component in Phase 3 (pre-open-only deploy).
    """
    range_token = range_label if range_label is not None else "-"
    key = f"radar:event_emitted:{trade_date}:{event_type}:{symbol}:{range_token}"
    return _cache.setnx_ex(key, _DEDUP_TTL)


def _emit(event_type: str, symbol: str, frame_ts: str, trigger: dict) -> bool:
    """XADD one event to radar:events. Returns True if the XADD succeeded."""
    event = {
        "event_id":   str(uuid.uuid4()),
        "ts":         frame_ts,
        "symbol":     symbol,
        "event_type": event_type,
        "trigger":    trigger,
        "regime_id":  None,   # daily_regime deferred (Phase 1 scope: NULL link)
    }
    if not _cache.xadd_event(_EVENTS_STREAM, event):
        return False
    logger.info(
        f"radar_events: emitted [{event_type}] {symbol} dir={trigger['direction']} "
        f"range={trigger.get('range_label')} price={trigger['price']}"
    )
    return True


def _emit_orb_ranges(row: dict, symbol: str, conds: dict, trade_date: str,
                     frame_ts: str, label_to_def: dict) -> int:
    """
    Per-range ORB emission: one orb_break_volume event per OR window that broke (incl.
    the default). Reads row["ranges"][label] = {status, break_atr} (populated by
    RangeTracker.update); the flat row["or_status"] is NOT consulted here. rvol is
    symbol-level (the rule's rvol_min applies once, same metric the alert engine uses).
    """
    rvol      = row.get("rvol")
    rvol_min  = conds.get("rvol_min")
    if rvol_min is not None and (rvol is None or rvol < rvol_min):
        return 0
    break_states = conds.get("or_status", _BREAK_STATES)

    count = 0
    for label, cell in (row.get("ranges") or {}).items():
        status = cell.get("status")
        if status not in break_states:
            continue
        if not _is_first_crossing(_ORB_EVENT, symbol, trade_date, label):
            continue   # this window already emitted today
        range_id  = (label_to_def.get(label) or {}).get("id")
        direction = _resolve_direction(_ORB_EVENT, {"or_status": status})
        trigger   = _build_trigger(
            row, direction,
            or_status=status, or_break_atr=cell.get("break_atr"),
            range_label=label, range_id=range_id,
        )
        if _emit(_ORB_EVENT, symbol, frame_ts, trigger):
            count += 1
    return count


def process_events(
    rows:       list[dict],
    rules:      list[dict],
    trade_date: str,
    frame_ts:   str,
    range_defs: list[dict] | None = None,
) -> int:
    """
    Detect first-crossing events on enriched rows and XADD each to radar:events.

    rows       — the same enriched snapshot rows process_alerts sees (post tracker.update);
                 orb emission requires row["ranges"] (set by RangeTracker.update).
    rules      — the shared alert/event rule list (radar_alert_rules.json).
    frame_ts   — the frame's generated_at ISO string; becomes the event ts.
    range_defs — tracker.defs(): label -> {id, is_default, …}, used to stamp range_id on
                 ORB events (provenance). Option (a): the emitter stays a pure function of
                 the rows + this def list; orb_ranges.py is NOT modified, so the frames
                 path it also feeds is untouched.
    Returns the number of events emitted this cycle. Never raises — event emission is a
    side channel and must not disturb the poll loop.
    """
    if not rules:
        return 0

    label_to_def = {d["label"]: d for d in (range_defs or [])}
    emitted = 0
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue
        for rule in rules:
            event_type = rule["name"]
            conds      = rule.get("conditions", {})

            if event_type == _ORB_EVENT:
                # Per-range path (Phase 3): one event per broken OR window.
                emitted += _emit_orb_ranges(
                    row, symbol, conds, trade_date, frame_ts, label_to_def
                )
                continue

            # Row-level path (gap_momentum / range_expansion): not OR-specific, so
            # range_label stays None and dedup is one-per-(symbol,type,day) — unchanged.
            if not _matches(row, conds):
                continue
            if not _is_first_crossing(event_type, symbol, trade_date):
                continue
            direction = _resolve_direction(event_type, row)
            if _emit(event_type, symbol, frame_ts, _build_trigger(row, direction)):
                emitted += 1

    if emitted:
        logger.info(f"radar_events: {emitted} event(s) emitted this cycle")
    return emitted
