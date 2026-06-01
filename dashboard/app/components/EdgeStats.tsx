"use client";

import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

type SetupStat = {
  count: number;
  hypothesis_correct_rate: number | null;
  avg_move_pct: number | null;
  avg_score: number | null;
};

type Stats = {
  total_setups: number;
  with_outcomes: number;
  by_setup_type: Record<string, SetupStat>;
  by_bias: Record<string, { count: number; hypothesis_correct_rate: number | null }>;
  top_symbols: string[];
};

const SETUP_COLORS: Record<string, string> = {
  news_catalyst: "#7C6FE0",
  gap_play:      "#3B82F6",
  fii_driven:    "#F97316",
  watchlist:     "#6B7280",
};

export function EdgeStats({ stats }: { stats: Stats }) {
  const setupTypes = ["news_catalyst", "gap_play", "fii_driven", "watchlist"];

  const hitRateData = setupTypes.map((st) => ({
    name: st.replace(/_/g, " "),
    rate: stats.by_setup_type[st]?.hypothesis_correct_rate ?? 0,
    color: SETUP_COLORS[st],
  }));

  const moveData = setupTypes.map((st) => ({
    name: st.replace(/_/g, " "),
    move: stats.by_setup_type[st]?.avg_move_pct ?? 0,
    color: SETUP_COLORS[st],
  }));

  return (
    <div className="space-y-8">
      {/* Summary row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {setupTypes.map((st) => {
          const s = stats.by_setup_type[st];
          return (
            <div
              key={st}
              style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
              className="rounded-xl p-4 space-y-1"
            >
              <p style={{ color: SETUP_COLORS[st] }} className="text-xs font-medium capitalize">
                {st.replace(/_/g, " ")}
              </p>
              <p className="text-2xl font-bold">{s?.count ?? 0}</p>
              <p style={{ color: "var(--muted)" }} className="text-xs">setups</p>
              <p style={{ color: "var(--bullish)" }} className="text-sm font-mono">
                {s?.hypothesis_correct_rate != null
                  ? `${s.hypothesis_correct_rate.toFixed(0)}% hit`
                  : "no data"}
              </p>
              <p style={{ color: "var(--muted)" }} className="text-xs font-mono">
                {s?.avg_move_pct != null
                  ? `avg ${s.avg_move_pct >= 0 ? "+" : ""}${s.avg_move_pct.toFixed(1)}%`
                  : ""}
              </p>
            </div>
          );
        })}
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Hit rate chart */}
        <div
          style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
          className="rounded-xl p-4"
        >
          <p style={{ color: "var(--muted)" }} className="text-xs mb-4">Hit rate % by setup type</p>
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={hitRateData} margin={{ left: -10 }}>
              <XAxis dataKey="name" tick={{ fill: "#6B6880", fontSize: 10 }} />
              <YAxis domain={[0, 100]} tick={{ fill: "#6B6880", fontSize: 10 }} />
              <Tooltip
                contentStyle={{ backgroundColor: "#13131A", border: "1px solid #1E1E2E", borderRadius: 6 }}
                labelStyle={{ color: "#E8E6F0" }}
              />
              <Bar dataKey="rate" radius={[3, 3, 0, 0]}>
                {hitRateData.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Avg move chart */}
        <div
          style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
          className="rounded-xl p-4"
        >
          <p style={{ color: "var(--muted)" }} className="text-xs mb-4">Avg move % by setup type</p>
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={moveData} margin={{ left: -10 }}>
              <XAxis dataKey="name" tick={{ fill: "#6B6880", fontSize: 10 }} />
              <YAxis tick={{ fill: "#6B6880", fontSize: 10 }} />
              <Tooltip
                contentStyle={{ backgroundColor: "#13131A", border: "1px solid #1E1E2E", borderRadius: 6 }}
                labelStyle={{ color: "#E8E6F0" }}
              />
              <Bar dataKey="move" radius={[3, 3, 0, 0]}>
                {moveData.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Bias performance table */}
      <div
        style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
        className="rounded-xl overflow-hidden"
      >
        <p style={{ color: "var(--muted)", borderBottom: "1px solid var(--border)" }}
           className="text-xs px-4 py-2">
          Hit rate by market bias
        </p>
        {["bullish", "bearish"].map((dir) => {
          const b = stats.by_bias[dir];
          const color = dir === "bullish" ? "var(--bullish)" : "var(--bearish)";
          return (
            <div
              key={dir}
              style={{ borderBottom: "1px solid var(--border)" }}
              className="grid grid-cols-3 px-4 py-3 text-sm"
            >
              <span style={{ color }} className="capitalize font-medium">{dir}</span>
              <span style={{ color: "var(--muted)" }}>{b?.count ?? 0} setups</span>
              <span className="font-mono">
                {b?.hypothesis_correct_rate != null
                  ? `${b.hypothesis_correct_rate.toFixed(0)}%`
                  : "—"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
