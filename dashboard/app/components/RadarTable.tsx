"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

// ── types ─────────────────────────────────────────────────────────────────────

type RadarRow = {
  symbol: string;
  ltp: number | null;
  prev_close: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number;
  gap_pct: number | null;
  change_pct: number | null;
  change_from_open_pct: number | null;
  rvol: number | null;
  range_used_pct: number | null;
  atr_multiple: number | null;
  index_membership: string;
};

type RadarSnapshot = {
  generated_at: string;
  rows: RadarRow[];
};

export type RadarResponse = {
  snapshot: RadarSnapshot | null;
  stale: boolean;
  market_open: boolean;
  status: { state?: string };
};

// ── column config ─────────────────────────────────────────────────────────────

type SortKey = keyof RadarRow;

const COLUMNS: { key: SortKey; label: string; align: "left" | "right" }[] = [
  { key: "symbol",               label: "Symbol",     align: "left"  },
  { key: "ltp",                  label: "LTP",        align: "right" },
  { key: "change_pct",           label: "Chg%",       align: "right" },
  { key: "gap_pct",              label: "Gap%",       align: "right" },
  { key: "change_from_open_pct", label: "From Open%", align: "right" },
  { key: "rvol",                 label: "RVOL",       align: "right" },
  { key: "atr_multiple",         label: "ATR×",       align: "right" },
  { key: "volume",               label: "Volume",     align: "right" },
];

// ── filter state ──────────────────────────────────────────────────────────────

type FilterState = {
  gapMin: string; gapMax: string;
  rvolMin: string;
  chgMin: string; chgMax: string;
  atrMin: string;
};

const EMPTY_FILTERS: FilterState = {
  gapMin: "", gapMax: "", rvolMin: "",
  chgMin: "", chgMax: "", atrMin: "",
};

type PresetDef = { label: string; apply: (s: FilterState) => FilterState };
const PRESETS: PresetDef[] = [
  { label: "Gap Up >2%",           apply: s => ({ ...s, gapMin: "2",   gapMax: ""    }) },
  { label: "Gap Down <-2%",        apply: s => ({ ...s, gapMin: "",    gapMax: "-2"  }) },
  { label: "Volume Surge (RVOL>2)", apply: s => ({ ...s, rvolMin: "2"               }) },
  { label: "Range Expansion (ATR>1.5)", apply: s => ({ ...s, atrMin: "1.5"          }) },
];

function filtersFromParams(sp: URLSearchParams): FilterState {
  return {
    gapMin:  sp.get("gapMin")  ?? "",
    gapMax:  sp.get("gapMax")  ?? "",
    rvolMin: sp.get("rvolMin") ?? "",
    chgMin:  sp.get("chgMin")  ?? "",
    chgMax:  sp.get("chgMax")  ?? "",
    atrMin:  sp.get("atrMin")  ?? "",
  };
}

function filtersToQS(f: FilterState): string {
  const p = new URLSearchParams();
  if (f.gapMin)  p.set("gapMin",  f.gapMin);
  if (f.gapMax)  p.set("gapMax",  f.gapMax);
  if (f.rvolMin) p.set("rvolMin", f.rvolMin);
  if (f.chgMin)  p.set("chgMin",  f.chgMin);
  if (f.chgMax)  p.set("chgMax",  f.chgMax);
  if (f.atrMin)  p.set("atrMin",  f.atrMin);
  return p.toString();
}

function num(s: string): number | null {
  const n = parseFloat(s);
  return isNaN(n) ? null : n;
}

// ── filter + sort logic ───────────────────────────────────────────────────────

function applyFilters(rows: RadarRow[], f: FilterState): RadarRow[] {
  const gapMin  = num(f.gapMin);
  const gapMax  = num(f.gapMax);
  const rvolMin = num(f.rvolMin);
  const chgMin  = num(f.chgMin);
  const chgMax  = num(f.chgMax);
  const atrMin  = num(f.atrMin);

  return rows.filter(r => {
    if (gapMin  !== null && (r.gap_pct      === null || r.gap_pct      < gapMin))  return false;
    if (gapMax  !== null && (r.gap_pct      === null || r.gap_pct      > gapMax))  return false;
    if (rvolMin !== null && (r.rvol         === null || r.rvol         < rvolMin)) return false;
    if (chgMin  !== null && (r.change_pct   === null || r.change_pct   < chgMin))  return false;
    if (chgMax  !== null && (r.change_pct   === null || r.change_pct   > chgMax))  return false;
    if (atrMin  !== null && (r.atr_multiple === null || r.atr_multiple < atrMin))  return false;
    return true;
  });
}

function applySort(rows: RadarRow[], key: SortKey, asc: boolean): RadarRow[] {
  return [...rows].sort((a, b) => {
    const va = a[key] as number | string | null;
    const vb = b[key] as number | string | null;
    if (va === null && vb === null) return 0;
    if (va === null) return asc ? 1 : -1;
    if (vb === null) return asc ? -1 : 1;
    if (typeof va === "string") return asc ? va.localeCompare(vb as string) : (vb as string).localeCompare(va);
    return asc ? (va as number) - (vb as number) : (vb as number) - (va as number);
  });
}

// ── formatters ────────────────────────────────────────────────────────────────

function fmt(v: number | null, dp = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(dp);
}

function fmtPct(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : "") + v.toFixed(2) + "%";
}

function fmtVol(v: number): string {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (v >= 1_000)     return (v / 1_000).toFixed(0)     + "K";
  return String(v);
}

function pctColor(v: number | null): string {
  if (v === null) return "var(--muted)";
  return v > 0 ? "var(--bullish)" : v < 0 ? "var(--bearish)" : "var(--muted)";
}

// ── components ────────────────────────────────────────────────────────────────

function FilterInput({
  label, value, onChange,
}: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--muted)" }}>
      {label}
      <input
        type="number"
        step="any"
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{
          width: 58, padding: "3px 6px", background: "var(--bg)",
          border: "1px solid var(--border)", borderRadius: 6,
          color: "var(--text)", fontSize: 12, outline: "none",
        }}
      />
    </label>
  );
}

// ── main export ───────────────────────────────────────────────────────────────

const POLL_MS    = 45_000;
const SORT_DEFAULT: SortKey = "rvol";

export function RadarTable({ initial }: { initial: RadarResponse }) {
  const searchParams = useSearchParams();

  const [data,    setData]    = useState<RadarResponse>(initial);
  const [sortKey, setSortKey] = useState<SortKey>(SORT_DEFAULT);
  const [sortAsc, setSortAsc] = useState(false);   // RVOL default: descending
  const [filters, setFilters] = useState<FilterState>(() => filtersFromParams(searchParams));
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Persist filters to URL
  useEffect(() => {
    const qs  = filtersToQS(filters);
    const url = qs ? `?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, [filters]);

  // 45-second polling, paused when tab is hidden
  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/radar", { cache: "no-store" });
      if (res.ok) setData(await res.json());
    } catch { /* silent — show last data */ }
  }, []);

  useEffect(() => {
    timerRef.current = setInterval(() => {
      if (!document.hidden) fetchData();
    }, POLL_MS);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [fetchData]);

  // Column header click: toggle asc/desc on same column, reset to sensible default on new column
  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortAsc(a => !a);
    } else {
      setSortKey(key);
      setSortAsc(key === "symbol"); // text: default asc; numbers: default desc
    }
  }

  const setF = (patch: Partial<FilterState>) => setFilters(f => ({ ...f, ...patch }));
  const hasFilters = Object.values(filters).some(v => v !== "");

  const allRows  = data.snapshot?.rows ?? [];
  const filtered = applySort(applyFilters(allRows, filters), sortKey, sortAsc);

  const asOf = (() => {
    if (!data.snapshot?.generated_at) return null;
    try {
      return new Date(data.snapshot.generated_at).toLocaleTimeString("en-IN", {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      });
    } catch { return null; }
  })();

  const chip = (text: string, color: string, bg = "transparent") => (
    <span style={{
      fontSize: 11, borderRadius: 4, padding: "2px 7px",
      border: `1px solid ${color}`, color, background: bg,
    }}>{text}</span>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* ── header ── */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ color: "var(--muted)", fontSize: 20, fontWeight: 500, margin: 0 }}>
          Radar — Intraday Screener
        </h1>
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          {asOf && <span style={{ fontSize: 12, color: "var(--muted)" }}>as of {asOf}</span>}
          {data.stale && chip("STALE", "#fff", "var(--bearish)")}
          {!data.market_open && chip("MARKET CLOSED", "var(--muted)")}
          {data.status?.state === "token_expired" && chip("TOKEN EXPIRED", "#fff", "var(--bearish)")}
        </div>
      </div>

      {/* ── filter bar ── */}
      <div style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 10, padding: "12px 16px",
        display: "flex", flexDirection: "column", gap: 10,
      }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <FilterInput label="Gap% ≥"  value={filters.gapMin}  onChange={v => setF({ gapMin:  v })} />
          <FilterInput label="Gap% ≤"  value={filters.gapMax}  onChange={v => setF({ gapMax:  v })} />
          <FilterInput label="RVOL ≥"  value={filters.rvolMin} onChange={v => setF({ rvolMin: v })} />
          <FilterInput label="Chg% ≥"  value={filters.chgMin}  onChange={v => setF({ chgMin:  v })} />
          <FilterInput label="Chg% ≤"  value={filters.chgMax}  onChange={v => setF({ chgMax:  v })} />
          <FilterInput label="ATR× ≥"  value={filters.atrMin}  onChange={v => setF({ atrMin:  v })} />

          {hasFilters && (
            <button
              onClick={() => setFilters(EMPTY_FILTERS)}
              style={{
                padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
                border: "1px solid var(--bearish)", color: "var(--bearish)", background: "transparent",
              }}
            >Clear filters</button>
          )}
        </div>

        {/* Quick preset chips */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {PRESETS.map(p => (
            <button
              key={p.label}
              onClick={() => setFilters(f => p.apply(f))}
              style={{
                padding: "3px 10px", borderRadius: 12, fontSize: 11, cursor: "pointer",
                border: "1px solid var(--accent)", color: "var(--accent)", background: "transparent",
              }}
            >{p.label}</button>
          ))}
        </div>
      </div>

      {/* ── row count ── */}
      <div style={{ color: "var(--muted)", fontSize: 12 }}>
        Showing {filtered.length} of {allRows.length}
      </div>

      {/* ── table ── */}
      {allRows.length === 0 ? (
        <div style={{
          background: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: 10, padding: 40, textAlign: "center", color: "var(--muted)",
        }}>
          {data.snapshot === null
            ? "No snapshot data yet — radar poller is not running or has not polled yet."
            : "No rows match the current filters."}
        </div>
      ) : (
        <div style={{ overflowX: "auto", borderRadius: 10, border: "1px solid var(--border)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 700 }}>
            <thead>
              <tr style={{ background: "var(--surface)", borderBottom: "1px solid var(--border)" }}>
                {COLUMNS.map(col => (
                  <th
                    key={col.key}
                    onClick={() => handleSort(col.key)}
                    style={{
                      padding: "8px 12px", textAlign: col.align, cursor: "pointer",
                      color: sortKey === col.key ? "var(--text)" : "var(--muted)",
                      whiteSpace: "nowrap", userSelect: "none", fontWeight: 500,
                    }}
                  >
                    {col.label}
                    {sortKey === col.key ? (sortAsc ? " ▲" : " ▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((row, i) => {
                const rvolHot = row.rvol !== null && row.rvol > 2;
                return (
                  <tr
                    key={row.symbol}
                    style={{
                      borderBottom: "1px solid var(--border)",
                      background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.018)",
                    }}
                  >
                    {/* Symbol */}
                    <td style={{ padding: "7px 12px", whiteSpace: "nowrap" }}>
                      <span style={{ color: "var(--text)", fontWeight: 600 }}>{row.symbol}</span>
                    </td>
                    {/* LTP */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--text)" }}>
                      {fmt(row.ltp)}
                    </td>
                    {/* Chg% */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: pctColor(row.change_pct) }}>
                      {fmtPct(row.change_pct)}
                    </td>
                    {/* Gap% */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: pctColor(row.gap_pct) }}>
                      {fmtPct(row.gap_pct)}
                    </td>
                    {/* From Open% */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: pctColor(row.change_from_open_pct) }}>
                      {fmtPct(row.change_from_open_pct)}
                    </td>
                    {/* RVOL */}
                    <td style={{
                      padding: "7px 12px", textAlign: "right",
                      color:      rvolHot ? "var(--accent)" : "var(--text)",
                      fontWeight: rvolHot ? 700 : 400,
                    }}>
                      {fmt(row.rvol)}
                    </td>
                    {/* ATR× */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--text)" }}>
                      {fmt(row.atr_multiple)}
                    </td>
                    {/* Volume */}
                    <td style={{ padding: "7px 12px", textAlign: "right", color: "var(--muted)" }}>
                      {fmtVol(row.volume)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
