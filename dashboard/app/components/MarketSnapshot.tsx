"use client";

import { useEffect, useState } from "react";

type SnapTile = {
  label: string;
  category: string;
  last: number | null;
  change_pct: number | null;
  stale: boolean;
};

function Tile({ tile }: { tile: SnapTile }) {
  const up = (tile.change_pct ?? 0) >= 0;
  const color = tile.stale
    ? "var(--muted)"
    : up
    ? "var(--bullish)"
    : "var(--bearish)";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-start",
        backgroundColor: "var(--surface)",
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: "5px 10px",
        minWidth: 90,
        flexShrink: 0,
        gap: 1,
      }}
    >
      <span style={{ fontSize: 10, color: "var(--muted)", fontWeight: 500, letterSpacing: "0.03em" }}>
        {tile.label}
      </span>
      {tile.stale || tile.last == null ? (
        <span style={{ fontSize: 12, color: "var(--muted)", fontFamily: "monospace" }}>—</span>
      ) : (
        <>
          <span style={{ fontSize: 12, color, fontFamily: "monospace", fontWeight: 600 }}>
            {tile.last.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
          </span>
          <span style={{ fontSize: 10, color, fontFamily: "monospace" }}>
            {tile.change_pct! >= 0 ? "+" : ""}
            {tile.change_pct!.toFixed(2)}%
          </span>
        </>
      )}
    </div>
  );
}

export function MarketSnapshot({ initial }: { initial: SnapTile[] }) {
  const [tiles, setTiles] = useState<SnapTile[]>(initial);

  useEffect(() => {
    const refresh = async () => {
      try {
        const res = await fetch("/api/market-snapshot");
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data.tiles) && data.tiles.length > 0) {
            setTiles(data.tiles);
          }
        }
      } catch {
        /* keep showing last known data */
      }
    };

    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, []);

  if (tiles.length === 0) return null;

  return (
    <div
      style={{
        overflowX: "auto",
        paddingBottom: 2,
        WebkitOverflowScrolling: "touch",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 6,
          width: "max-content",
        }}
      >
        {tiles.map((t) => (
          <Tile key={t.label} tile={t} />
        ))}
      </div>
    </div>
  );
}
