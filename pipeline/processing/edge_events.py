"""
pipeline/processing/edge_events.py — Phase 2 /edge event table: shared query builder
for the paginated event listing AND the filtered CSV/Parquet export.

One row per radar_event with the four label horizons (5m / 15m / 30m / eod) PIVOTED
into columns, so a row = one observation that exports cleanly for a pandas groupby.

Row model (identical between the table API and the export — they share _build_query
so the schemas can NEVER drift):

  identity / surfaced   event_id, ts, symbol, event_type, direction, rvol, gap_pct,
                        atr_multiple, or_status, entry_price, regime_id
  per horizon           ret_<h>, mae_<h>, mfe_<h>   for h in 5m/15m/30m/eod
  export-only           offset_<h>                  for h in 5m/15m/30m   (NOT eod —
                        the labeler stores offset_seconds=None for eod, it is
                        meaningless there). matched_ts is deliberately excluded — it is
                        redundant given offset_seconds.

NULL vs 0 (load-bearing): every label column is `max(col) FILTER (WHERE horizon=...)`.
  * label row absent (horizon not yet matured)  → aggregate over {} → SQL NULL
  * label row present with NULL metric (a polling gap / EOD-floor miss) → NULL
  * label row present with a real 0.0           → 0.0
So an immature event yields NULL (not 0, not a dropped row) and a genuine 0.0000 stays
distinct from NULL. The API serialises NULL→None→JSON null; the exporter keeps it as a
pandas NaN (distinct from 0.0 in both CSV and Parquet).

DAY-BUCKET EXPRESSION — byte-identical to migration 014's unique index and
event_labeler._IST_DAY_EXPR. The date filter uses the IMMUTABLE interval form
``( <col> AT TIME ZONE INTERVAL '5 hours 30 minutes')::date`` so it buckets identically
AND can use the index. test_edge_events.py diffs this string against the migration and
the labeler rather than assuming equivalence.
"""

import io
from datetime import date

# ── day-bucket expression (see module docstring) ──────────────────────────────────
def _ist_day_expr(col: str) -> str:
    """IMMUTABLE IST (+05:30, no DST) trade-day bucket for `col`. Byte-identical literal
    to migration 014 / event_labeler so it hits the index and buckets the same."""
    return f"({col} AT TIME ZONE INTERVAL '5 hours 30 minutes')::date"

# Canonical bare form for the equality assertion in the test (matches _IST_DAY_EXPR).
IST_DAY_EXPR = _ist_day_expr("ts")

HORIZONS        = ("5m", "15m", "30m", "eod")
MINUTE_HORIZONS = ("5m", "15m", "30m")   # eod has no meaningful offset_seconds

# ── surfaced columns (identity + first-class trigger extractions) ─────────────────
# Extracted in the SELECT (not generated columns) — zero schema change, fine at this
# scale. entry_price is trigger->>'price' (the LTP at the crossing = labeler entry).
_SURFACED_SELECT = """
    e.event_id::text                       AS event_id,
    e.ts                                   AS ts,
    e.symbol                               AS symbol,
    e.event_type                           AS event_type,
    e.range_label                          AS range_label,
    e.trigger->>'direction'                AS direction,
    (e.trigger->>'rvol')::float8           AS rvol,
    (e.trigger->>'gap_pct')::float8        AS gap_pct,
    (e.trigger->>'atr_multiple')::float8   AS atr_multiple,
    e.trigger->>'or_status'                AS or_status,
    (e.trigger->>'price')::float8          AS entry_price,
    e.regime_id                            AS regime_id"""

# Ordered identity/surfaced column names (drives export column order + JSON shape).
SURFACED_COLUMNS = [
    "event_id", "ts", "symbol", "event_type", "range_label", "direction", "rvol",
    "gap_pct", "atr_multiple", "or_status", "entry_price", "regime_id",
]


def _label_select(include_offsets: bool) -> str:
    """Pivot fragment: ret/mae/mfe per horizon (+ offset_seconds per minute horizon
    when include_offsets, i.e. the export path)."""
    parts: list[str] = []
    for h in HORIZONS:
        parts.append(f"max(l.forward_return) FILTER (WHERE l.horizon = '{h}') AS ret_{h}")
        parts.append(f"max(l.mae)            FILTER (WHERE l.horizon = '{h}') AS mae_{h}")
        parts.append(f"max(l.mfe)            FILTER (WHERE l.horizon = '{h}') AS mfe_{h}")
    if include_offsets:
        for h in MINUTE_HORIZONS:
            parts.append(
                f"max(l.offset_seconds) FILTER (WHERE l.horizon = '{h}') AS offset_{h}"
            )
    return ",\n    ".join(parts)


def label_columns(include_offsets: bool) -> list[str]:
    """Ordered label column names matching _label_select."""
    cols = [f"{m}_{h}" for h in HORIZONS for m in ("ret", "mae", "mfe")]
    if include_offsets:
        cols += [f"offset_{h}" for h in MINUTE_HORIZONS]
    return cols


def export_columns() -> list[str]:
    """Full ordered export schema = surfaced + per-horizon labels + minute offsets."""
    return SURFACED_COLUMNS + label_columns(include_offsets=True)


# ── sortable columns (whitelist — never interpolate caller-supplied names) ─────────
# Maps an API sort key → the SQL expression it sorts on.
_SORT_EXPR: dict[str, str] = {
    "ts":           "e.ts",
    "symbol":       "e.symbol",
    "event_type":   "e.event_type",
    "range_label":  "e.range_label",
    "direction":    "e.trigger->>'direction'",
    "rvol":         "(e.trigger->>'rvol')::float8",
    "gap_pct":      "(e.trigger->>'gap_pct')::float8",
    "atr_multiple": "(e.trigger->>'atr_multiple')::float8",
    "entry_price":  "(e.trigger->>'price')::float8",
    **{f"ret_{h}": f"max(l.forward_return) FILTER (WHERE l.horizon = '{h}')" for h in HORIZONS},
    **{f"mae_{h}": f"max(l.mae)            FILTER (WHERE l.horizon = '{h}')" for h in HORIZONS},
    **{f"mfe_{h}": f"max(l.mfe)            FILTER (WHERE l.horizon = '{h}')" for h in HORIZONS},
}
SORT_KEYS = tuple(_SORT_EXPR.keys())
DEFAULT_SORT = "ts"


# ── filters ───────────────────────────────────────────────────────────────────────
class EdgeFilters:
    """Validated, normalised filter set. All fields optional; absent → no constraint."""

    __slots__ = ("date_from", "date_to", "symbol", "event_type", "range_label",
                 "direction", "rvol_min", "gap_min", "gap_max")

    def __init__(self, *, date_from=None, date_to=None, symbol=None, event_type=None,
                 range_label=None, direction=None, rvol_min=None, gap_min=None, gap_max=None):
        self.date_from   = self._parse_date(date_from, "date_from")
        self.date_to     = self._parse_date(date_to, "date_to")
        self.symbol      = symbol.upper().strip() if symbol else None
        self.event_type  = event_type.strip() if event_type else None
        # Canonical ORB slice key: the OR window string "HH:MM-HH:MM" (not a range name).
        self.range_label = range_label.strip() if range_label else None
        self.direction   = direction.strip() if direction else None
        self.rvol_min    = rvol_min
        self.gap_min     = gap_min
        self.gap_max     = gap_max

    @staticmethod
    def _parse_date(value, field):
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {field}: {value!r} (expected YYYY-MM-DD)") from exc

    def where(self) -> tuple[str, list]:
        """Build the WHERE clause (conditions on radar_events `e` only — so the same
        clause works for the paginated query, the export, AND the count) + params."""
        conds: list[str] = []
        params: list = []
        if self.date_from is not None:
            conds.append(f"{_ist_day_expr('e.ts')} >= %s")
            params.append(self.date_from)
        if self.date_to is not None:
            conds.append(f"{_ist_day_expr('e.ts')} <= %s")
            params.append(self.date_to)
        if self.symbol:
            conds.append("e.symbol = %s")
            params.append(self.symbol)
        if self.event_type:
            conds.append("e.event_type = %s")
            params.append(self.event_type)
        if self.range_label:
            conds.append("e.range_label = %s")
            params.append(self.range_label)
        if self.direction:
            conds.append("e.trigger->>'direction' = %s")
            params.append(self.direction)
        if self.rvol_min is not None:
            conds.append("(e.trigger->>'rvol')::float8 >= %s")
            params.append(self.rvol_min)
        if self.gap_min is not None:
            conds.append("(e.trigger->>'gap_pct')::float8 >= %s")
            params.append(self.gap_min)
        if self.gap_max is not None:
            conds.append("(e.trigger->>'gap_pct')::float8 <= %s")
            params.append(self.gap_max)
        clause = (" WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params


# ── query assembly ────────────────────────────────────────────────────────────────
def _build_query(filters: EdgeFilters, *, sort: str, ascending: bool,
                 include_offsets: bool, limit=None, offset=None) -> tuple[str, list]:
    """Assemble the pivot SELECT shared by the listing and the export. limit/offset are
    omitted for the export (full filtered set)."""
    if sort not in _SORT_EXPR:
        raise ValueError(f"Unknown sort key {sort!r}")
    where, params = filters.where()
    direction_sql = "ASC" if ascending else "DESC"
    # NULLS LAST so immature/NULL metrics sink regardless of direction.
    order = f"ORDER BY {_SORT_EXPR[sort]} {direction_sql} NULLS LAST, e.event_id"
    sql = f"""
SELECT {_SURFACED_SELECT},
    {_label_select(include_offsets)}
FROM radar_events e
LEFT JOIN event_labels l ON l.event_id = e.event_id
{where}
GROUP BY e.event_id, e.ts, e.symbol, e.event_type, e.range_label, e.trigger, e.regime_id
{order}
"""
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)
    return sql, params


def count_events(conn, filters: EdgeFilters) -> int:
    """Total events matching the filters (for pagination). Counts radar_events alone —
    every filter is on `e`, so the label join is unnecessary here."""
    where, params = filters.where()
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM radar_events e{where}", params)
        return cur.fetchone()[0]


def query_events(conn, filters: EdgeFilters, *, sort=DEFAULT_SORT, ascending=False,
                 limit=50, offset=0) -> tuple[list[dict], int]:
    """Return (rows, total). rows are dicts in SURFACED + label (no offsets) order."""
    import psycopg2.extras
    sql, params = _build_query(
        filters, sort=sort, ascending=ascending,
        include_offsets=False, limit=limit, offset=offset,
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    return rows, count_events(conn, filters)


def export_dataframe(conn, filters: EdgeFilters, *, sort=DEFAULT_SORT, ascending=False):
    """Build a pandas DataFrame of the FULL filtered set (no limit/offset), columns in
    export_columns() order, with minute-horizon offset_seconds included. NULLs stay NaN
    (distinct from 0.0) so loose matches can be filtered downstream."""
    import pandas as pd
    sql, params = _build_query(
        filters, sort=sort, ascending=ascending,
        include_offsets=True, limit=None, offset=None,
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=col_names)
    return df.reindex(columns=export_columns())


def export_bytes(conn, filters: EdgeFilters, fmt: str, *, sort=DEFAULT_SORT,
                 ascending=False) -> bytes:
    """Serialise the filtered DataFrame to CSV or Parquet bytes."""
    df = export_dataframe(conn, filters, sort=sort, ascending=ascending)
    buf = io.BytesIO()
    if fmt == "parquet":
        df.to_parquet(buf, index=False)
    elif fmt == "csv":
        df.to_csv(buf, index=False)
    else:
        raise ValueError(f"Unknown export format {fmt!r} (expected 'csv' or 'parquet')")
    return buf.getvalue()
