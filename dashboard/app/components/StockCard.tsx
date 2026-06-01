type Stock = {
  rank: number | null;
  symbol: string;
  sentiment: string;
  setup_type: string;
  thesis: string;
  score: number;
  gap_pct: number | null;
  gap_source: string | null;
  mention_count: number;
  signals: Record<string, number | string>;
};

const SENT_EMOJI: Record<string, string> = { bullish: "🟢", bearish: "🔴", neutral: "⚪" };
const SENT_COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--neutral)",
};
const SETUP_COLOR: Record<string, string> = {
  news_catalyst: "#7C6FE0",
  gap_play:      "#3B82F6",
  fii_driven:    "#F97316",
  watchlist:     "#6B7280",
};

const SIGNALS = [
  { key: "news_mention",  label: "M" },
  { key: "importance",    label: "I" },
  { key: "gap_potential", label: "G" },
  { key: "fii_alignment", label: "F" },
] as const;

export function StockCard({ stock }: { stock: Stock }) {
  const sentColor = SENT_COLOR[stock.sentiment] ?? "var(--neutral)";
  const setupColor = SETUP_COLOR[stock.setup_type] ?? "#6B7280";

  return (
    <div
      style={{
        backgroundColor: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 10,
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      {/* Header: rank · symbol · emoji · setup pill */}
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
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
        <span style={{ color: sentColor, fontSize: 16, fontWeight: 600, flex: 1 }}>
          {stock.symbol}
        </span>
        <span style={{ fontSize: 14 }}>{SENT_EMOJI[stock.sentiment] ?? "⚪"}</span>
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
          {stock.setup_type.replace(/_/g, " ")}
        </span>
      </div>

      {/* Thesis */}
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
        {stock.thesis}
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

      {/* Signal bars: M / I / G / F */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 6 }}>
        {SIGNALS.map(({ key, label }) => {
          const val = Math.min(Math.max(Number(stock.signals?.[key] ?? 0), 0), 1);
          return (
            <div key={key} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <div
                style={{
                  height: 2,
                  borderRadius: 1,
                  backgroundColor: "var(--border)",
                  position: "relative",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    height: 2,
                    width: `${val * 100}%`,
                    backgroundColor: "var(--accent)",
                    borderRadius: 1,
                  }}
                />
              </div>
              <span
                style={{ fontSize: 10, color: "var(--muted)", textAlign: "center" }}
              >
                {label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
