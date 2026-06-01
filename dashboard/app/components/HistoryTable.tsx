"use client";

import { useState } from "react";

type HistoryRow = {
  date: string;
  bias_direction: string;
  bias_confidence: number | null;
  setup_count: number;
  outcome_count: number;
  hypothesis_correct_rate: number | null;
  top_symbols: string[];
};

type Setup = {
  setup_id: number;
  symbol: string;
  setup_type: string;
  hypothesis: string;
  score: number;
  gap_pct: number | null;
  thesis: string;
  outcome: {
    move_pct: number | null;
    hypothesis_correct: boolean | null;
    result: string | null;
  } | null;
};

type NewsHeadline = {
  id: number;
  source: string;
  headline: string;
  url: string | null;
  sentiment: string | null;
  importance: number | null;
  reason: string | null;
  scraped_at: string | null;
};

const DIR_COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--neutral)",
};
const SENT_BG: Record<string, string> = {
  bullish: "rgba(29,158,117,0.15)",
  bearish: "rgba(224,82,82,0.15)",
  neutral: "rgba(107,104,128,0.15)",
};
const SENT_COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--muted)",
};

function impColor(imp: number | null): string {
  if (!imp) return "var(--border)";
  const t = (imp - 1) / 4;
  return `rgba(${Math.round(107 + t * 17)}, ${Math.round(104 + t * 7)}, ${Math.round(128 + t * 96)}, ${(0.35 + t * 0.65).toFixed(2)})`;
}

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

export function HistoryTable({ rows }: { rows: HistoryRow[] }) {
  const [expanded, setExpanded]   = useState<string | null>(null);
  const [setups, setSetups]       = useState<Record<string, Setup[]>>({});
  const [headlines, setHeadlines] = useState<Record<string, NewsHeadline[]>>({});
  const [loading, setLoading]     = useState<string | null>(null);

  async function toggle(date: string) {
    if (expanded === date) {
      setExpanded(null);
      return;
    }
    setExpanded(date);
    if (!setups[date]) {
      setLoading(date);
      try {
        const [setupsRes, newsRes] = await Promise.all([
          fetch(`${API}/api/setups/${date}`),
          fetch(`${API}/api/news/${date}`),
        ]);
        const setupsData = await setupsRes.json();
        const newsData   = newsRes.ok ? await newsRes.json() : { headlines: [] };
        setSetups((prev) => ({ ...prev, [date]: setupsData }));
        setHeadlines((prev) => ({ ...prev, [date]: newsData.headlines ?? [] }));
      } finally {
        setLoading(null);
      }
    }
  }

  if (rows.length === 0) {
    return (
      <p style={{ color: "var(--muted)" }} className="py-8 text-center">
        No history yet.
      </p>
    );
  }

  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 12, overflow: "hidden" }}>
      {/* Header */}
      <div
        style={{
          backgroundColor: "var(--surface)",
          borderBottom: "1px solid var(--border)",
          color: "var(--muted)",
          display: "grid",
          gridTemplateColumns: "1fr 1fr repeat(3, 80px) 1fr",
          gap: 8,
          padding: "8px 16px",
          fontSize: 12,
          fontWeight: 500,
        }}
      >
        <span>Date</span>
        <span>Bias</span>
        <span style={{ textAlign: "center" }}>Setups</span>
        <span style={{ textAlign: "center" }}>Outcomes</span>
        <span style={{ textAlign: "center" }}>Hit rate</span>
        <span>Top symbols</span>
      </div>

      {rows.map((row) => {
        const dirColor  = DIR_COLOR[row.bias_direction] ?? "var(--neutral)";
        const isOpen    = expanded === row.date;
        const daySetups = setups[row.date];
        const dayNews   = headlines[row.date];

        return (
          <div key={row.date} style={{ borderBottom: "1px solid var(--border)" }}>
            {/* Row button */}
            <button
              onClick={() => toggle(row.date)}
              style={{
                width: "100%",
                display: "grid",
                gridTemplateColumns: "1fr 1fr repeat(3, 80px) 1fr",
                gap: 8,
                padding: "10px 16px",
                fontSize: 13,
                textAlign: "left",
                backgroundColor: isOpen ? "var(--surface)" : "transparent",
                cursor: "pointer",
                border: "none",
                color: "var(--text)",
              }}
            >
              <span style={{ fontFamily: "monospace", fontSize: 12 }}>{row.date}</span>
              <span style={{ color: dirColor, fontWeight: 500, textTransform: "capitalize" }}>
                {row.bias_direction}
              </span>
              <span style={{ textAlign: "center" }}>{row.setup_count}</span>
              <span style={{ textAlign: "center" }}>{row.outcome_count}</span>
              <span style={{ textAlign: "center" }}>
                {row.hypothesis_correct_rate != null
                  ? `${Math.round(row.hypothesis_correct_rate * 100)}%`
                  : "—"}
              </span>
              <span style={{ color: "var(--muted)", fontSize: 12 }}>
                {row.top_symbols.slice(0, 3).join(", ")}
              </span>
            </button>

            {/* Expanded panel */}
            {isOpen && (
              <div
                style={{
                  backgroundColor: "var(--bg)",
                  borderTop: "1px solid var(--border)",
                  padding: "12px 16px",
                }}
              >
                {loading === row.date ? (
                  <p style={{ color: "var(--muted)", fontSize: 13, padding: "8px 0" }}>
                    Loading…
                  </p>
                ) : (
                  <>
                    {/* Setups */}
                    {daySetups && daySetups.length > 0 && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 }}>
                        {daySetups.map((s) => (
                          <div
                            key={s.setup_id}
                            style={{
                              border: "1px solid var(--border)",
                              borderRadius: 8,
                              padding: "8px 12px",
                              fontSize: 13,
                              display: "grid",
                              gridTemplateColumns: "1fr 1fr 80px 64px 80px",
                              gap: 8,
                              alignItems: "center",
                            }}
                          >
                            <span style={{ fontWeight: 600 }}>{s.symbol}</span>
                            <span style={{ color: "var(--muted)", fontSize: 12 }}>
                              {s.setup_type.replace(/_/g, " ")}
                            </span>
                            <span
                              style={{
                                color: DIR_COLOR[s.hypothesis] ?? "var(--neutral)",
                                textTransform: "capitalize",
                              }}
                            >
                              {s.hypothesis}
                            </span>
                            <span style={{ color: "var(--muted)", fontFamily: "monospace", fontSize: 12 }}>
                              {s.gap_pct != null
                                ? `${s.gap_pct >= 0 ? "+" : ""}${s.gap_pct.toFixed(1)}%`
                                : "—"}
                            </span>
                            <span>
                              {s.outcome ? (
                                <span
                                  style={{
                                    color:
                                      s.outcome.hypothesis_correct === true
                                        ? "var(--bullish)"
                                        : s.outcome.hypothesis_correct === false
                                        ? "var(--bearish)"
                                        : "var(--muted)",
                                    fontFamily: "monospace",
                                    fontSize: 12,
                                  }}
                                >
                                  {s.outcome.move_pct != null
                                    ? `${s.outcome.move_pct >= 0 ? "+" : ""}${s.outcome.move_pct.toFixed(1)}%`
                                    : "—"}
                                </span>
                              ) : (
                                <span style={{ color: "var(--muted)", fontSize: 12 }}>
                                  no outcome
                                </span>
                              )}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Top headlines for this day */}
                    {dayNews && dayNews.length > 0 && (
                      <div>
                        <p
                          style={{
                            fontSize: 11,
                            color: "var(--muted)",
                            fontWeight: 600,
                            letterSpacing: "0.05em",
                            textTransform: "uppercase",
                            marginBottom: 8,
                          }}
                        >
                          Top headlines
                        </p>
                        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                          {dayNews.slice(0, 5).map((h) => {
                            const sent = (h.sentiment ?? "neutral").toLowerCase();
                            const imp  = h.importance ?? 0;
                            return (
                              <div
                                key={h.id}
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  gap: 8,
                                  padding: "6px 10px",
                                  backgroundColor: "var(--surface)",
                                  border: "0.5px solid var(--border)",
                                  borderRadius: 6,
                                  minWidth: 0,
                                }}
                              >
                                {/* Imp dot */}
                                <div
                                  style={{
                                    width: 3,
                                    height: imp > 0 ? imp * 5 + 4 : 4,
                                    backgroundColor: impColor(h.importance),
                                    borderRadius: 2,
                                    flexShrink: 0,
                                  }}
                                />
                                {/* Headline */}
                                {h.url ? (
                                  <a
                                    href={h.url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={{
                                      flex: 1,
                                      fontSize: 12,
                                      color: "var(--text)",
                                      textDecoration: "none",
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                      whiteSpace: "nowrap",
                                    }}
                                  >
                                    {h.headline}
                                  </a>
                                ) : (
                                  <span
                                    style={{
                                      flex: 1,
                                      fontSize: 12,
                                      color: "var(--text)",
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                      whiteSpace: "nowrap",
                                    }}
                                  >
                                    {h.headline}
                                  </span>
                                )}
                                {/* Sentiment chip */}
                                <span
                                  style={{
                                    fontSize: 10,
                                    padding: "1px 6px",
                                    borderRadius: 999,
                                    backgroundColor: SENT_BG[sent] ?? SENT_BG.neutral,
                                    color: SENT_COLOR[sent] ?? "var(--muted)",
                                    flexShrink: 0,
                                  }}
                                >
                                  {sent}
                                </span>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {(!daySetups || daySetups.length === 0) &&
                      (!dayNews || dayNews.length === 0) && (
                        <p style={{ color: "var(--muted)", fontSize: 13 }}>No data.</p>
                      )}
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
