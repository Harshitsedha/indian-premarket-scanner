"use client";

import { useEffect, useMemo, useState } from "react";

type Headline = {
  id: number;
  source: string;
  headline: string;
  url: string | null;
  sentiment: string | null;
  importance: number | null;
  reason: string | null;
  scraped_at: string | null;
};

type NewsData = {
  date: string;
  count: number;
  headlines: Headline[];
};

type SortKey = "importance" | "newest" | "sentiment" | "source";
type SentKey = "all" | "bullish" | "bearish" | "neutral";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

const SENT_COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--muted)",
};
const SENT_BG: Record<string, string> = {
  bullish: "rgba(29,158,117,0.15)",
  bearish: "rgba(224,82,82,0.15)",
  neutral: "rgba(107,104,128,0.15)",
};
const SORT_SENT: Record<string, number> = { bullish: 0, neutral: 1, bearish: 2 };

function impColor(imp: number | null): string {
  if (!imp) return "var(--border)";
  const t = (imp - 1) / 4; // 0→1
  return `rgba(${Math.round(107 + t * 17)}, ${Math.round(104 + t * 7)}, ${Math.round(128 + t * 96)}, ${(0.35 + t * 0.65).toFixed(2)})`;
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function NewsPage() {
  const [data, setData] = useState<NewsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [sort, setSort] = useState<SortKey>("importance");
  const [sentFilter, setSentFilter] = useState<SentKey>("all");
  const [sourceFilter, setSourceFilter] = useState("all");

  useEffect(() => {
    fetch(`${API}/api/news/today`)
      .then((r) => r.json())
      .then(setData)
      .catch(() =>
        setData({ date: new Date().toISOString().slice(0, 10), count: 0, headlines: [] })
      )
      .finally(() => setLoading(false));
  }, []);

  const sources = useMemo(
    () => Array.from(new Set((data?.headlines ?? []).map((h) => h.source))).sort(),
    [data]
  );

  const filtered = useMemo(() => {
    let hs = data?.headlines ?? [];
    if (sentFilter !== "all") hs = hs.filter((h) => (h.sentiment ?? "neutral") === sentFilter);
    if (sourceFilter !== "all") hs = hs.filter((h) => h.source === sourceFilter);
    return [...hs].sort((a, b) => {
      if (sort === "importance") {
        const d = (b.importance ?? 0) - (a.importance ?? 0);
        if (d !== 0) return d;
        return (b.scraped_at ?? "") < (a.scraped_at ?? "") ? -1 : 1;
      }
      if (sort === "newest")
        return (b.scraped_at ?? "") < (a.scraped_at ?? "") ? -1 : 1;
      if (sort === "sentiment")
        return (
          (SORT_SENT[a.sentiment ?? "neutral"] ?? 1) -
          (SORT_SENT[b.sentiment ?? "neutral"] ?? 1)
        );
      if (sort === "source") return a.source.localeCompare(b.source);
      return 0;
    });
  }, [data, sentFilter, sourceFilter, sort]);

  const sentPills: { key: SentKey; label: string }[] = [
    { key: "all", label: "All" },
    { key: "bullish", label: "🟢 Bullish" },
    { key: "bearish", label: "🔴 Bearish" },
    { key: "neutral", label: "⚪ Neutral" },
  ];

  return (
    <div>
      {/* Page header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 20,
        }}
      >
        <h1 style={{ color: "var(--muted)", fontSize: 20, fontWeight: 500, margin: 0 }}>
          News — {data?.date ?? "…"}
        </h1>
        <span style={{ color: "var(--muted)", fontSize: 13 }}>
          {loading
            ? "…"
            : `${filtered.length}${filtered.length !== (data?.count ?? 0) ? ` of ${data?.count}` : ""} headlines`}
        </span>
      </div>

      {/* Sticky filter bar */}
      <div
        style={{
          position: "sticky",
          top: 45,
          zIndex: 40,
          backgroundColor: "var(--bg)",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          flexWrap: "wrap",
          gap: 10,
          alignItems: "center",
          padding: "10px 0",
          marginBottom: 16,
        }}
      >
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          style={{
            backgroundColor: "var(--surface)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "4px 8px",
            fontSize: 13,
            cursor: "pointer",
          }}
        >
          <option value="importance">Importance</option>
          <option value="newest">Newest</option>
          <option value="sentiment">Sentiment</option>
          <option value="source">Source</option>
        </select>

        <div style={{ display: "flex", gap: 4 }}>
          {sentPills.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setSentFilter(key)}
              style={{
                fontSize: 12,
                padding: "3px 10px",
                borderRadius: 999,
                border: "1px solid",
                borderColor: sentFilter === key ? "var(--accent)" : "var(--border)",
                backgroundColor:
                  sentFilter === key ? "rgba(124,111,224,0.15)" : "transparent",
                color: sentFilter === key ? "var(--accent)" : "var(--muted)",
                cursor: "pointer",
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          style={{
            backgroundColor: "var(--surface)",
            color: "var(--text)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: "4px 8px",
            fontSize: 13,
            cursor: "pointer",
          }}
        >
          <option value="all">All sources</option>
          {sources.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {/* Content */}
      {loading ? (
        <p style={{ color: "var(--muted)", textAlign: "center", padding: "48px 0" }}>
          Loading…
        </p>
      ) : filtered.length === 0 ? (
        <div style={{ textAlign: "center", padding: "48px 0" }}>
          <p style={{ color: "var(--muted)" }}>No headlines yet today.</p>
          <p style={{ color: "var(--muted)", fontSize: 13, marginTop: 8 }}>
            Headlines appear after the 08:45 IST pipeline run.
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {filtered.map((h) => (
            <HeadlineRow key={h.id} h={h} />
          ))}
        </div>
      )}
    </div>
  );
}

function HeadlineRow({ h }: { h: Headline }) {
  const imp = h.importance ?? 0;
  const sent = (h.sentiment ?? "neutral").toLowerCase();

  return (
    <div
      style={{
        display: "flex",
        alignItems: "stretch",
        backgroundColor: "var(--surface)",
        border: "0.5px solid var(--border)",
        borderRadius: 8,
        overflow: "hidden",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) =>
        ((e.currentTarget as HTMLElement).style.borderColor = "var(--accent)")
      }
      onMouseLeave={(e) =>
        ((e.currentTarget as HTMLElement).style.borderColor = "var(--border)")
      }
    >
      {/* Importance bar + number */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 4,
          padding: "12px 10px",
          minWidth: 36,
        }}
      >
        <div
          style={{
            width: 3,
            height: imp > 0 ? imp * 8 + 4 : 4,
            backgroundColor: impColor(h.importance),
            borderRadius: 2,
          }}
        />
        <span
          style={{ fontSize: 11, color: "var(--muted)", fontVariantNumeric: "tabular-nums" }}
        >
          {imp || "—"}
        </span>
      </div>

      {/* Main */}
      <div style={{ flex: 1, padding: "10px 4px 10px 0", minWidth: 0 }}>
        {h.url ? (
          <a
            href={h.url}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: "var(--text)",
              fontSize: 15,
              lineHeight: 1.4,
              display: "block",
              textDecoration: "none",
            }}
            onMouseEnter={(e) =>
              ((e.currentTarget as HTMLElement).style.textDecoration = "underline")
            }
            onMouseLeave={(e) =>
              ((e.currentTarget as HTMLElement).style.textDecoration = "none")
            }
          >
            {h.headline}
          </a>
        ) : (
          <p style={{ color: "var(--text)", fontSize: 15, lineHeight: 1.4, margin: 0 }}>
            {h.headline}
          </p>
        )}
        {h.reason && (
          <p
            style={{
              color: "var(--muted)",
              fontSize: 12,
              fontStyle: "italic",
              marginTop: 4,
              lineHeight: 1.4,
            }}
          >
            {h.reason}
          </p>
        )}
      </div>

      {/* Right */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          justifyContent: "center",
          gap: 4,
          padding: "10px 12px 10px 8px",
          minWidth: 100,
          flexShrink: 0,
        }}
      >
        <span
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 999,
            backgroundColor: SENT_BG[sent] ?? SENT_BG.neutral,
            color: SENT_COLOR[sent] ?? "var(--muted)",
            fontWeight: 500,
          }}
        >
          {sent}
        </span>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>{h.source}</span>
        {h.scraped_at && (
          <span style={{ fontSize: 11, color: "var(--muted)" }}>
            {timeAgo(h.scraped_at)}
          </span>
        )}
      </div>
    </div>
  );
}
