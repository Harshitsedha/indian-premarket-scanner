"use client";

import { useEffect, useRef, useState } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

type ParamSpec = { name: string; type: string; default: number };
type FeatureSpec = { name: string; description: string; params: ParamSpec[] };

type Job = {
  id: string;
  mode: "run" | "record" | "train_test";
  params: Record<string, unknown>;
  status: "queued" | "running" | "done" | "error";
  summary: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  result_path: string | null;
};

type Ruleset = {
  id: string;
  job_id: string;
  train_csv: string;
  rules_raw: string;
  rules_parsed: {
    filters: Array<Record<string, unknown>>;
    confidence: string;
    claude_reasoning: string;
  };
  feature_stats: Record<string, unknown>;
  committed_at: string;
};

type ValidationCheck = { name: string; passed: boolean; detail: string | null };
type ValidationReport = {
  passed: boolean;
  checks: ValidationCheck[];
  verdict: string;
  risk_notes: string[];
};

type StrategyRow = {
  id: string;
  name: string;
  display_name: string;
  description: string;
  code?: string;                        // present in /all response; absent in /strategies
  validated: boolean;
  validation_report: ValidationReport | null;
  created_at: string;
  updated_at: string;
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function paramLabel(params: Record<string, unknown>): string {
  if (params.symbol) return params.symbol as string;
  if (params.test_symbol) return params.test_symbol as string;
  return "Watchlist";
}

function dateRange(params: Record<string, unknown>): string {
  const s = (params.start ?? params.test_start) as string | undefined;
  const e = (params.end   ?? params.test_end)   as string | undefined;
  return s && e ? `${s} → ${e}` : "—";
}

// ── Status pill ───────────────────────────────────────────────────────────────

function StatusPill({ status }: { status: Job["status"] }) {
  const map: Record<string, { bg: string; color: string }> = {
    queued:  { bg: "rgba(107,104,128,0.15)", color: "var(--muted)"    },
    running: { bg: "rgba(124,111,224,0.15)", color: "var(--accent)"   },
    done:    { bg: "rgba(29,158,117,0.15)",  color: "var(--bullish)"  },
    error:   { bg: "rgba(224,82,82,0.15)",   color: "var(--bearish)"  },
  };
  const s = map[status] ?? map.queued;
  return (
    <span style={{
      fontSize: 11, padding: "2px 8px", borderRadius: 999,
      backgroundColor: s.bg, color: s.color,
      fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em",
    }}>
      {status}
    </span>
  );
}

// ── Rule chip ─────────────────────────────────────────────────────────────────

function RuleChip({ label }: { label: string }) {
  return (
    <span style={{
      fontSize: 11, padding: "2px 8px", borderRadius: 6,
      backgroundColor: "rgba(124,111,224,0.10)", color: "var(--accent)",
      border: "1px solid rgba(124,111,224,0.25)", fontFamily: "monospace",
    }}>
      {label}
    </span>
  );
}

// ── Summary block (for done jobs) ─────────────────────────────────────────────

function SummaryBlock({ job, ruleset }: { job: Job; ruleset: Ruleset | null }) {
  if (!job.summary) return null;
  const s = job.summary;

  if (s.mode === "record") {
    return (
      <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>
        {s.total_candidates as number} candidates &nbsp;·&nbsp;
        {s.taken as number} taken &nbsp;·&nbsp;
        {s.flat as number} flat
        {Array.isArray(s.features) && (
          <span> · [{(s.features as string[]).join(", ")}]</span>
        )}
      </div>
    );
  }

  if (s.mode === "train_test") {
    const unf = s.unfiltered_trade_count as number;
    const fil = s.filtered_trade_count  as number;
    const wr  = s.win_rate   != null ? `${((s.win_rate as number) * 100).toFixed(1)}%` : "—";
    const exp = s.expectancy_pct != null ? `${(s.expectancy_pct as number).toFixed(2)}%` : "—";
    const pf  = s.profit_factor != null ? (s.profit_factor as number).toFixed(2) : "∞";

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 6 }}>
        {/* Rules chips */}
        {Array.isArray(s.rules_applied) && (s.rules_applied as string[]).length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {(s.rules_applied as string[]).map((r) => <RuleChip key={r} label={r} />)}
            <span style={{ fontSize: 11, color: "var(--muted)", marginLeft: 4 }}>
              ({s.confidence as string} confidence)
            </span>
          </div>
        )}

        {/* Claude reasoning */}
        {typeof s.claude_reasoning === "string" && (
          <p style={{ fontSize: 12, color: "var(--muted)", margin: 0, fontStyle: "italic", lineHeight: 1.5 }}>
            {s.claude_reasoning as string}
          </p>
        )}

        {/* Trade counts + expectancy */}
        <div style={{ fontSize: 12, color: "var(--muted)", fontFamily: "monospace" }}>
          {unf} unfiltered → <strong style={{ color: "var(--text)" }}>{fil} filtered</strong>
          &nbsp;·&nbsp; WR {wr} &nbsp;·&nbsp; E[P&L] {exp} &nbsp;·&nbsp; PF {pf}
        </div>

        {/* Lockout proof */}
        {ruleset && (
          <div style={{ fontSize: 11, color: "var(--muted)", borderTop: "1px solid var(--border)", paddingTop: 6, marginTop: 2 }}>
            Rules committed at{" "}
            <span style={{ fontFamily: "monospace", color: "var(--text)" }}>
              {new Date(ruleset.committed_at).toISOString()}
            </span>
            {" "}(before test range was fetched) · ruleset {ruleset.id.slice(0, 8)}
          </div>
        )}
      </div>
    );
  }

  // Run mode
  const n   = s.total_trades as number;
  const wr  = s.win_rate   != null ? `${((s.win_rate as number) * 100).toFixed(1)}%` : "—";
  const exp = s.expectancy_pct != null ? `${(s.expectancy_pct as number).toFixed(2)}%` : "—";
  const pf  = s.profit_factor != null ? (s.profit_factor as number).toFixed(2) : "∞";
  return (
    <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 4, fontFamily: "monospace" }}>
      {n} trades &nbsp;·&nbsp; WR {wr} &nbsp;·&nbsp; E[P&L] {exp} &nbsp;·&nbsp; PF {pf}
    </div>
  );
}

// ── Job list row ──────────────────────────────────────────────────────────────

function JobRow({ job }: { job: Job }) {
  const [expanded, setExpanded] = useState(false);
  const [ruleset, setRuleset]   = useState<Ruleset | null>(null);

  // Fetch ruleset on expand if this is a done train_test job
  useEffect(() => {
    if (expanded && job.mode === "train_test" && job.status === "done" && !ruleset) {
      fetch(`/api/backtest/rulesets/${job.id}`)
        .then((r) => r.json())
        .then((d) => setRuleset(d))
        .catch(() => {});
    }
  }, [expanded, job.id, job.mode, job.status, ruleset]);

  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <button
        onClick={() => setExpanded((x) => !x)}
        style={{
          width: "100%", display: "grid",
          gridTemplateColumns: "80px 90px 110px 1fr 80px 70px",
          gap: 8, padding: "10px 14px", textAlign: "left",
          border: "none",
          backgroundColor: expanded ? "var(--surface)" : "transparent",
          cursor: "pointer", color: "var(--text)", fontSize: 13, alignItems: "center",
        }}
      >
        <StatusPill status={job.status} />
        <span style={{ color: "var(--accent)", fontSize: 12 }}>{job.mode}</span>
        <span style={{ fontFamily: "monospace", fontSize: 12 }}>{paramLabel(job.params)}</span>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>{dateRange(job.params)}</span>
        <span style={{ color: "var(--muted)", fontSize: 11 }}>{fmtTime(job.created_at)}</span>
        {job.status === "done" && job.result_path ? (
          <a
            href={`/api/backtest/jobs/${job.id}/result`}
            download
            onClick={(e) => e.stopPropagation()}
            style={{
              fontSize: 11, padding: "2px 8px", borderRadius: 6,
              backgroundColor: "rgba(124,111,224,0.15)", color: "var(--accent)",
              textDecoration: "none", textAlign: "center",
            }}
          >
            CSV
          </a>
        ) : <span />}
      </button>

      {expanded && (
        <div style={{
          padding: "10px 14px 14px", backgroundColor: "var(--bg)",
          borderTop: "1px solid var(--border)",
          display: "flex", flexDirection: "column", gap: 6,
        }}>
          {job.status === "done" && <SummaryBlock job={job} ruleset={ruleset} />}
          {job.status === "error" && (
            <pre style={{ fontSize: 11, color: "var(--bearish)", whiteSpace: "pre-wrap", margin: 0 }}>
              {job.error?.slice(0, 800)}
            </pre>
          )}
          {(job.status === "queued" || job.status === "running") && (
            <p style={{ fontSize: 12, color: "var(--muted)", margin: 0 }}>
              {job.status === "running"
                ? "Running… started at " + fmtTime(job.started_at)
                : "Waiting in queue"}
            </p>
          )}
          <div style={{ fontSize: 11, color: "var(--muted)", fontFamily: "monospace", marginTop: 4 }}>
            id: {job.id}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Feature picker ────────────────────────────────────────────────────────────

function FeaturePicker({
  features,
  selected,
  paramOverrides,
  onToggle,
  onParamChange,
  warnFeatures,
}: {
  features: FeatureSpec[];
  selected: Set<string>;
  paramOverrides: Record<string, Record<string, string>>;
  onToggle: (name: string) => void;
  onParamChange: (feature: string, param: string, value: string) => void;
  warnFeatures?: Set<string>;  // features in train job but not selected here
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {features.map((f) => {
        const mismatch = warnFeatures?.has(f.name) && !selected.has(f.name);
        return (
          <label
            key={f.name}
            style={{
              display: "grid",
              gridTemplateColumns: "18px 1fr auto",
              gap: 10, alignItems: "center",
              padding: "8px 10px",
              border: `1px solid ${mismatch ? "rgba(224,82,82,0.5)" : selected.has(f.name) ? "var(--accent)" : "var(--border)"}`,
              borderRadius: 8,
              backgroundColor: selected.has(f.name) ? "rgba(124,111,224,0.06)" : "transparent",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              checked={selected.has(f.name)}
              onChange={() => onToggle(f.name)}
              onClick={(e) => e.stopPropagation()}
              style={{ accentColor: "var(--accent)", cursor: "pointer" }}
            />
            <span>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{f.name}</span>
              <span style={{ color: "var(--muted)", fontSize: 11, marginLeft: 8 }}>{f.description}</span>
              {mismatch && (
                <span style={{ color: "var(--bearish)", fontSize: 10, marginLeft: 8 }}>
                  (used in train job)
                </span>
              )}
            </span>
            {f.params.length > 0 && selected.has(f.name) && (
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                {f.params.map((p) => (
                  <label key={p.name} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--muted)" }}>
                    {p.name}:
                    <input
                      type="number"
                      value={paramOverrides[f.name]?.[p.name] ?? String(p.default)}
                      onChange={(e) => onParamChange(f.name, p.name, e.target.value)}
                      onClick={(e) => e.preventDefault()}
                      style={{
                        width: 52, padding: "2px 6px", fontSize: 12,
                        backgroundColor: "var(--bg)", border: "1px solid var(--border)",
                        borderRadius: 4, color: "var(--text)",
                      }}
                    />
                  </label>
                ))}
              </div>
            )}
            {f.params.length > 0 && !selected.has(f.name) && (
              <span style={{ fontSize: 11, color: "var(--muted)" }}>
                {f.params.map((p) => `${p.name}=${p.default}`).join(", ")}
              </span>
            )}
          </label>
        );
      })}
    </div>
  );
}

// ── Field wrapper ─────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{
        fontSize: 11, color: "var(--muted)", fontWeight: 600,
        textTransform: "uppercase", letterSpacing: "0.05em",
      }}>
        {label}
      </label>
      {children}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "7px 10px", borderRadius: 8, fontSize: 13,
  backgroundColor: "var(--bg)", border: "1px solid var(--border)",
  color: "var(--text)", outline: "none",
};

// ── Scope toggle (shared) ─────────────────────────────────────────────────────

function ScopeToggle({
  scope, symbol, setScope, setSymbol, symbolKey,
}: {
  scope: "symbol" | "multi";
  symbol: string;
  setScope: (s: "symbol" | "multi") => void;
  setSymbol: (s: string) => void;
  symbolKey: string;
}) {
  const [symbolList, setSymbolList]     = useState<string[]>([]);
  const [filtered, setFiltered]         = useState<string[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [activeIdx, setActiveIdx]       = useState(-1);
  const debounceRef = useRef(0);
  const wrapperRef  = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/backtest/symbols")
      .then(r => r.json())
      .then((data: Array<{ symbol: string }>) => setSymbolList(data.map(d => d.symbol)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  function filterSymbols(val: string) {
    if (!val) { setFiltered([]); setShowDropdown(false); return; }
    const q = val.toUpperCase();
    const matches = symbolList.filter(s => s.includes(q)).slice(0, 8);
    setFiltered(matches);
    setShowDropdown(matches.length > 0);
    setActiveIdx(-1);
  }

  function handleSelect(s: string) {
    setSymbol(s);
    setShowDropdown(false);
    setFiltered([]);
    setActiveIdx(-1);
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: 12, alignItems: "center" }}>
      <div style={{ display: "flex", gap: 0, borderRadius: 8, overflow: "hidden", border: "1px solid var(--border)" }}>
        {(["symbol", "multi"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setScope(s)}
            style={{
              padding: "5px 16px", fontSize: 12, border: "none", cursor: "pointer",
              backgroundColor: scope === s ? "rgba(124,111,224,0.2)" : "transparent",
              color: scope === s ? "var(--accent)" : "var(--muted)",
            }}
          >
            {s === "symbol" ? "Single" : "Watchlist"}
          </button>
        ))}
      </div>
      {scope === "symbol" ? (
        <div ref={wrapperRef} style={{ position: "relative" }}>
          <input
            value={symbol}
            onChange={(e) => {
              const val = e.target.value.toUpperCase();
              setSymbol(val);
              clearTimeout(debounceRef.current);
              debounceRef.current = window.setTimeout(() => filterSymbols(val), 150);
            }}
            onKeyDown={(e) => {
              if (!showDropdown || filtered.length === 0) return;
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActiveIdx(i => Math.min(i + 1, filtered.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActiveIdx(i => Math.max(i - 1, -1));
              } else if (e.key === "Enter" && activeIdx >= 0) {
                e.preventDefault();
                handleSelect(filtered[activeIdx]);
              } else if (e.key === "Escape") {
                setShowDropdown(false);
                setActiveIdx(-1);
              }
            }}
            onFocus={() => { if (filtered.length > 0) setShowDropdown(true); }}
            placeholder={`NSE ticker for ${symbolKey}`}
            style={{ ...inputStyle, fontFamily: "monospace", width: "100%", boxSizing: "border-box" }}
            required
            autoComplete="off"
          />
          {showDropdown && (
            <ul style={{
              position: "absolute", top: "100%", left: 0, right: 0,
              margin: 0, padding: 4, listStyle: "none",
              backgroundColor: "var(--surface)", border: "1px solid var(--border)",
              borderRadius: 8, zIndex: 100, marginTop: 2,
              boxShadow: "0 4px 16px rgba(0,0,0,0.4)",
              maxHeight: 240, overflowY: "auto",
            }}>
              {filtered.map((s, i) => (
                <li
                  key={s}
                  onMouseDown={() => handleSelect(s)}
                  onMouseEnter={() => setActiveIdx(i)}
                  style={{
                    padding: "6px 10px", fontSize: 13, cursor: "pointer",
                    fontFamily: "monospace", borderRadius: 6,
                    backgroundColor: i === activeIdx ? "rgba(124,111,224,0.25)" : "transparent",
                    color: "var(--text)",
                  }}
                >
                  {s}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <span style={{ fontSize: 12, color: "var(--muted)" }}>50 symbols · SCAN_WATCHLIST</span>
      )}
    </div>
  );
}

// ── Strategy Manager ──────────────────────────────────────────────────────────

type ManagerView = "list" | "describe" | "generating" | "edit" | "validating" | "report";

function toMachineName(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}
function isValidMachineName(s: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(s);
}

function StrategyManager({ onStrategyChange }: { onStrategyChange: () => void }) {
  const [allStrategies, setAllStrategies] = useState<StrategyRow[]>([]);
  const [refreshKey, setRefreshKey]       = useState(0);
  const [view, setView]                   = useState<ManagerView>("list");
  const [isExisting, setIsExisting]       = useState(false);

  // Form fields
  const [displayName, setDisplayName]           = useState("");
  const [machineName, setMachineName]           = useState("");
  const [machineNameTouched, setMachineNameTouched] = useState(false);
  const [description, setDescription]           = useState("");
  const [code, setCode]                         = useState("");
  const [formError, setFormError]               = useState("");
  const [report, setReport]                     = useState<ValidationReport | null>(null);

  useEffect(() => {
    fetch("/api/backtest/strategies/all")
      .then(r => r.json())
      .then(setAllStrategies)
      .catch(() => {});
  }, [refreshKey]);

  function refresh() { setRefreshKey(k => k + 1); }

  function resetForm() {
    setDisplayName(""); setMachineName(""); setMachineNameTouched(false);
    setDescription(""); setCode(""); setFormError(""); setReport(null);
  }

  function startNew() {
    resetForm(); setIsExisting(false); setView("describe");
  }

  function startEdit(row: StrategyRow) {
    setDisplayName(row.display_name);
    setMachineName(row.name);
    setMachineNameTouched(true);     // name is locked for existing strategies
    setDescription(row.description);
    setCode(row.code ?? "");
    setFormError(""); setReport(null);
    setIsExisting(true);
    setView("edit");                 // skip describe; go straight to editor
  }

  async function handleGenerate() {
    if (!description.trim()) { setFormError("Enter a description first."); return; }
    setView("generating"); setFormError("");
    try {
      const res = await fetch("/api/backtest/strategies/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          ...(code.trim() ? { existing_code: code } : {}),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Generation failed");
      setCode(data.code);
      setView("edit");
    } catch (err) {
      setFormError(String(err));
      setView("describe");
    }
  }

  async function handleValidate() {
    if (!isValidMachineName(machineName)) {
      setFormError("Machine name: lowercase letters/digits/underscores, start with a letter.");
      return;
    }
    if (!displayName.trim()) { setFormError("Display name is required."); return; }
    if (!code.trim())        { setFormError("Code cannot be empty."); return; }
    setView("validating"); setFormError("");
    try {
      const res = await fetch("/api/backtest/strategies/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: machineName, display_name: displayName, description, code }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Request failed");
      setReport(data);
      setView("report");
      if (data.passed) { refresh(); onStrategyChange(); }
    } catch (err) {
      setFormError(String(err));
      setView("edit");
    }
  }

  async function handleDelete(name: string, dName: string) {
    if (!confirm(`Delete strategy "${dName}"? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/backtest/strategies/${name}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) { alert(data.detail ?? "Delete failed"); return; }
      refresh(); onStrategyChange();
    } catch { alert("Delete failed — check the console."); }
  }

  // ── Shared styles ─────────────────────────────────────────────────────────
  const btn = (accent = false): React.CSSProperties => ({
    padding: "7px 16px", borderRadius: 8, fontSize: 12, cursor: "pointer",
    border: accent ? "none" : "1px solid var(--border)",
    backgroundColor: accent ? "var(--accent)" : "transparent",
    color: accent ? "#fff" : "var(--muted)", fontWeight: accent ? 600 : 400,
  });
  const dangerBtn: React.CSSProperties = {
    padding: "4px 10px", borderRadius: 6, fontSize: 11, cursor: "pointer",
    border: "1px solid rgba(224,82,82,0.4)", backgroundColor: "transparent",
    color: "var(--bearish)",
  };

  const validatedPill = (v: boolean) => (
    <span style={{
      fontSize: 10, padding: "2px 7px", borderRadius: 999, fontWeight: 600,
      textTransform: "uppercase" as const, letterSpacing: "0.05em",
      backgroundColor: v ? "rgba(29,158,117,0.15)" : "rgba(224,180,82,0.15)",
      color: v ? "var(--bullish)" : "rgb(200,155,60)",
    }}>
      {v ? "Validated" : "Draft"}
    </span>
  );

  // ── Loading states ────────────────────────────────────────────────────────
  if (view === "generating" || view === "validating") {
    return (
      <div style={{ padding: "20px 0", color: "var(--muted)", fontSize: 13, display: "flex", alignItems: "center", gap: 10 }}>
        <style>{`@keyframes stratSpin{to{transform:rotate(360deg)}}`}</style>
        <span style={{ display: "inline-block", width: 14, height: 14, border: "2px solid var(--accent)", borderTopColor: "transparent", borderRadius: "50%", animation: "stratSpin 0.8s linear infinite" }} />
        {view === "generating" ? "Claude is writing your strategy…" : "Claude is validating your strategy…"}
      </div>
    );
  }

  // ── Validation report (Step 3) ────────────────────────────────────────────
  if (view === "report" && report) {
    const checkLabel: Record<string, string> = {
      no_look_ahead:          "No look-ahead",
      no_future_imports:      "No future imports",
      protocol_compliance:    "Protocol compliance",
      reset_day_clears_state: "reset_day clears state",
      on_bar_no_side_effects: "on_bar no side effects",
    };
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ fontWeight: 600, fontSize: 13, margin: 0 }}>Validation Report</p>

        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {report.checks.map(c => (
            <div key={c.name} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12 }}>
              <span style={{ color: c.passed ? "var(--bullish)" : "var(--bearish)", fontWeight: 700, minWidth: 14 }}>
                {c.passed ? "✓" : "✗"}
              </span>
              <span style={{ color: c.passed ? "var(--text)" : "var(--bearish)", fontWeight: 600, minWidth: 180 }}>
                {checkLabel[c.name] ?? c.name}
              </span>
              {c.detail && (
                <span style={{ color: "var(--muted)", fontFamily: "monospace" }}>{c.detail}</span>
              )}
            </div>
          ))}
        </div>

        {report.risk_notes.length > 0 && (
          <div style={{ fontSize: 11, color: "var(--muted)", fontStyle: "italic" }}>
            Notes: {report.risk_notes.join(" · ")}
          </div>
        )}

        <p style={{
          margin: 0, fontSize: 12, padding: "8px 12px", borderRadius: 8,
          backgroundColor: report.passed ? "rgba(29,158,117,0.10)" : "rgba(224,82,82,0.10)",
          color: report.passed ? "var(--bullish)" : "var(--bearish)",
        }}>
          {report.passed
            ? `✓ ${report.verdict} Strategy "${displayName}" is now registered and appears in the dropdown.`
            : `✗ ${report.verdict} Fix the issues above and revalidate.`}
        </p>

        {report.passed
          ? <button type="button" style={btn()} onClick={() => { setView("list"); resetForm(); }}>Done</button>
          : <button type="button" style={btn(true)} onClick={() => setView("edit")}>Edit Code</button>
        }
      </div>
    );
  }

  // ── Code editor (Step 2) ──────────────────────────────────────────────────
  if (view === "edit") {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>{displayName || "New Strategy"}</span>
          <span style={{ fontSize: 11, fontFamily: "monospace", color: "var(--muted)" }}>{machineName}</span>
        </div>

        <textarea
          value={code}
          onChange={e => setCode(e.target.value)}
          spellCheck={false}
          style={{
            width: "100%", minHeight: 340, padding: "10px 12px",
            fontFamily: "monospace", fontSize: 12, lineHeight: 1.55,
            backgroundColor: "var(--bg)", border: "1px solid var(--border)",
            borderRadius: 8, color: "var(--text)", resize: "vertical",
            boxSizing: "border-box",
          }}
          placeholder="class MyStrategy:&#10;    ..."
        />

        {formError && (
          <p style={{ color: "var(--bearish)", fontSize: 12, margin: 0 }}>{formError}</p>
        )}

        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" style={btn()} onClick={() => setView(isExisting ? "list" : "describe")}>
            ← Back
          </button>
          <button type="button" style={btn(true)} onClick={handleValidate}>
            Validate &amp; Save
          </button>
        </div>
      </div>
    );
  }

  // ── Describe form (Step 1) ────────────────────────────────────────────────
  if (view === "describe") {
    const nameOk = !machineName || isValidMachineName(machineName);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Display name
            </label>
            <input
              value={displayName}
              onChange={e => {
                setDisplayName(e.target.value);
                if (!machineNameTouched) setMachineName(toMachineName(e.target.value));
              }}
              placeholder="e.g. EMA Crossover"
              style={inputStyle}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Machine name{isExisting ? " (locked)" : " (auto-derived, editable)"}
            </label>
            <input
              value={machineName}
              onChange={e => { setMachineName(e.target.value); setMachineNameTouched(true); }}
              placeholder="ema_crossover"
              disabled={isExisting}
              style={{
                ...inputStyle,
                fontFamily: "monospace",
                borderColor: nameOk ? undefined : "var(--bearish)",
                opacity: isExisting ? 0.6 : 1,
              }}
            />
            {!nameOk && (
              <span style={{ fontSize: 10, color: "var(--bearish)" }}>
                Lowercase letters/digits/underscores, start with a letter
              </span>
            )}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
            Describe your strategy in plain English
          </label>
          <textarea
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="e.g. Buy when the 9-period EMA crosses above the 21-period EMA on the first occurrence of the day…"
            style={{
              ...inputStyle, minHeight: 90, resize: "vertical",
              fontFamily: "inherit", lineHeight: 1.5,
            }}
          />
        </div>

        {formError && (
          <p style={{ color: "var(--bearish)", fontSize: 12, margin: 0 }}>{formError}</p>
        )}

        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" style={btn()} onClick={() => setView("list")}>← List</button>
          <button type="button" style={btn(true)} onClick={handleGenerate}>
            {code.trim() ? "Regenerate Code" : "Generate Code"}
          </button>
          {code.trim() && (
            <button type="button" style={btn()} onClick={() => setView("edit")}>
              Skip to Editor →
            </button>
          )}
        </div>
      </div>
    );
  }

  // ── Strategy list ─────────────────────────────────────────────────────────
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button type="button" style={btn(true)} onClick={startNew}>+ New Strategy</button>
      </div>

      {allStrategies.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>No strategies yet.</p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {allStrategies.map(row => (
            <div key={row.name} style={{
              display: "grid", gridTemplateColumns: "140px auto 1fr auto",
              gap: 10, alignItems: "center", padding: "9px 12px",
              borderBottom: "1px solid var(--border)",
            }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{row.display_name}</span>
              {validatedPill(row.validated)}
              <span style={{ color: "var(--muted)", fontSize: 12 }}>{row.description}</span>
              <div style={{ display: "flex", gap: 6 }}>
                {row.name !== "gap_and_go" && (
                  <>
                    <button type="button" style={btn()} onClick={() => startEdit(row)}>Edit</button>
                    <button type="button" style={dangerBtn} onClick={() => handleDelete(row.name, row.display_name)}>Delete</button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ── Main page ─────────────────────────────────────────────────────────────────

export default function BacktestPage() {
  // Feature registry
  const [features, setFeatures]       = useState<FeatureSpec[]>([]);
  const [featuresErr, setFeaturesErr] = useState("");

  // Validated strategies (for job form dropdown)
  const [validatedStrategies, setValidatedStrategies] = useState<StrategyRow[]>([]);
  const [stratRefreshKey, setStratRefreshKey]         = useState(0);

  // Strategy Manager panel visibility
  const [showManager, setShowManager] = useState(false);

  // Mode toggle
  const [mode, setMode] = useState<"run" | "record" | "train_test">("run");

  // run / record shared fields
  const [scope, setScope]   = useState<"symbol" | "multi">("symbol");
  const [symbol, setSymbol] = useState("TATASTEEL");
  const [start, setStart]   = useState("2026-05-01");
  const [end, setEnd]       = useState("2026-06-04");

  // Record feature selection (shared with train_test)
  const [selectedFeatures, setSelectedFeatures] = useState<Set<string>>(new Set());
  const [paramOverrides, setParamOverrides]     = useState<Record<string, Record<string, string>>>({});

  // train_test-specific fields
  const [trainJobs, setTrainJobs]           = useState<Job[]>([]);
  const [selectedTrainJob, setSelectedTrainJob] = useState<Job | null>(null);
  const [testScope, setTestScope]           = useState<"symbol" | "multi">("symbol");
  const [testSymbol, setTestSymbol]         = useState("TATASTEEL");
  const [testStart, setTestStart]           = useState("2026-06-01");
  const [testEnd, setTestEnd]               = useState("2026-06-04");

  // Strategy selector
  const [strategy, setStrategy] = useState("gap_and_go");

  // Advanced / strategy params
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [strategyParams, setStrategyParams] = useState({
    min_gap_pct:       "1.0",
    opening_range_min: "15",
    stop_pct:          "1.0",
    target_r:          "2.0",
    entry_window_min:  "60",
  });

  // Submit state
  const [submitting, setSubmitting]   = useState(false);
  const [submitError, setSubmitError] = useState("");

  // Job list
  const [jobs, setJobs] = useState<Job[]>([]);
  const pollRef         = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch feature registry once ───────────────────────────────────────────
  useEffect(() => {
    fetch(`/api/backtest/features`)
      .then((r) => r.json())
      .then((d) => setFeatures(d.features ?? []))
      .catch(() => setFeaturesErr("Could not load feature list — is the API running?"));
  }, []);

  // ── Fetch validated strategies for the dropdown ───────────────────────────
  useEffect(() => {
    fetch("/api/backtest/strategies")
      .then(r => r.json())
      .then((list: StrategyRow[]) => setValidatedStrategies(list))
      .catch(() => {});
  }, [stratRefreshKey]);

  // ── Poll job list every 2s ────────────────────────────────────────────────
  useEffect(() => {
    const poll = () => {
      fetch(`/api/backtest/jobs?limit=50`)
        .then((r) => r.json())
        .then((d: Job[]) => {
          setJobs(d);
          // Keep completed Record jobs for the train dropdown
          setTrainJobs(d.filter((j) => j.mode === "record" && j.status === "done"));
        })
        .catch(() => {});
    };
    poll();
    pollRef.current = setInterval(poll, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  // ── Build feature string ──────────────────────────────────────────────────
  function buildFeaturesStr(): string {
    return Array.from(selectedFeatures)
      .map((name) => {
        const spec = features.find((f) => f.name === name);
        if (!spec || spec.params.length === 0) return name;
        const overrides = paramOverrides[name] ?? {};
        const parts = spec.params
          .map((p) => `${p.name}=${overrides[p.name] ?? String(p.default)}`)
          .join("&");
        return `${name}:${parts}`;
      })
      .join(",");
  }

  // Features used in the selected train job (for mismatch warning)
  const trainJobFeatureNames: Set<string> = (() => {
    if (!selectedTrainJob?.summary) return new Set();
    const feats = (selectedTrainJob.summary as Record<string, unknown>).features;
    if (!Array.isArray(feats)) return new Set();
    // strip param suffixes to get base names: "gap_pct", "or_width_15" → "gap_pct", "or_width"
    return new Set(
      (feats as string[]).map((f) => f.split("_").slice(0, -1).join("_") || f)
    );
  })();

  // ── Submit ────────────────────────────────────────────────────────────────
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError("");

    if ((mode === "record" || mode === "train_test") && selectedFeatures.size === 0) {
      setSubmitError("Select at least one feature.");
      return;
    }
    if (mode === "train_test" && !selectedTrainJob) {
      setSubmitError("Select a completed Record job as the training set.");
      return;
    }

    setSubmitting(true);
    let body: Record<string, unknown>;

    if (mode === "train_test") {
      const trainCsv = (selectedTrainJob!.summary as Record<string, unknown>).result_path
        ?? selectedTrainJob!.result_path;
      // Prefer result_path on the job row; summary.result_path is a fallback
      body = {
        mode:       "train_test",
        train_csv:  selectedTrainJob!.result_path ?? trainCsv,
        test_start: testStart,
        test_end:   testEnd,
        features:   buildFeaturesStr(),
        strategy,
        ca_ack: true,
      };
      if (testScope === "symbol") {
        body.test_symbol = testSymbol.trim().toUpperCase();
      } else {
        body.test_multi = true;
      }
    } else {
      body = {
        mode,
        start,
        end,
        strategy,
        interval: "minutes/1",
        ca_ack: true,
        strategy_params: {
          min_gap_pct:       parseFloat(strategyParams.min_gap_pct),
          opening_range_min: parseInt(strategyParams.opening_range_min, 10),
          stop_pct:          parseFloat(strategyParams.stop_pct),
          target_r:          parseFloat(strategyParams.target_r),
          entry_window_min:  parseInt(strategyParams.entry_window_min, 10),
        },
      };
      if (scope === "symbol") {
        body.symbol = symbol.trim().toUpperCase();
      } else {
        body.multi = true;
      }
      if (mode === "record") {
        body.features = buildFeaturesStr();
      }
    }

    try {
      console.log("[backtest] POST body:", JSON.stringify(body, null, 2));
      const res = await fetch(`/api/backtest/jobs`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = data.detail ?? JSON.stringify(data);
        setSubmitError(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      const updated = await fetch(`/api/backtest/jobs?limit=50`).then((r) => r.json());
      setJobs(updated);
    } catch (err) {
      setSubmitError(String(err));
    } finally {
      setSubmitting(false);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────
  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running").length;

  const modeLabels: Record<string, string> = {
    run:        "Run",
    record:     "Record",
    train_test: "Train → Test",
  };
  const submitLabel: Record<string, string> = {
    run:        "Run Backtest",
    record:     "Run Record",
    train_test: "Submit Train → Test",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <h1 style={{ color: "var(--muted)", fontSize: 20, fontWeight: 500 }}>Backtester</h1>
        {activeJobs > 0 && (
          <span style={{ fontSize: 12, color: "var(--accent)" }}>
            {activeJobs} job{activeJobs > 1 ? "s" : ""} active
          </span>
        )}
      </div>

      {/* ── Strategy Manager ── */}
      <div style={{
        backgroundColor: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: 12, overflow: "hidden",
      }}>
        <button
          type="button"
          onClick={() => setShowManager(x => !x)}
          style={{
            width: "100%", display: "flex", alignItems: "center", gap: 8,
            padding: "12px 16px", border: "none", background: "transparent",
            cursor: "pointer", color: "var(--text)", fontSize: 13, fontWeight: 500,
          }}
        >
          <span style={{ fontSize: 10, color: "var(--muted)" }}>{showManager ? "▾" : "▸"}</span>
          Strategy Manager
          <span style={{ fontSize: 11, color: "var(--muted)", fontWeight: 400, marginLeft: 4 }}>
            — generate, validate, and register new strategies without redeploying
          </span>
        </button>
        {showManager && (
          <div style={{ padding: "0 16px 16px", borderTop: "1px solid var(--border)" }}>
            <StrategyManager onStrategyChange={() => setStratRefreshKey(k => k + 1)} />
          </div>
        )}
      </div>

      {/* ── Form ── */}
      <form
        onSubmit={handleSubmit}
        style={{
          backgroundColor: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: 12, padding: 20,
          display: "flex", flexDirection: "column", gap: 16,
        }}
      >
        {/* Mode toggle */}
        <div style={{
          display: "flex", gap: 0, borderRadius: 8, overflow: "hidden",
          border: "1px solid var(--border)", alignSelf: "flex-start",
        }}>
          {(["run", "record", "train_test"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              style={{
                padding: "7px 20px", fontSize: 13, border: "none", cursor: "pointer",
                backgroundColor: mode === m ? "var(--accent)" : "transparent",
                color: mode === m ? "#fff" : "var(--muted)",
                fontWeight: mode === m ? 600 : 400,
              }}
            >
              {modeLabels[m]}
            </button>
          ))}
        </div>

        {/* ── Run / Record fields ── */}
        {(mode === "run" || mode === "record") && (
          <>
            <ScopeToggle
              scope={scope} symbol={symbol}
              setScope={setScope} setSymbol={setSymbol}
              symbolKey="backtest"
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Field label="Start">
                <input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle} required />
              </Field>
              <Field label="End">
                <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle} required />
              </Field>
            </div>

            {/* Strategy selector — populated from validated strategies in DB */}
            <Field label="Strategy">
              <select
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
                style={inputStyle}
              >
                {validatedStrategies.length > 0
                  ? validatedStrategies.map(s => (
                      <option key={s.name} value={s.name}>{s.display_name}</option>
                    ))
                  : <option value="gap_and_go">Gap and Go</option>
                }
              </select>
            </Field>

            {/* Advanced / strategy params */}
            <div>
              <button
                type="button"
                onClick={() => setShowAdvanced((x) => !x)}
                style={{
                  background: "none", border: "none", cursor: "pointer",
                  color: "var(--muted)", fontSize: 12, padding: 0,
                  display: "flex", alignItems: "center", gap: 4,
                }}
              >
                <span style={{ fontSize: 10 }}>{showAdvanced ? "▾" : "▸"}</span>
                Advanced (strategy params)
              </button>

              {showAdvanced && (
                <div style={{
                  marginTop: 10,
                  display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10,
                  padding: "12px 14px",
                  backgroundColor: "var(--bg)", border: "1px solid var(--border)",
                  borderRadius: 8,
                }}>
                  {(
                    [
                      { key: "min_gap_pct",       label: "Min gap %",          step: "0.1"  },
                      { key: "opening_range_min",  label: "Opening range (min)", step: "1"    },
                      { key: "stop_pct",           label: "Stop %",             step: "0.1"  },
                      { key: "target_r",           label: "Target R",           step: "0.1"  },
                      { key: "entry_window_min",   label: "Entry window (bars)", step: "1"    },
                    ] as Array<{ key: keyof typeof strategyParams; label: string; step: string }>
                  ).map(({ key, label, step }) => (
                    <Field key={key} label={label}>
                      <input
                        type="number"
                        step={step}
                        value={strategyParams[key]}
                        onChange={(e) =>
                          setStrategyParams((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        style={{ ...inputStyle, fontFamily: "monospace" }}
                      />
                    </Field>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {/* ── Train → Test fields ── */}
        {mode === "train_test" && (
          <>
            <Field label="Training set (completed Record job)">
              {trainJobs.length === 0 ? (
                <p style={{ fontSize: 12, color: "var(--bearish)", margin: 0 }}>
                  No completed Record jobs yet — run one first.
                </p>
              ) : (
                <select
                  value={selectedTrainJob?.id ?? ""}
                  onChange={(e) => {
                    const j = trainJobs.find((j) => j.id === e.target.value) ?? null;
                    setSelectedTrainJob(j);
                  }}
                  style={{ ...inputStyle }}
                  required
                >
                  <option value="">— select a Record job —</option>
                  {trainJobs.map((j) => {
                    const lbl = paramLabel(j.params);
                    const dr  = dateRange(j.params);
                    const s   = (j.summary as Record<string, unknown> | null);
                    const n   = String(s?.total_candidates ?? "?");
                    const feats = Array.isArray(s?.features) ? (s!.features as string[]).join(", ") : "";
                    return (
                      <option key={j.id} value={j.id}>
                        {lbl} · {dr} · {n} candidates · [{feats}]
                      </option>
                    );
                  })}
                </select>
              )}
            </Field>

            <Field label="Test scope">
              <ScopeToggle
                scope={testScope} symbol={testSymbol}
                setScope={setTestScope} setSymbol={setTestSymbol}
                symbolKey="test"
              />
            </Field>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Field label="Test start">
                <input type="date" value={testStart} onChange={(e) => setTestStart(e.target.value)} style={inputStyle} required />
              </Field>
              <Field label="Test end">
                <input type="date" value={testEnd} onChange={(e) => setTestEnd(e.target.value)} style={inputStyle} required />
              </Field>
            </div>
          </>
        )}

        {/* ── Feature picker (record + train_test) ── */}
        {(mode === "record" || mode === "train_test") && (
          <div>
            <p style={{
              fontSize: 11, color: "var(--muted)", fontWeight: 600,
              textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8,
            }}>
              Features
              {mode === "train_test" && selectedTrainJob && (
                <span style={{ fontWeight: 400, marginLeft: 8, color: "var(--muted)", textTransform: "none" }}>
                  (highlighted in red = used in train job but not selected)
                </span>
              )}
            </p>
            {featuresErr ? (
              <p style={{ color: "var(--bearish)", fontSize: 12 }}>{featuresErr}</p>
            ) : (
              <FeaturePicker
                features={features}
                selected={selectedFeatures}
                paramOverrides={paramOverrides}
                warnFeatures={mode === "train_test" ? trainJobFeatureNames : undefined}
                onToggle={(name) =>
                  setSelectedFeatures((prev) => {
                    const next = new Set(prev);
                    next.has(name) ? next.delete(name) : next.add(name);
                    return next;
                  })
                }
                onParamChange={(feat, param, val) =>
                  setParamOverrides((prev) => ({
                    ...prev,
                    [feat]: { ...(prev[feat] ?? {}), [param]: val },
                  }))
                }
              />
            )}
          </div>
        )}

        {submitError && (
          <p style={{ color: "var(--bearish)", fontSize: 12, margin: 0 }}>{submitError}</p>
        )}

        <button
          type="submit"
          disabled={submitting}
          style={{
            alignSelf: "flex-start", padding: "9px 24px", borderRadius: 8,
            backgroundColor: submitting ? "var(--border)" : "var(--accent)",
            color: "#fff", border: "none",
            cursor: submitting ? "not-allowed" : "pointer",
            fontSize: 13, fontWeight: 600,
          }}
        >
          {submitting ? "Submitting…" : submitLabel[mode]}
        </button>
      </form>

      {/* ── Job list ── */}
      <div>
        <p style={{ fontSize: 13, color: "var(--muted)", fontWeight: 500, marginBottom: 8 }}>
          Recent Jobs
          <span style={{ fontSize: 11, fontWeight: 400, marginLeft: 8 }}>
            (auto-refreshes every 2s · click a row to expand)
          </span>
        </p>

        {jobs.length === 0 ? (
          <div style={{
            backgroundColor: "var(--surface)", border: "1px solid var(--border)",
            borderRadius: 12, padding: 24, textAlign: "center",
          }}>
            <p style={{ color: "var(--muted)" }}>No jobs yet. Submit a run above.</p>
          </div>
        ) : (
          <div style={{ border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
            <div style={{
              display: "grid", gridTemplateColumns: "80px 90px 110px 1fr 80px 70px",
              gap: 8, padding: "7px 14px",
              backgroundColor: "var(--surface)", borderBottom: "1px solid var(--border)",
              fontSize: 11, color: "var(--muted)", fontWeight: 600,
              textTransform: "uppercase", letterSpacing: "0.05em",
            }}>
              <span>Status</span><span>Mode</span><span>Symbol</span>
              <span>Range</span><span>Time</span><span></span>
            </div>
            {jobs.map((j) => <JobRow key={j.id} job={j} />)}
          </div>
        )}
      </div>
    </div>
  );
}
