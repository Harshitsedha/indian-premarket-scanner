import { BiasCard } from "./components/BiasCard";
import { StockCard } from "./components/StockCard";
import { TopNews, type TopHeadline } from "./components/TopNews";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

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

export default async function TodayPage() {
  let data: {
    date: string;
    bias: null | Record<string, unknown>;
    stocks: Stock[];
    generated_at: string | null;
  } = {
    date: new Date().toISOString().slice(0, 10),
    bias: null,
    stocks: [],
    generated_at: null,
  };

  let topHeadlines: TopHeadline[] = [];

  try {
    const [briefingRes, newsRes] = await Promise.all([
      fetch(`${API}/api/briefing/today`, { cache: "no-store" }),
      fetch(`${API}/api/news/today`, { cache: "no-store" }),
    ]);
    if (briefingRes.ok) data = await briefingRes.json();
    if (newsRes.ok) {
      const newsData = await newsRes.json();
      topHeadlines = (newsData.headlines ?? []).slice(0, 3);
    }
  } catch {
    /* API unreachable — show empty state */
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <h1 style={{ color: "var(--muted)", fontSize: 20, fontWeight: 500, marginBottom: 8 }}>
        Today&apos;s Pre-Market Briefing
      </h1>

      {data.bias ? (
        <BiasCard
          bias={data.bias as Parameters<typeof BiasCard>[0]["bias"]}
          date={data.date}
          generatedAt={data.generated_at}
        />
      ) : (
        <div
          style={{
            backgroundColor: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 12,
            padding: 24,
            textAlign: "center",
          }}
        >
          <p style={{ color: "var(--muted)" }}>
            No briefing generated yet today. Run the pipeline at 08:45 IST.
          </p>
        </div>
      )}

      <TopNews headlines={topHeadlines} />

      {data.stocks.length > 0 && (
        <>
          <p style={{ color: "var(--muted)", fontSize: 13, fontWeight: 500, marginTop: 4 }}>
            Stocks in Play — {data.stocks.length} ranked
          </p>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(480px, 1fr))",
              gap: 12,
            }}
          >
            {data.stocks.map((stock) => (
              <StockCard key={stock.symbol} stock={stock} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
