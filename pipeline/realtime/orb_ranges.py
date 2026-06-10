"""
pipeline/realtime/orb_ranges.py — multi-range Opening Range tracking engine.

One code path for every OR window, including the classic 09:15-09:30 default
(seeded as a standard row in orb_range_defs). The poller feeds each cycle's
rows through RangeTracker.update(), which:

  1. materialises every range whose window has closed (ONCE, from the per-poll
     intraday history at radar:polls:{date}:{symbol})
  2. computes latched break status / break-ATR per symbol per range
  3. tracks first_break_side, break_time, rvol_at_break, break_atr_max
  4. annotates snapshot rows: row["ranges"][label] = {status, break_atr},
     plus the legacy or_high/or_low/or_status/or_break_atr fields from the
     default range so existing consumers keep working unchanged.

Redis keys (all TTL 24h):
  radar:or:{date}:{HH:MM}-{HH:MM}       HASH symbol -> {or_high, or_low, frozen_at}
                                         plus "_meta" -> {frozen_at} (freeze marker)
  radar:or_state:{date}:{HH:MM}-{HH:MM} HASH symbol -> break state dict
"""

from datetime import date, datetime, time as dtime

import psycopg2
from loguru import logger

from utils.config import settings
import storage.redis_client as _cache

_OR_TTL = 86_400   # 24h — survives poller restarts, cleaned up by the EOD job

_POLLS_KEY_FMT = "radar:polls:{date}:{symbol}"
_OR_KEY_FMT    = "radar:or:{date}:{label}"
_STATE_KEY_FMT = "radar:or_state:{date}:{label}"

_META_FIELD = "_meta"   # freeze marker inside the radar:or hash

SESSION_START = dtime(9, 15)
SESSION_END   = dtime(15, 30)

MAX_ACTIVE_RANGES = 6   # poller cost control — enforced by the API too


# ── range definitions ─────────────────────────────────────────────────────────

def default_or_end() -> dtime:
    """End of the default OR window: 09:15 + settings.or_window_minutes."""
    total = SESSION_START.hour * 60 + SESSION_START.minute + settings.or_window_minutes
    return dtime(total // 60, total % 60)


def range_label(start: dtime, end: dtime) -> str:
    return f"{start:%H:%M}-{end:%H:%M}"


def _is_default(d: dict) -> bool:
    return (
        d["scope"] == "standard"
        and d["start"] == SESSION_START
        and d["end"] == default_or_end()
    )


def builtin_default_def() -> dict:
    """In-code fallback so the poller works before migration 012 is applied."""
    end = default_or_end()
    return {
        "id":     0,
        "name":   f"Default OR {settings.or_window_minutes}m",
        "start":  SESSION_START,
        "end":    end,
        "label":  range_label(SESSION_START, end),
        "scope":  "standard",
        "is_default": True,
    }


def load_range_defs(trade_date: str) -> list[dict]:
    """
    Load today's active range defs (standard + today's session-scoped) from DB.
    Falls back to the built-in default range on any DB error (e.g. table missing).
    """
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host, port=settings.postgres_port,
            dbname=settings.postgres_db, user=settings.postgres_user,
            password=settings.postgres_password,
        )
        defs: list[dict] = []
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, or_start, or_end, scope
                FROM   orb_range_defs
                WHERE  active = TRUE
                  AND  (scope = 'standard' OR (scope = 'session' AND session_date = %s))
                ORDER BY or_start, or_end
                """,
                (trade_date,),
            )
            for rid, name, start, end, scope in cur.fetchall():
                d = {
                    "id": rid, "name": name, "start": start, "end": end,
                    "label": range_label(start, end), "scope": scope,
                }
                d["is_default"] = _is_default(d)
                defs.append(d)
        conn.close()
        if not defs:
            logger.warning("orb_ranges: no active range defs in DB — using built-in default")
            return [builtin_default_def()]
        return defs[:MAX_ACTIVE_RANGES]
    except Exception as exc:
        logger.warning(f"orb_ranges: range defs load failed ({exc}) — using built-in default")
        return [builtin_default_def()]


# ── window high/low from poll history ─────────────────────────────────────────

def compute_window_hl(records: list[dict], anchored: bool) -> tuple[float | None, float | None]:
    """
    Derive a window's high/low from per-poll records ({"t","h","l","p","v"})
    already filtered to the window.

    Anchored windows (start == 09:15): the day-cumulative h/l at the last poll
    IS the window h/l — exact.

    Non-anchored windows: day h/l includes pre-window movement, so use the max/min
    of sampled prices, extended by the day h/l only when it moved DURING the window
    (last vs first record) — that move must have happened inside the window.
    """
    if not records:
        return None, None
    first, last = records[0], records[-1]

    if anchored:
        hi = last.get("h") if last.get("h") is not None else None
        lo = last.get("l") if last.get("l") is not None else None
        if hi is not None and lo is not None:
            return hi, lo
        # fall through to price-sample estimate if h/l missing

    prices = [r["p"] for r in records if r.get("p") is not None]
    hi = max(prices) if prices else None
    lo = min(prices) if prices else None

    lh, fh = last.get("h"), first.get("h")
    if lh is not None and fh is not None and lh > fh:
        hi = lh if hi is None else max(hi, lh)
    ll, fl = last.get("l"), first.get("l")
    if ll is not None and fl is not None and ll < fl:
        lo = ll if lo is None else min(lo, ll)

    return hi, lo


# ── break status (same latch semantics as the original single-OR logic) ───────

def compute_break_status(
    prev_status:   str,
    ltp:           float | None,
    or_high:       float,
    or_low:        float,
    prev_close:    float | None,
    avg_range_pct: float | None,
) -> tuple[str, float | None]:
    """
    Latched OR status + break distance in ATR units.
      - Once "broke_up"/"broke_down", never flips back to "inside".
      - A break of the opposite side updates to the new break direction.
    Returns (new_status, or_break_atr).
    """
    if ltp is None:
        return prev_status, None

    if ltp > or_high:
        new_status = "broke_up"
    elif ltp < or_low:
        new_status = "broke_down"
    else:
        new_status = prev_status if prev_status in ("broke_up", "broke_down") else "inside"

    or_break_atr = None
    if new_status in ("broke_up", "broke_down") and avg_range_pct and avg_range_pct > 0:
        ref = prev_close or (or_high + or_low) / 2
        atr_price = ref * avg_range_pct
        if atr_price > 0:
            distance = (ltp - or_high) if new_status == "broke_up" else (or_low - ltp)
            or_break_atr = round(max(0.0, distance) / atr_price, 2)

    return new_status, or_break_atr


# ── tracker ───────────────────────────────────────────────────────────────────

class RangeTracker:
    """
    Holds per-range materialised OR values and per-symbol break state for one
    trading day, persisting both to Redis so poller restarts lose nothing.
    """

    def __init__(self, trade_date: str):
        self.trade_date = trade_date
        # label -> {"def": d, "materialized": dict|None, "state": dict}
        self.ranges: dict[str, dict] = {}

    # -- definitions -----------------------------------------------------------

    def set_defs(self, defs: list[dict]) -> None:
        """Merge a fresh def list: add new ranges (loading any persisted Redis
        state), drop deactivated ones, keep in-memory state for the rest."""
        wanted = {d["label"]: d for d in defs}
        for label in list(self.ranges):
            if label not in wanted:
                logger.info(f"orb_ranges: range {label} deactivated — dropping")
                del self.ranges[label]
        for label, d in wanted.items():
            if label in self.ranges:
                self.ranges[label]["def"] = d   # name/id may have changed
            else:
                self.ranges[label] = {
                    "def":          d,
                    "materialized": self._load_or(label),
                    "state":        _cache.hgetall_json(self._state_key(label)),
                }

    def defs(self) -> list[dict]:
        return [r["def"] for r in self.ranges.values()]

    # -- redis keys -------------------------------------------------------------

    def _or_key(self, label: str) -> str:
        return _OR_KEY_FMT.format(date=self.trade_date, label=label)

    def _state_key(self, label: str) -> str:
        return _STATE_KEY_FMT.format(date=self.trade_date, label=label)

    def _load_or(self, label: str) -> dict | None:
        """Load a persisted materialised range. None = not yet frozen."""
        data = _cache.hgetall_json(self._or_key(label))
        if _META_FIELD not in data:
            return None
        data.pop(_META_FIELD)
        return data

    # -- poll history ------------------------------------------------------------

    def append_poll_history(self, rows: list[dict], now_ist: datetime) -> None:
        """RPUSH one compact record per symbol (~80 bytes), TTL 24h on first push."""
        t = now_ist.strftime("%H:%M:%S")
        items = {
            _POLLS_KEY_FMT.format(date=self.trade_date, symbol=row["symbol"]): {
                "t": t,
                "h": row.get("high"),
                "l": row.get("low"),
                "p": row.get("ltp"),
                "v": row.get("volume"),
            }
            for row in rows
        }
        _cache.rpush_json_many(items, _OR_TTL)

    # -- materialisation ----------------------------------------------------------

    def _materialize(self, label: str, rows: list[dict], now_ist: datetime) -> None:
        """ONE-time expensive pass: window h/l per symbol from poll history."""
        rng      = self.ranges[label]
        d        = rng["def"]
        anchored = d["start"] == SESSION_START
        start_s  = d["start"].strftime("%H:%M:%S")
        end_s    = d["end"].strftime("%H:%M:%S")
        frozen_at = now_ist.isoformat()

        materialized: dict[str, dict] = {}
        for row in rows:
            sym     = row["symbol"]
            records = _cache.lrange_json(_POLLS_KEY_FMT.format(date=self.trade_date, symbol=sym))
            window  = [r for r in records if r.get("t") and start_s <= r["t"] <= end_s]
            hi, lo  = compute_window_hl(window, anchored)
            if hi is None or lo is None:
                if anchored and row.get("high") is not None and row.get("low") is not None:
                    # Poller wasn't running during the window — day h/l is the
                    # closest available estimate (pre-refactor behaviour).
                    hi, lo = row["high"], row["low"]
                else:
                    continue
            materialized[sym] = {
                "or_high":   round(hi, 4),
                "or_low":    round(lo, 4),
                "frozen_at": frozen_at,
            }

        rng["materialized"] = materialized
        payload: dict = {_META_FIELD: {"frozen_at": frozen_at}}
        payload.update(materialized)
        _cache.hset_json(self._or_key(label), payload, _OR_TTL)
        logger.info(f"orb_ranges: materialized {label} — {len(materialized)} symbols")

    # -- per-cycle update -----------------------------------------------------------

    def update(self, rows: list[dict], baselines: dict[str, dict], now_ist: datetime) -> None:
        """
        Annotate snapshot rows with per-range status and update break state.
        Call AFTER append_poll_history so the current poll is part of any
        window materialised this cycle.
        """
        now_t = now_ist.time()
        for row in rows:
            row["ranges"] = {}

        for label, rng in self.ranges.items():
            d          = rng["def"]
            is_default = d.get("is_default", False)

            if rng["materialized"] is None:
                if now_t >= d["end"]:
                    self._materialize(label, rows, now_ist)
                else:
                    for row in rows:
                        row["ranges"][label] = {"status": "forming", "break_atr": None}
                        if is_default:
                            row["or_high"]      = None
                            row["or_low"]       = None
                            row["or_status"]    = "forming"
                            row["or_break_atr"] = None
                    continue

            materialized = rng["materialized"]
            state        = rng["state"]
            dirty: dict[str, dict] = {}

            for row in rows:
                sym = row["symbol"]
                m   = materialized.get(sym)
                if m is None:
                    row["ranges"][label] = {"status": None, "break_atr": None}
                    if is_default:
                        row["or_high"]      = None
                        row["or_low"]       = None
                        row["or_status"]    = None
                        row["or_break_atr"] = None
                    continue

                baseline = baselines.get(sym)
                st       = state.get(sym) or {"or_status": "inside"}
                prev     = st.get("or_status", "inside")

                status, break_atr = compute_break_status(
                    prev, row.get("ltp"), m["or_high"], m["or_low"],
                    row.get("prev_close"),
                    baseline.get("avg_range_pct_20d") if baseline else None,
                )

                changed = status != prev or sym not in state
                st["or_status"] = status

                # First break of the day for this range: capture context
                if prev not in ("broke_up", "broke_down") and status in ("broke_up", "broke_down"):
                    st["first_break_side"] = "up" if status == "broke_up" else "down"
                    st["break_time"]       = now_ist.isoformat()
                    st["rvol_at_break"]    = row.get("rvol")
                if status == "broke_up" and not st.get("broke_up"):
                    st["broke_up"] = True
                    changed = True
                if status == "broke_down" and not st.get("broke_down"):
                    st["broke_down"] = True
                    changed = True
                if break_atr is not None and break_atr > (st.get("break_atr_max") or 0.0):
                    st["break_atr_max"] = break_atr
                    changed = True

                state[sym] = st
                if changed:
                    dirty[sym] = st

                row["ranges"][label] = {"status": status, "break_atr": break_atr}
                if is_default:
                    row["or_high"]      = m["or_high"]
                    row["or_low"]       = m["or_low"]
                    row["or_status"]    = status
                    row["or_break_atr"] = break_atr

            if dirty:
                _cache.hset_json(self._state_key(label), dirty, _OR_TTL)
