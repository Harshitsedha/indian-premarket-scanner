/**
 * Next.js route handler: proxies /api/market-snapshot to the FastAPI backend.
 * Client-side polling uses this same-origin route to avoid CORS issues in production.
 */
import { NextResponse } from "next/server";

const BACKEND = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8001";

export async function GET() {
  try {
    const res = await fetch(`${BACKEND}/api/market-snapshot`, {
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`Upstream ${res.status}`);
    const data = await res.json();
    return NextResponse.json(data, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (err) {
    return NextResponse.json(
      { tiles: [], error: String(err) },
      { status: 502, headers: { "Cache-Control": "no-store" } }
    );
  }
}
