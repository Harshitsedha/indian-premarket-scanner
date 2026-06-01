import Link from "next/link";

export type TopHeadline = {
  id: number;
  source: string;
  headline: string;
  url: string | null;
  sentiment: string | null;
  importance: number | null;
};

const SENT_EMOJI: Record<string, string> = { bullish: "🟢", bearish: "🔴", neutral: "⚪" };
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

export function TopNews({ headlines }: { headlines: TopHeadline[] }) {
  return (
    <div
      style={{
        backgroundColor: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 12,
        padding: 16,
      }}
    >
      {/* Section header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 12,
        }}
      >
        <span
          style={{
            fontSize: 11,
            color: "var(--muted)",
            fontWeight: 600,
            letterSpacing: "0.06em",
            textTransform: "uppercase",
          }}
        >
          Top stories
        </span>
        <Link
          href="/news"
          style={{ fontSize: 12, color: "var(--accent)", textDecoration: "none" }}
        >
          see all →
        </Link>
      </div>

      {headlines.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          No headlines yet — run pipeline at 08:45 IST.
        </p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {headlines.map((h) => {
            const sent = (h.sentiment ?? "neutral").toLowerCase();
            return (
              <div
                key={h.id}
                style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}
              >
                <span style={{ fontSize: 10, flexShrink: 0 }}>
                  {SENT_EMOJI[sent] ?? "⚪"}
                </span>
                {h.url ? (
                  <a
                    href={h.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{
                      flex: 1,
                      fontSize: 13,
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
                      fontSize: 13,
                      color: "var(--text)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {h.headline}
                  </span>
                )}
                <span
                  style={{
                    fontSize: 11,
                    padding: "2px 7px",
                    borderRadius: 999,
                    backgroundColor: SENT_BG[sent] ?? SENT_BG.neutral,
                    color: SENT_COLOR[sent] ?? "var(--muted)",
                    flexShrink: 0,
                    fontWeight: 500,
                  }}
                >
                  {sent}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
