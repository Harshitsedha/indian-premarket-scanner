import { EdgeStats } from "../components/EdgeStats";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

export default async function EdgePage() {
  let stats: Record<string, unknown> | null = null;
  try {
    const res = await fetch(`${API}/api/edge/summary?days=90`, { cache: "no-store" });
    if (res.ok) stats = await res.json();
  } catch {
    /* API unreachable */
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold" style={{ color: "var(--muted)" }}>
          Edge Analysis
        </h1>
        <span style={{ color: "var(--muted)" }} className="text-sm">
          Last 90 days · updated weekly on Fridays
        </span>
      </div>

      {stats ? (
        <>
          <div className="flex gap-6 text-sm" style={{ color: "var(--muted)" }}>
            <span>Total setups: <strong style={{ color: "var(--text)" }}>{stats.total_setups as number}</strong></span>
            <span>With outcomes: <strong style={{ color: "var(--text)" }}>{stats.with_outcomes as number}</strong></span>
          </div>
          <EdgeStats stats={stats as Parameters<typeof EdgeStats>[0]["stats"]} />
        </>
      ) : (
        <div
          style={{ backgroundColor: "var(--surface)", border: "1px solid var(--border)" }}
          className="rounded-xl p-8 text-center"
        >
          <p style={{ color: "var(--muted)" }}>
            No edge data yet. Log setups and outcomes for a few weeks, then the Friday report will populate this page.
          </p>
        </div>
      )}
    </div>
  );
}
