import { HistoryTable } from "../components/HistoryTable";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

export default async function HistoryPage() {
  let rows: unknown[] = [];
  try {
    const res = await fetch(`${API}/api/briefing/history?days=30`, { cache: "no-store" });
    if (res.ok) rows = await res.json();
  } catch {
    /* API unreachable */
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold" style={{ color: "var(--muted)" }}>
          Briefing History
        </h1>
        <span style={{ color: "var(--muted)" }} className="text-sm">
          Last 30 days · click a row to expand setups
        </span>
      </div>
      <HistoryTable rows={rows as Parameters<typeof HistoryTable>[0]["rows"]} />
    </div>
  );
}
