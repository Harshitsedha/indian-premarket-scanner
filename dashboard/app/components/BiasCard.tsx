type BiasData = {
  direction: string;
  confidence: number | null;
  score: number;
  reason: string;
  fii_dii: string;
  global_summary: string | null;
};

const EMOJI: Record<string, string> = { bullish: "🟢", bearish: "🔴", neutral: "⚪" };
const COLOR: Record<string, string> = {
  bullish: "var(--bullish)",
  bearish: "var(--bearish)",
  neutral: "var(--neutral)",
};

export function BiasCard({
  bias,
  date,
  generatedAt,
}: {
  bias: BiasData;
  date: string;
  generatedAt: string | null;
}) {
  const dir = (bias.direction || "neutral").toLowerCase();
  const color = COLOR[dir] ?? "var(--neutral)";
  const emoji = EMOJI[dir] ?? "⚪";

  // Score bar: -1 → +1 mapped to 0% → 100%, midpoint at 50%
  const scorePct = ((bias.score + 1) / 2) * 100;
  const isPos = bias.score >= 0;

  return (
    <div
      style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
      className="rounded-xl p-6 space-y-4"
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <p style={{ color: "var(--muted)" }} className="text-xs mb-1">
            {date}
          </p>
          <h2
            style={{ color }}
            className="text-3xl font-bold tracking-tight"
          >
            {emoji} {dir.toUpperCase()}
          </h2>
        </div>
        {/* Confidence dots */}
        {bias.confidence != null && (
          <div className="flex gap-1 mt-1 pt-1">
            {Array.from({ length: 5 }).map((_, i) => (
              <div
                key={i}
                style={{
                  backgroundColor:
                    i < bias.confidence! ? color : "var(--border)",
                  width: 10,
                  height: 10,
                }}
                className="rounded-full"
              />
            ))}
          </div>
        )}
      </div>

      {/* Score bar */}
      <div>
        <p style={{ color: "var(--muted)" }} className="text-xs mb-1">
          Score: {bias.score > 0 ? "+" : ""}{bias.score.toFixed(3)}
        </p>
        <div style={{ backgroundColor: "var(--border)" }} className="h-2 rounded-full relative">
          {/* Center marker */}
          <div
            style={{ left: "50%", backgroundColor: "var(--border)" }}
            className="absolute top-0 h-2 w-px"
          />
          <div
            style={{
              backgroundColor: isPos ? "var(--bullish)" : "var(--bearish)",
              left: isPos ? "50%" : `${scorePct}%`,
              width: `${Math.abs(scorePct - 50)}%`,
            }}
            className="absolute h-2 rounded-full"
          />
        </div>
      </div>

      {/* Reason */}
      <p style={{ color: "var(--muted)" }} className="text-sm leading-relaxed">
        {bias.reason}
      </p>

      {/* FII/DII + global */}
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {bias.fii_dii && (
          <span style={{ color: "var(--text)" }}>💰 {bias.fii_dii}</span>
        )}
        {bias.global_summary && (
          <span style={{ color: "var(--text)" }}>🌍 {bias.global_summary}</span>
        )}
      </div>

      {/* Timestamp */}
      {generatedAt && (
        <p style={{ color: "var(--muted)" }} className="text-xs text-right">
          Generated {new Date(generatedAt).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} IST
        </p>
      )}
    </div>
  );
}
