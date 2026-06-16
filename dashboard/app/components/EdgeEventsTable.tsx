"use client";

// Phase 2 /edge event table — server-side filtered / sorted / paginated view of
// radar_events ⋈ event_labels, one row per event with horizons pivoted to columns.
// All filtering/sorting/paging happens on the API (/api/edge/events); this component
// only holds the control state and renders the returned page. Export hits
// /api/edge/events/export with the same filters → the FULL filtered set, not this page.

import { useCallback, useEffect, useRef, useState } from "react";

// ── types ─────────────────────────────────────────────────────────────────────

type EdgeRow = {
  event_id: string;
  ts: string;
  symbol: string;
  event_type: string;
  range_label: string | null;   // OR window that broke ("HH:MM-HH:MM"); null for non-OR events
  direction: string | null;
  rvol: number | null;
  gap_pct: number | null;
  atr_multiple: number | null;
  or_status: string | null;
  entry_price: number | null;
  regime_id: string | null;
  // label horizons (NULL = not yet matured OR a polling gap; distinct from a real 0)
  ret_5m: number | null;  mae_5m: number | null;  mfe_5m: number | null;
  ret_15m: number | null; mae_15m: number | null; mfe_15m: number | null;
  ret_30m: number | null; mae_30m: number | null; mfe_30m: number | null;
  ret_eod: number | null; mae_eod: number | null; mfe_eod: number | null;
};

type EdgeResponse = { rows: EdgeRow[]; total: number; limit: number; offset: number };

// ── filter + sort state ─────────────────────────────────────────────────────────

type Filters = {
  dateFrom: string; dateTo: string;
  symbol: string; eventType: string; rangeLabel: string; direction: string;
  rvolMin: string; gapMin: string; gapMax: string;
};

const EMPTY_FILTERS: Filters = {
  dateFrom: "", dateTo: "", symbol: "", eventType: "", rangeLabel: "", direction: "",
  rvolMin: "", gapMin: "", gapMax: "",
};

const PAGE_SIZE = 50;

// Event types come from the shared radar alert/event rule set.
const EVENT_TYPES = ["orb_break_volume", "gap_momentum", "range_expansion"];

type SortKey =
  | "ts" | "symbol" | "event_type" | "range_label" | "direction" | "rvol" | "gap_pct"
  | "atr_multiple" | "entry_price"
  | "ret_5m" | "ret_15m" | "ret_30m" | "ret_eod"
  | "mae_5m" | "mae_15m" | "mae_30m" | "mae_eod"
  | "mfe_5m" | "mfe_15m" | "mfe_30m" | "mfe_eod";

// Columns: key drives both the header label and (when sortable) the API sort param.
const COLUMNS: { key: SortKey; label: string; sortable: boolean }[] = [
  { key: "ts",           label: "Time",    sortable: true },
  { key: "symbol",       label: "Symbol",  sortable: true },
  { key: "event_type",   label: "Event",   sortable: true },
  { key: "range_label",  label: "OR Win",  sortable: true },
  { key: "direction",    label: "Dir",     sortable: true },
  { key: "rvol",         label: "RVOL",    sortable: true },
  { key: "gap_pct",      label: "Gap%",    sortable: true },
  { key: "atr_multiple", label: "ATR×",    sortable: true },
  { key: "entry_price",  label: "Entry",   sortable: true },
  { key: "ret_5m",       label: "5m",      sortable: true },
  { key: "ret_15m",      label: "15m",     sortable: true },
  { key: "ret_30m",      label: "30m",     sortable: true },
  { key: "ret_eod",      label: "EOD",     sortable: true },
];

// ── formatters ────────────────────────────────────────────────────────────────

// NULL renders as a muted em-dash; a real 0 renders as "0.00" in normal text — the
// two are visually distinct (acceptance: NULL vs 0 distinctness).
function NullCell() {
  return <span style={{ color: "var(--muted)", opacity: 0.5 }} title="no value (NULL)">—</span>;
}

function fmtNum(v: number | null, dp = 2): React.ReactNode {
  if (v === null || v === undefined) return <NullCell />;
  return v.toFixed(dp);
}

function RetCell({ v }: { v: number | null }) {
  if (v === null || v === undefined) return <NullCell />;
  const color = v > 0 ? "var(--bullish)" : v < 0 ? "var(--bearish)" : "var(--text)";
  // A real 0 shows as a plain "0.00" in default text colour — never an em-dash.
  return <span style={{ color }}>{(v > 0 ? "+" : "") + v.toFixed(2)}</span>;
}

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch { return iso; }
}

function DirBadge({ d }: { d: string | null }) {
  if (d === null) return <span style={{ color: "var(--muted)", opacity: 0.5 }}>unsigned</span>;
  const up = d === "up";
  return (
    <span style={{ color: up ? "var(--bullish)" : "var(--bearish)", fontWeight: 600 }}>
      {up ? "▲ up" : "▼ down"}
    </span>
  );
}

// ── query-string builder (shared by listing fetch + export links) ─────────────────

function filtersToParams(f: Filters): URLSearchParams {
  const p = new URLSearchParams();
  if (f.dateFrom)  p.set("date_from", f.dateFrom);
  if (f.dateTo)    p.set("date_to", f.dateTo);
  if (f.symbol)     p.set("symbol", f.symbol.trim().toUpperCase());
  if (f.eventType)  p.set("event_type", f.eventType);
  if (f.rangeLabel) p.set("range_label", f.rangeLabel.trim());
  if (f.direction)  p.set("direction", f.direction);
  if (f.rvolMin)   p.set("rvol_min", f.rvolMin);
  if (f.gapMin)    p.set("gap_min", f.gapMin);
  if (f.gapMax)    p.set("gap_max", f.gapMax);
  return p;
}

// ── component ─────────────────────────────────────────────────────────────────

export function EdgeEventsTable() {
  const [filters, setFilters]   = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied]   = useState<Filters>(EMPTY_FILTERS); // committed filters
  const [sort, setSort]         = useState<SortKey>("ts");
  const [asc, setAsc]           = useState(false);
  const [page, setPage]         = useState(0);

  const [data, setData]     = useState<EdgeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]   = useState<string | null>(null);
  const reqRef = useRef(0);

  const fetchPage = useCallback(async () => {
    const seq = ++reqRef.current;
    setLoading(true);
    setError(null);
    const p = filtersToParams(applied);
    p.set("sort", sort);
    p.set("order", asc ? "asc" : "desc");
    p.set("limit", String(PAGE_SIZE));
    p.set("offset", String(page * PAGE_SIZE));
    try {
      const res = await fetch(`/api/edge/events?${p.toString()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body: EdgeResponse = await res.json();
      if (seq === reqRef.current) setData(body);
    } catch (e) {
      if (seq === reqRef.current) setError(e instanceof Error ? e.message : "Fetch failed");
    } finally {
      if (seq === reqRef.current) setLoading(false);
    }
  }, [applied, sort, asc, page]);

  // Re-fetch whenever the committed filters / sort / page change. fetchPage sets
  // loading state synchronously to show the spinner immediately — a deliberate
  // external-data-sync effect, not derived state.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { fetchPage(); }, [fetchPage]);

  function applyFilters() { setApplied(filters); setPage(0); }
  function clearFilters() { setFilters(EMPTY_FILTERS); setApplied(EMPTY_FILTERS); setPage(0); }

  function handleSort(key: SortKey) {
    if (key === sort) { setAsc(a => !a); }
    else { setSort(key); setAsc(key === "symbol" || key === "event_type"); }
    setPage(0);
  }

  const setF = (patch: Partial<Filters>) => setFilters(f => ({ ...f, ...patch }));

  const total      = data?.total ?? 0;
  const rows       = data?.rows ?? [];
  const pageCount  = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const exportQS   = (() => {
    const p = filtersToParams(applied);
    p.set("sort", sort);
    p.set("order", asc ? "asc" : "desc");
    return p.toString();
  })();
  const rangeStart = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const rangeEnd   = Math.min(total, (page + 1) * PAGE_SIZE);

  const inputStyle: React.CSSProperties = {
    padding: "4px 7px", background: "var(--bg)", border: "1px solid var(--border)",
    borderRadius: 6, color: "var(--text)", fontSize: 12, outline: "none",
  };
  const btn = (active = false): React.CSSProperties => ({
    padding: "5px 12px", borderRadius: 6, fontSize: 12, cursor: "pointer",
    border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
    color: active ? "var(--accent)" : "var(--muted)", background: "transparent",
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* ── header ── */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ color: "var(--muted)", fontSize: 20, fontWeight: 500, margin: 0 }}>
          Edge — Event Table
        </h1>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          one row per event · horizons 5m / 15m / 30m / EOD
        </span>
      </div>

      {/* ── filter bar ── */}
      <div style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 10, padding: "12px 16px",
        display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end",
      }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          From (IST day)
          <input type="date" value={filters.dateFrom} onChange={e => setF({ dateFrom: e.target.value })} style={inputStyle} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          To (IST day)
          <input type="date" value={filters.dateTo} onChange={e => setF({ dateTo: e.target.value })} style={inputStyle} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          Symbol
          <input type="text" placeholder="e.g. TATASTEEL" value={filters.symbol}
            onChange={e => setF({ symbol: e.target.value.toUpperCase() })}
            style={{ ...inputStyle, width: 120, fontFamily: "monospace" }} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          Event type
          <select value={filters.eventType} onChange={e => setF({ eventType: e.target.value })} style={inputStyle}>
            <option value="">All</option>
            {EVENT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          OR window
          <input type="text" placeholder="e.g. 09:45-10:30" value={filters.rangeLabel}
            onChange={e => setF({ rangeLabel: e.target.value })}
            title="Canonical ORB slice key — the time window, not a range name"
            style={{ ...inputStyle, width: 110, fontFamily: "monospace" }} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          Direction
          <select value={filters.direction} onChange={e => setF({ direction: e.target.value })} style={inputStyle}>
            <option value="">All</option>
            <option value="up">up</option>
            <option value="down">down</option>
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          RVOL ≥
          <input type="number" step="any" value={filters.rvolMin}
            onChange={e => setF({ rvolMin: e.target.value })} style={{ ...inputStyle, width: 70 }} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          Gap% ≥
          <input type="number" step="any" value={filters.gapMin}
            onChange={e => setF({ gapMin: e.target.value })} style={{ ...inputStyle, width: 70 }} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "var(--muted)" }}>
          Gap% ≤
          <input type="number" step="any" value={filters.gapMax}
            onChange={e => setF({ gapMax: e.target.value })} style={{ ...inputStyle, width: 70 }} />
        </label>

        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={applyFilters} style={btn(true)}>Apply</button>
          <button onClick={clearFilters} style={btn()}>Clear</button>
        </div>
      </div>

      {/* ── export + count + paging ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div style={{ fontSize: 12, color: "var(--muted)" }}>
          {loading ? "Loading…" : error ? <span style={{ color: "var(--bearish)" }}>Error: {error}</span>
            : <>Showing <strong style={{ color: "var(--text)" }}>{rangeStart}–{rangeEnd}</strong> of {total}</>}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* Export hits the same filters → the WHOLE filtered set, not this page. */}
          <a href={`/api/edge/events/export?fmt=csv&${exportQS}`} style={{ ...btn(), textDecoration: "none" }}>⬇ CSV</a>
          <a href={`/api/edge/events/export?fmt=parquet&${exportQS}`} style={{ ...btn(), textDecoration: "none" }}>⬇ Parquet</a>
          <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0} style={{ ...btn(), opacity: page === 0 ? 0.4 : 1 }}>‹ Prev</button>
          <span style={{ fontSize: 12, color: "var(--muted)" }}>Page {page + 1} / {pageCount}</span>
          <button onClick={() => setPage(p => (p + 1 < pageCount ? p + 1 : p))} disabled={page + 1 >= pageCount} style={{ ...btn(), opacity: page + 1 >= pageCount ? 0.4 : 1 }}>Next ›</button>
        </div>
      </div>

      {/* ── table ── */}
      {rows.length === 0 && !loading ? (
        <div style={{
          background: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: 10, padding: 40, textAlign: "center", color: "var(--muted)",
        }}>
          No events match the current filters.
        </div>
      ) : (
        <div style={{ overflowX: "auto", borderRadius: 10, border: "1px solid var(--border)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 900 }}>
            <thead>
              <tr style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)" }}>
                {COLUMNS.map(col => (
                  <th
                    key={col.key}
                    onClick={() => col.sortable && handleSort(col.key)}
                    style={{
                      padding: "8px 12px", textAlign: col.key === "symbol" || col.key === "event_type" || col.key === "range_label" || col.key === "direction" ? "left" : "right",
                      cursor: col.sortable ? "pointer" : "default",
                      color: sort === col.key ? "var(--text)" : "var(--muted)",
                      whiteSpace: "nowrap", userSelect: "none", fontWeight: 500,
                    }}
                  >
                    {col.label}{sort === col.key ? (asc ? " ▲" : " ▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={row.event_id} style={{
                  borderBottom: "1px solid var(--border)",
                  background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.018)",
                }}>
                  <td style={{ padding: "7px 12px", whiteSpace: "nowrap", color: "var(--muted)" }}>{fmtTime(row.ts)}</td>
                  <td style={{ padding: "7px 12px", color: "var(--text)", fontWeight: 600 }}>{row.symbol}</td>
                  <td style={{ padding: "7px 12px", color: "var(--muted)", fontSize: 12 }}>{row.event_type}</td>
                  <td style={{ padding: "7px 12px", fontFamily: "monospace", fontSize: 12, color: row.range_label ? "var(--text)" : "var(--muted)" }}>
                    {row.range_label ?? <span style={{ opacity: 0.5 }}>—</span>}
                  </td>
                  <td style={{ padding: "7px 12px" }}><DirBadge d={row.direction} /></td>
                  <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--text)" }}>{fmtNum(row.rvol)}</td>
                  <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--text)" }}>{fmtNum(row.gap_pct)}</td>
                  <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--text)" }}>{fmtNum(row.atr_multiple)}</td>
                  <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--muted)" }}>{fmtNum(row.entry_price)}</td>
                  <td style={{ padding: "7px 12px", textAlign: "right" }}><RetCell v={row.ret_5m} /></td>
                  <td style={{ padding: "7px 12px", textAlign: "right" }}><RetCell v={row.ret_15m} /></td>
                  <td style={{ padding: "7px 12px", textAlign: "right" }}><RetCell v={row.ret_30m} /></td>
                  <td style={{ padding: "7px 12px", textAlign: "right" }}><RetCell v={row.ret_eod} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
