"use client";

import { useEffect, useMemo, useState } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

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

type SortKey = "importance" | "newest" | "sentiment" | "source";
type SentKey = "all" | "bullish" | "bearish" | "neutral";

type Theme = {
  bg: string;
  surface: string;
  card: string;
  border: string;
  text: string;
  muted: string;
  accent: string;
  inputBg: string;
};

// ── Themes ────────────────────────────────────────────────────────────────────

const DARK: Theme = {
  bg:      "#0A0A0F",
  surface: "#13131A",
  card:    "#16161F",
  border:  "#1E1E2E",
  text:    "#E8E6F0",
  muted:   "#6B6880",
  accent:  "#7C6FE0",
  inputBg: "#0F0F18",
};

const LIGHT: Theme = {
  bg:      "#F0F0F5",
  surface: "#FAFAFA",
  card:    "#FFFFFF",
  border:  "#DCDCE8",
  text:    "#1A1826",
  muted:   "#7878A0",
  accent:  "#6355D0",
  inputBg: "#F5F5FA",
};

// ── Sentiment palette ─────────────────────────────────────────────────────────

const SENT: Record<string, { border: string; bg: string; text: string; label: string }> = {
  bullish: { border: "#1D9E75", bg: "rgba(29,158,117,0.10)",  text: "#1D9E75", label: "Bullish" },
  bearish: { border: "#E05252", bg: "rgba(224,82,82,0.10)",   text: "#E05252", label: "Bearish" },
  neutral: { border: "#6B6880", bg: "rgba(107,104,128,0.08)", text: "#8886A0", label: "Neutral" },
};

const sentKey = (s: string | null) => (s ?? "neutral").toLowerCase();
const sentOf  = (s: string | null) => SENT[sentKey(s)] ?? SENT.neutral;

// ── Helpers ───────────────────────────────────────────────────────────────────

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

const SORT_SENT: Record<string, number> = { bullish: 0, bearish: 1, neutral: 2 };

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (mins < 1)  return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ── ImportanceBars ────────────────────────────────────────────────────────────

function ImportanceBars({ value, t }: { value: number | null; t: Theme }) {
  const imp = value ?? 0;
  return (
    <div style={{ display: "flex", gap: 2, alignItems: "flex-end" }}>
      {[1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          style={{
            width: 3,
            height: 4 + i * 2,
            borderRadius: 1.5,
            backgroundColor: i <= imp
              ? `rgba(124,111,224,${0.35 + (imp / 5) * 0.65})`
              : t.border,
          }}
        />
      ))}
    </div>
  );
}

// ── HeadlineCard ──────────────────────────────────────────────────────────────

function HeadlineCard({ h, t }: { h: Headline; t: Theme }) {
  const [hovered, setHovered] = useState(false);
  const sp = sentOf(h.sentiment);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        backgroundColor: t.card,
        border: `1px solid ${hovered ? t.accent : t.border}`,
        borderLeft: `4px solid ${sp.border}`,
        borderRadius: 8,
        padding: "12px 14px 10px",
        display: "flex",
        flexDirection: "column",
        gap: 7,
        boxSizing: "border-box",
      }}
    >
      {/* Source + importance bars */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span style={{
          fontSize: 11, fontWeight: 700, color: t.muted,
          textTransform: "uppercase", letterSpacing: "0.06em",
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "70%",
        }}>
          {h.source}
        </span>
        <ImportanceBars value={h.importance} t={t} />
      </div>

      {/* Headline */}
      {h.url ? (
        <a
          href={h.url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            color: hovered ? t.accent : t.text,
            fontSize: 14, fontWeight: 500, lineHeight: 1.45,
            textDecoration: "none", display: "block",
          }}
        >
          {h.headline}
        </a>
      ) : (
        <p style={{ color: t.text, fontSize: 14, fontWeight: 500, lineHeight: 1.45, margin: 0 }}>
          {h.headline}
        </p>
      )}

      {/* Claude reason */}
      {h.reason && (
        <p style={{ color: t.muted, fontSize: 12, lineHeight: 1.4, fontStyle: "italic", margin: 0 }}>
          {h.reason}
        </p>
      )}

      {/* Sentiment badge + time */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 2 }}>
        <span style={{
          fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999,
          backgroundColor: sp.bg, color: sp.text,
        }}>
          {sp.label}
        </span>
        {h.scraped_at && (
          <span style={{ fontSize: 11, color: t.muted }}>{timeAgo(h.scraped_at)}</span>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function NewsPage() {
  // Store headlines and metadata as separate state to avoid reading through
  // a nullable wrapper object — explicit extraction from json.headlines.
  const [headlines,    setHeadlines]    = useState<Headline[]>([]);
  const [newsDate,     setNewsDate]     = useState<string>("");
  const [loading,      setLoading]      = useState(true);
  const [fetchedCount, setFetchedCount] = useState(0);   // raw count from API
  const [sort,         setSort]         = useState<SortKey>("importance");
  const [sentFilter,   setSentFilter]   = useState<SentKey>("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [isDark,       setIsDark]       = useState(true);

  const t = isDark ? DARK : LIGHT;

  useEffect(() => {
    fetch(`${API}/api/news/today`)
      .then((r) => r.json())
      .then((json) => {
        // Explicitly read json.headlines — never treat the wrapper object as the array.
        const hs: Headline[] = Array.isArray(json.headlines) ? json.headlines : [];
        setHeadlines(hs);
        setFetchedCount(typeof json.count === "number" ? json.count : hs.length);
        setNewsDate(typeof json.date === "string" ? json.date : "");
      })
      .catch(() => {
        setHeadlines([]);
        setFetchedCount(0);
      })
      .finally(() => setLoading(false));
  }, []);

  const sources = useMemo(
    () => Array.from(new Set(headlines.map((h) => h.source))).sort(),
    [headlines]
  );

  const filtered = useMemo(() => {
    let hs = headlines;
    // Case-insensitive sentiment match — DB may store "Bullish" or "bullish".
    if (sentFilter !== "all")
      hs = hs.filter((h) => sentKey(h.sentiment) === sentFilter);
    if (sourceFilter !== "all")
      hs = hs.filter((h) => h.source === sourceFilter);
    return [...hs].sort((a, b) => {
      if (sort === "importance") {
        const d = (b.importance ?? 0) - (a.importance ?? 0);
        return d !== 0 ? d : (b.scraped_at ?? "") > (a.scraped_at ?? "") ? -1 : 1;
      }
      if (sort === "newest")
        return (b.scraped_at ?? "") > (a.scraped_at ?? "") ? -1 : 1;
      if (sort === "sentiment")
        return (SORT_SENT[sentKey(a.sentiment)] ?? 2) - (SORT_SENT[sentKey(b.sentiment)] ?? 2);
      if (sort === "source")
        return a.source.localeCompare(b.source);
      return 0;
    });
  }, [headlines, sentFilter, sourceFilter, sort]);

  const sentPills: { key: SentKey; label: string }[] = [
    { key: "all",     label: "All" },
    { key: "bullish", label: "▲ Bullish" },
    { key: "bearish", label: "▼ Bearish" },
    { key: "neutral", label: "● Neutral" },
  ];

  const inputStyle = {
    backgroundColor: t.inputBg,
    color:           t.text,
    border:          `1px solid ${t.border}`,
    borderRadius:    6,
    padding:         "4px 10px",
    fontSize:        12,
    cursor:          "pointer",
    outline:         "none",
  } as const;

  const pillStyle = (active: boolean) =>
    ({
      fontSize:        12,
      padding:         "4px 12px",
      borderRadius:    999,
      border:          `1px solid ${active ? t.accent : t.border}`,
      backgroundColor: active ? "rgba(124,111,224,0.15)" : "transparent",
      color:           active ? t.accent : t.muted,
      cursor:          "pointer",
      fontWeight:      (active ? 600 : 400) as number,
    } as const);

  const filtersActive = sentFilter !== "all" || sourceFilter !== "all";

  return (
    <div style={{ backgroundColor: t.bg, minHeight: "100vh", padding: "0 0 40px" }}>
      <style>{`
        .news-grid { display: grid; grid-template-columns: repeat(2,1fr); gap: 10px; }
        @media (max-width: 720px) { .news-grid { grid-template-columns: 1fr; } }
      `}</style>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16, flexWrap: "wrap", gap: 8 }}>
        <div>
          <h1 style={{ color: t.text, fontSize: 18, fontWeight: 600, margin: 0 }}>News</h1>
          <span style={{ color: t.muted, fontSize: 12 }}>
            {newsDate || "…"}&nbsp;·&nbsp;
            {loading
              ? "loading…"
              : `${filtered.length}${filtered.length !== fetchedCount ? ` of ${fetchedCount}` : ""} headlines`}
          </span>
        </div>
        <button
          onClick={() => setIsDark((d) => !d)}
          style={{
            backgroundColor: t.inputBg, border: `1px solid ${t.border}`,
            borderRadius: 20, padding: "5px 14px",
            color: t.muted, fontSize: 12, cursor: "pointer",
          }}
        >
          {isDark ? "☀ Light" : "☾ Dark"}
        </button>
      </div>

      {/* Filter bar */}
      <div style={{
        position: "sticky", top: 0, zIndex: 40,
        backgroundColor: t.bg, borderBottom: `1px solid ${t.border}`,
        display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center",
        padding: "10px 0", marginBottom: 16,
      }}>
        <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} style={inputStyle}>
          <option value="importance">↕ Importance</option>
          <option value="newest">⏱ Newest</option>
          <option value="sentiment">● Sentiment</option>
          <option value="source">A Source</option>
        </select>

        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          {sentPills.map(({ key, label }) => (
            <button key={key} onClick={() => setSentFilter(key)} style={pillStyle(sentFilter === key)}>
              {label}
            </button>
          ))}
        </div>

        <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} style={inputStyle}>
          <option value="all">All sources</option>
          {sources.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      {/* Content */}
      {loading ? (
        <div style={{ textAlign: "center", padding: "64px 0" }}>
          <div style={{
            width: 28, height: 28,
            border: `3px solid ${t.border}`,
            borderTop: `3px solid ${t.accent}`,
            borderRadius: "50%", margin: "0 auto 12px",
            animation: "spin 0.8s linear infinite",
          }} />
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
          <p style={{ color: t.muted, fontSize: 13 }}>Loading headlines…</p>
        </div>
      ) : filtered.length === 0 ? (
        <div style={{ textAlign: "center", padding: "64px 0" }}>
          <p style={{ color: t.muted, fontSize: 16, fontWeight: 500 }}>No headlines found.</p>
          <p style={{ color: t.muted, fontSize: 13, marginTop: 6 }}>
            {filtersActive
              ? "Try adjusting the filters above."
              : "Headlines appear after the 08:45 IST pipeline run."}
          </p>
          {filtersActive && (
            <button
              onClick={() => { setSentFilter("all"); setSourceFilter("all"); }}
              style={{
                marginTop: 12, fontSize: 12, padding: "5px 14px",
                borderRadius: 6, border: `1px solid ${t.border}`,
                backgroundColor: "transparent", color: t.accent, cursor: "pointer",
              }}
            >
              Clear filters
            </button>
          )}
        </div>
      ) : (
        <div className="news-grid">
          {filtered.map((h) => (
            <HeadlineCard key={h.id} h={h} t={t} />
          ))}
        </div>
      )}
    </div>
  );
}
