"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

// ── types ─────────────────────────────────────────────────────────────────────

type ORStatus = "forming" | "inside" | "broke_up" | "broke_down" | null;

type RangeCell = { status: ORStatus; break_atr: number | null };

export type RangeDef = {
  id: number;
  name: string;
  or_start: string;   // "HH:MM"
  or_end: string;     // "HH:MM"
  scope: "session" | "standard";
  session_date: string | null;
  label: string;      // "HH:MM-HH:MM" — key into row.ranges
  is_default: boolean;
};

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
  // B: Opening Range
  or_high: number | null;
  or_low: number | null;
  or_status: ORStatus;
  or_break_atr: number | null;
  // Custom OR ranges: label -> {status, break_atr}
  ranges?: Record<string, RangeCell>;
  // D: News / catalyst
  has_news: boolean;
  catalyst_line: string | null;
  headline_count: number;
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
  ranges?: RangeDef[];
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
  { key: "or_status",            label: "OR Status",  align: "left"  },
  { key: "volume",               label: "Volume",     align: "right" },
];

// ── filter state ──────────────────────────────────────────────────────────────

type FilterState = {
  gapMin: string; gapMax: string;
  rvolMin: string;
  chgMin: string; chgMax: string;
  atrMin: string;
  orStatus: string;   // "" | "forming" | "inside" | "broke_up" | "broke_down"
  newsOnly: string;   // "" | "1"
};

const EMPTY_FILTERS: FilterState = {
  gapMin: "", gapMax: "", rvolMin: "",
  chgMin: "", chgMax: "", atrMin: "",
  orStatus: "", newsOnly: "",
};

type PresetDef = { label: string; apply: (s: FilterState) => FilterState };
const PRESETS: PresetDef[] = [
  { label: "Gap Up >2%",            apply: s => ({ ...s, gapMin: "2",   gapMax: ""    }) },
  { label: "Gap Down <-2%",         apply: s => ({ ...s, gapMin: "",    gapMax: "-2"  }) },
  { label: "Volume Surge (RVOL>2)", apply: s => ({ ...s, rvolMin: "2"               }) },
  { label: "Range Expansion (ATR>1.5)", apply: s => ({ ...s, atrMin: "1.5"          }) },
  { label: "ORB Break Up",          apply: s => ({ ...s, orStatus: "broke_up"       }) },
  { label: "ORB Break Down",        apply: s => ({ ...s, orStatus: "broke_down"     }) },
];

function filtersFromParams(sp: URLSearchParams): FilterState {
  return {
    gapMin:   sp.get("gapMin")   ?? "",
    gapMax:   sp.get("gapMax")   ?? "",
    rvolMin:  sp.get("rvolMin")  ?? "",
    chgMin:   sp.get("chgMin")   ?? "",
    chgMax:   sp.get("chgMax")   ?? "",
    atrMin:   sp.get("atrMin")   ?? "",
    orStatus: sp.get("orStatus") ?? "",
    newsOnly: sp.get("newsOnly") ?? "",
  };
}

function filtersToQS(f: FilterState): string {
  const p = new URLSearchParams();
  if (f.gapMin)   p.set("gapMin",   f.gapMin);
  if (f.gapMax)   p.set("gapMax",   f.gapMax);
  if (f.rvolMin)  p.set("rvolMin",  f.rvolMin);
  if (f.chgMin)   p.set("chgMin",   f.chgMin);
  if (f.chgMax)   p.set("chgMax",   f.chgMax);
  if (f.atrMin)   p.set("atrMin",   f.atrMin);
  if (f.orStatus) p.set("orStatus", f.orStatus);
  if (f.newsOnly) p.set("newsOnly", f.newsOnly);
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
    if (f.orStatus && r.or_status !== f.orStatus)                                  return false;
    if (f.newsOnly === "1" && !r.has_news)                                         return false;
    return true;
  });
}

function applySort(rows: RadarRow[], key: SortKey, asc: boolean): RadarRow[] {
  return [...rows].sort((a, b) => {
    const va = a[key] as number | string | null | boolean;
    const vb = b[key] as number | string | null | boolean;
    if (va === null && vb === null) return 0;
    if (va === null) return asc ? 1 : -1;
    if (vb === null) return asc ? -1 : 1;
    if (typeof va === "string") return asc ? va.localeCompare(vb as string) : (vb as string).localeCompare(va);
    if (typeof va === "boolean") return asc ? (va ? 1 : -1) : (va ? -1 : 1);
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

// ── OR Status badge ───────────────────────────────────────────────────────────

const OR_BADGE: Record<string, { label: string; color: string; bg: string }> = {
  forming:    { label: "Forming",   color: "var(--muted)",    bg: "transparent" },
  inside:     { label: "Inside",    color: "var(--text)",     bg: "transparent" },
  broke_up:   { label: "Broke Up",  color: "var(--bullish)",  bg: "rgba(0,200,80,0.08)" },
  broke_down: { label: "Broke Dn",  color: "var(--bearish)",  bg: "rgba(220,50,50,0.08)" },
};

function ORBadge({ row }: { row: RadarRow }) {
  if (!row.or_status) return <span style={{ color: "var(--muted)" }}>—</span>;
  const b = OR_BADGE[row.or_status] ?? OR_BADGE.inside;
  const tooltip = row.or_break_atr !== null
    ? `${row.or_break_atr.toFixed(2)} ATR beyond OR`
    : row.or_high !== null && row.or_low !== null
      ? `OR ${row.or_low.toFixed(2)}–${row.or_high.toFixed(2)}`
      : undefined;
  return (
    <span
      title={tooltip}
      style={{
        fontSize: 11, borderRadius: 4, padding: "2px 7px",
        border: `1px solid ${b.color}`, color: b.color, background: b.bg,
        cursor: tooltip ? "help" : "default", whiteSpace: "nowrap",
      }}
    >
      {b.label}
      {row.or_break_atr !== null && (
        <span style={{ marginLeft: 4, opacity: 0.75 }}>{row.or_break_atr.toFixed(1)}×</span>
      )}
    </span>
  );
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
  const [selRange, setSelRange] = useState<string>(() => searchParams.get("orRange") ?? "");
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Add-range form state
  const [showAdd,  setShowAdd]  = useState(false);
  const [addName,  setAddName]  = useState("");
  const [addStart, setAddStart] = useState("10:00");
  const [addEnd,   setAddEnd]   = useState("10:45");
  const [addScope, setAddScope] = useState<"session" | "standard">("session");
  const [addErr,   setAddErr]   = useState<string | null>(null);
  const [addBusy,  setAddBusy]  = useState(false);

  // Persist filters + selected range to URL
  useEffect(() => {
    const p = new URLSearchParams(filtersToQS(filters));
    if (selRange) p.set("orRange", selRange);
    const qs  = p.toString();
    const url = qs ? `?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, [filters, selRange]);

  // Range defs are managed independently of the snapshot so the range controls
  // work when the market is closed / no snapshot exists (e.g. defining ranges
  // before open). Seeded from the initial response, refreshed from
  // /api/radar/ranges (DB-backed, Redis-independent) and from each poll.
  const [rangeDefs, setRangeDefs] = useState<RangeDef[]>(initial.ranges ?? []);

  const fetchRanges = useCallback(async () => {
    try {
      const res = await fetch("/api/radar/ranges", { cache: "no-store" });
      if (res.ok) {
        const body = await res.json();
        if (Array.isArray(body)) setRangeDefs(body);
      }
    } catch { /* keep last list */ }
  }, []);

  useEffect(() => { fetchRanges(); }, [fetchRanges]);

  // 45-second polling, paused when tab is hidden
  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/radar", { cache: "no-store" });
      if (res.ok) {
        const body: RadarResponse = await res.json();
        setData(body);
        if (Array.isArray(body.ranges) && body.ranges.length > 0) setRangeDefs(body.ranges);
      }
    } catch { /* silent — show last data */ }
  }, []);

  useEffect(() => {
    timerRef.current = setInterval(() => {
      if (!document.hidden) fetchData();
    }, POLL_MS);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [fetchData]);

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortAsc(a => !a);
    } else {
      setSortKey(key);
      setSortAsc(key === "symbol" || key === "or_status");
    }
  }

  const setF = (patch: Partial<FilterState>) => setFilters(f => ({ ...f, ...patch }));
  const hasFilters = Object.values(filters).some(v => v !== "");

  // ── OR range selection ──
  // The OR column (and orStatus filter) reflects the selected range. The default
  // range uses the row-level or_* fields; custom ranges read row.ranges[label].
  const defaultLabel = rangeDefs.find(r => r.is_default)?.label ?? "";
  const activeLabel  = selRange || defaultLabel;
  const activeDef    = rangeDefs.find(r => r.label === activeLabel);
  const useCustom    = activeLabel !== "" && activeLabel !== defaultLabel;

  const rawRows = data.snapshot?.rows ?? [];
  const allRows = useCustom
    ? rawRows.map(r => {
        const cell = r.ranges?.[activeLabel];
        return {
          ...r,
          or_status:    cell?.status ?? null,
          or_break_atr: cell?.break_atr ?? null,
          or_high:      null,
          or_low:       null,
        };
      })
    : rawRows;
  const filtered = applySort(applyFilters(allRows, filters), sortKey, sortAsc);

  async function refreshRanges() {
    await fetchRanges();
  }

  async function submitAddRange() {
    setAddErr(null);
    if (!addName.trim()) { setAddErr("Name is required"); return; }
    setAddBusy(true);
    try {
      const res = await fetch("/api/radar/ranges", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: addName.trim(), or_start: addStart, or_end: addEnd, scope: addScope,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setAddErr(typeof body.detail === "string" ? body.detail : `Failed (HTTP ${res.status})`);
        return;
      }
      setShowAdd(false);
      setAddName("");
      setSelRange(`${addStart}-${addEnd}`);
      await refreshRanges();
    } catch {
      setAddErr("Network error");
    } finally {
      setAddBusy(false);
    }
  }

  async function deleteRange(def: RangeDef) {
    if (def.is_default) return;
    if (!window.confirm(`Delete range "${def.name}" (${def.label})?`)) return;
    try {
      const res = await fetch(`/api/radar/ranges/${def.id}`, { method: "DELETE" });
      if (res.ok) {
        if (selRange === def.label) setSelRange("");
        await refreshRanges();
      }
    } catch { /* leave list as-is */ }
  }

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

          {/* OR status dropdown */}
          <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--muted)" }}>
            OR Status
            <select
              value={filters.orStatus}
              onChange={e => setF({ orStatus: e.target.value })}
              style={{
                padding: "3px 6px", background: "var(--bg)",
                border: "1px solid var(--border)", borderRadius: 6,
                color: "var(--text)", fontSize: 12, outline: "none",
              }}
            >
              <option value="">All</option>
              <option value="broke_up">Broke Up</option>
              <option value="broke_down">Broke Down</option>
              <option value="inside">Inside</option>
              <option value="forming">Forming</option>
            </select>
          </label>

          {/* News only toggle */}
          <button
            onClick={() => setF({ newsOnly: filters.newsOnly === "1" ? "" : "1" })}
            style={{
              padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
              border: `1px solid ${filters.newsOnly === "1" ? "var(--accent)" : "var(--border)"}`,
              color: filters.newsOnly === "1" ? "var(--accent)" : "var(--muted)",
              background: filters.newsOnly === "1" ? "rgba(var(--accent-rgb,100,180,255),0.08)" : "transparent",
            }}
          >
            📰 News only
          </button>

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

      {/* ── OR range selector — always rendered so ranges can be managed
             before market open / without snapshot data ── */}
      <div style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 10, padding: "10px 16px",
        display: "flex", flexDirection: "column", gap: 10,
      }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--muted)" }}>
              OR Range
              <select
                value={activeLabel}
                onChange={e => setSelRange(e.target.value === defaultLabel ? "" : e.target.value)}
                style={{
                  padding: "3px 6px", background: "var(--bg)",
                  border: "1px solid var(--border)", borderRadius: 6,
                  color: "var(--text)", fontSize: 12, outline: "none",
                }}
              >
                {rangeDefs.length === 0 && (
                  <option value="">No ranges loaded</option>
                )}
                {rangeDefs.map(r => (
                  <option key={r.id} value={r.label}>
                    {r.label} — {r.name}{r.scope === "session" ? " (today)" : ""}
                  </option>
                ))}
              </select>
            </label>

            {activeDef && !activeDef.is_default && (
              <button
                onClick={() => deleteRange(activeDef)}
                title={`Delete range ${activeDef.label}`}
                style={{
                  padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
                  border: "1px solid var(--bearish)", color: "var(--bearish)", background: "transparent",
                }}
              >✕ Delete</button>
            )}

            <button
              onClick={() => { setShowAdd(s => !s); setAddErr(null); }}
              style={{
                padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
                border: "1px solid var(--accent)", color: "var(--accent)", background: "transparent",
              }}
            >{showAdd ? "Cancel" : "+ Add range"}</button>
          </div>

          {showAdd && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              <input
                type="text"
                placeholder="Name (e.g. Mid-morning)"
                value={addName}
                onChange={e => setAddName(e.target.value)}
                style={{
                  width: 180, padding: "3px 6px", background: "var(--bg)",
                  border: "1px solid var(--border)", borderRadius: 6,
                  color: "var(--text)", fontSize: 12, outline: "none",
                }}
              />
              <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--muted)" }}>
                Start
                <input
                  type="time" value={addStart} min="09:15" max="15:30"
                  onChange={e => setAddStart(e.target.value)}
                  style={{
                    padding: "3px 6px", background: "var(--bg)",
                    border: "1px solid var(--border)", borderRadius: 6,
                    color: "var(--text)", fontSize: 12, outline: "none",
                  }}
                />
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, color: "var(--muted)" }}>
                End
                <input
                  type="time" value={addEnd} min="09:15" max="15:30"
                  onChange={e => setAddEnd(e.target.value)}
                  style={{
                    padding: "3px 6px", background: "var(--bg)",
                    border: "1px solid var(--border)", borderRadius: 6,
                    color: "var(--text)", fontSize: 12, outline: "none",
                  }}
                />
              </label>
              {(["session", "standard"] as const).map(s => (
                <button
                  key={s}
                  onClick={() => setAddScope(s)}
                  style={{
                    padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
                    border: `1px solid ${addScope === s ? "var(--accent)" : "var(--border)"}`,
                    color: addScope === s ? "var(--accent)" : "var(--muted)",
                    background: "transparent",
                  }}
                >{s === "session" ? "This session only" : "Standard (every day)"}</button>
              ))}
              <button
                onClick={submitAddRange}
                disabled={addBusy}
                style={{
                  padding: "4px 12px", borderRadius: 6, fontSize: 11,
                  cursor: addBusy ? "wait" : "pointer",
                  border: "1px solid var(--bullish)", color: "var(--bullish)", background: "transparent",
                }}
              >{addBusy ? "Saving…" : "Save"}</button>
              {addErr && <span style={{ fontSize: 11, color: "var(--bearish)" }}>{addErr}</span>}
            </div>
          )}
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
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, minWidth: 800 }}>
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
                    {/* Symbol + news chip */}
                    <td style={{ padding: "7px 12px", whiteSpace: "nowrap" }}>
                      <span style={{ color: "var(--text)", fontWeight: 600 }}>{row.symbol}</span>
                      {row.has_news && (
                        <span
                          title={row.catalyst_line ?? undefined}
                          style={{
                            marginLeft: 6, fontSize: 12, cursor: row.catalyst_line ? "help" : "default",
                          }}
                        >📰</span>
                      )}
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
                    {/* OR Status */}
                    <td style={{ padding: "7px 12px" }}>
                      <ORBadge row={row} />
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
