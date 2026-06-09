import { NextResponse } from "next/server";

const BACKEND = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8001";

export async function GET() {
  try {
    const res = await fetch(`${BACKEND}/api/radar`, { cache: "no-store" });
    if (!res.ok) {
      return NextResponse.json(
        { snapshot: null, stale: true, market_open: false, status: {} },
        { status: 200 }
      );
    }
    const data = await res.json();
    return NextResponse.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({
      snapshot: null,
      stale: true,
      market_open: false,
      status: {},
    });
  }
}
