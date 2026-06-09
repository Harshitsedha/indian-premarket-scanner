import { Suspense } from "react";
import { RadarTable, type RadarResponse } from "../components/RadarTable";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

export default async function RadarPage() {
  let initial: RadarResponse = {
    snapshot: null,
    stale: false,
    market_open: false,
    status: {},
  };

  try {
    const res = await fetch(`${API}/api/radar`, { cache: "no-store" });
    if (res.ok) initial = await res.json();
  } catch {
    /* API unreachable — show empty state via initial defaults */
  }

  return (
    <Suspense>
      <RadarTable initial={initial} />
    </Suspense>
  );
}
