"use client";

import { useState } from "react";

type Stock = {
  rank: number | null;
  symbol: string;
  sentiment: string;
  setup_type: string;
  thesis: string;
  catalyst_line: string | null;
  direction: string | null;
  score: number;
  gap_pct: number | null;
  gap_source: string | null;
  mention_count: number;
  signals: Record<string, number | string>;
};

// ── display maps ──────────────────────────────────────────────────────────────

const SENT_EMOJI: Record<string, string> = { bullish: "🟢", bearish: "🔴", neutral: "⚪" };

const DIR_COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--neutral)",
};

const DIR_BORDER: Record<string, string> = {
  bullish: "3px solid var(--bullish)",
  bearish: "3px solid var(--bearish)",
  neutral: "1px solid var(--border)",
};

// Human-readable setup labels (new Claude schema + legacy ranker values)
const SETUP_LABEL: Record<string, string> = {
  gap_up_continuation: "Gap Up Cont.",
  gap_fade:            "Gap Fade",
  news_momentum:       "News Momentum",
  sympathy:            "Sympathy",
  gap_play:            "Gap Play",
  other:               "Other",
  // legacy ranker values
  news_catalyst:       "News Catalyst",
  fii_driven:          "FII Driven",
  watchlist:           "Watchlist",
};

const SETUP_COLOR: Record<string, string> = {
  gap_up_continuation: "#1D9E75",   // green family
  gap_fade:            "#E05252",   // red family
  news_momentum:       "#7C6FE0",   // purple (accent)
  sympathy:            "#F97316",   // orange
  gap_play:            "#3B82F6",   // blue
  other:               "#6B7280",   // gray
  // legacy
  news_catalyst:       "#7C6FE0",
  fii_driven:          "#F97316",
  watchlist:           "#6B7280",
};

// Signal component display info for the hover tooltip (matches ranker.py keys)
const SIG_LABELS: { key: string; label: string }[] = [
  { key: "news_mention",  label: "Mentions" },
  { key: "importance",    label: "Importance" },
  { key: "move_score",    label: "Gap" },
  { key: "fii_alignment", label: "FII" },
];

// ── component ─────────────────────────────────────────────────────────────────

export function StockCard({ stock }: { stock: Stock }) {
  const [tipVisible, setTipVisible] = useState(false);

  const dir        = (stock.direction || stock.sentiment || "neutral").toLowerCase();
  const dirColor   = DIR_COLOR[dir]   ?? "var(--neutral)";
  const borderLeft = DIR_BORDER[dir]  ?? "1px solid var(--border)";

  const setupColor = SETUP_COLOR[stock.setup_type] ?? "#6B7280";
  const setupLabel = SETUP_LABEL[stock.setup_type] ?? stock.setup_type.replace(/_/g, " ");

  // Primary display text: catalyst_line preferred over thesis
  const displayText = (stock.catalyst_line || stock.thesis || "").trim();

  const sig = stock.signals ?? {};
  const breakdown = SIG_LABELS.map(
    ({ key, label }) => `${label} ${Number(sig[key] ?? 0).toFixed(2)}`
  ).join("  ·  ");

  const scoreBarPct = Math.min(Math.max(stock.score, 0), 1) * 100;

  return (
    <div
      style={{
        backgroundColor: "var(--surface)",
        border: "1px solid var(--border)",
        borderLeft,
        borderRadius: 10,
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      {/* Header: rank · symbol · emoji · setup pill · direction chip */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {stock.rank != null && (
          <span
            style={{
              backgroundColor: "var(--border)",
              color: "var(--muted)",
              fontSize: 11,
              fontFamily: "monospace",
              padding: "1px 6px",
              borderRadius: 999,
              flexShrink: 0,
            }}
          >
            #{stock.rank}
          </span>
        )}
        <span style={{ color: dirColor, fontSize: 16, fontWeight: 600, flex: 1 }}>
          {stock.symbol}
        </span>
        <span style={{ fontSize: 14 }}>{SENT_EMOJI[stock.sentiment] ?? "⚪"}</span>
        {/* Setup type pill */}
        <span
          style={{
            backgroundColor: setupColor + "28",
            color: setupColor,
            border: `1px solid ${setupColor}44`,
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 999,
            flexShrink: 0,
          }}
        >
          {setupLabel}
        </span>
        {/* Direction chip — only if a real direction is set and non-neutral */}
        {dir !== "neutral" && (
          <span
            style={{
              backgroundColor: dirColor + "22",
              color: dirColor,
              border: `1px solid ${dirColor}44`,
              fontSize: 10,
              padding: "2px 7px",
              borderRadius: 999,
              flexShrink: 0,
              fontWeight: 600,
              textTransform: "capitalize",
            }}
          >
            {dir}
          </span>
        )}
      </div>

      {/* Catalyst / thesis text */}
      <p
        style={{
          color: "var(--muted)",
          fontSize: 13,
          lineHeight: 1.45,
          margin: 0,
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {displayText}
      </p>

      {/* Stats row */}
      <p style={{ color: "var(--muted)", fontSize: 12, margin: 0 }}>
        {stock.gap_pct != null && (
          <>
            <span
              style={{
                color: stock.gap_pct >= 0 ? "var(--bullish)" : "var(--bearish)",
                fontFamily: "monospace",
              }}
            >
              gap {stock.gap_pct >= 0 ? "+" : ""}
              {stock.gap_pct.toFixed(1)}%
            </span>
            <span style={{ color: "var(--border)", margin: "0 6px" }}>·</span>
          </>
        )}
        score {stock.score.toFixed(2)}
        <span style={{ color: "var(--border)", margin: "0 6px" }}>·</span>
        {stock.mention_count} mention{stock.mention_count !== 1 ? "s" : ""}
      </p>

      {/* Single composite score bar with breakdown tooltip on hover/focus */}
      <div
        tabIndex={0}
        role="img"
        aria-label={`Salience score ${stock.score.toFixed(2)}. ${breakdown}`}
        style={{ position: "relative", cursor: "default", outline: "none" }}
        onMouseEnter={() => setTipVisible(true)}
        onMouseLeave={() => setTipVisible(false)}
        onFocus={() => setTipVisible(true)}
        onBlur={() => setTipVisible(false)}
      >
        {/* Bar track */}
        <div
          style={{
            height: 4,
            borderRadius: 2,
            backgroundColor: "var(--border)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: 4,
              width: `${scoreBarPct}%`,
              backgroundColor: "var(--accent)",
              borderRadius: 2,
              transition: "width 0.3s ease",
            }}
          />
        </div>

        {/* Breakdown tooltip */}
        {tipVisible && (
          <div
            style={{
              position: "absolute",
              bottom: "calc(100% + 6px)",
              left: 0,
              backgroundColor: "#12121a",
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "5px 10px",
              fontSize: 11,
              color: "var(--muted)",
              whiteSpace: "nowrap",
              zIndex: 20,
              pointerEvents: "none",
            }}
          >
            {breakdown}
          </div>
        )}
      </div>
    </div>
  );
}
