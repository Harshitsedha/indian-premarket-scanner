import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // TEMP: disabled, re-enable later — remove these two redirect entries
  async redirects() {
    return [
      { source: "/history", destination: "/", permanent: false },
      { source: "/edge",    destination: "/", permanent: false },
    ];
  },

  // In local dev, proxy /api/* to FastAPI on 8001 so relative fetch URLs work.
  // In production, nginx handles /api/ → 127.0.0.1:8001 — these rewrites are skipped.
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8001/api/:path*",
      },
    ];
  },
};

export default nextConfig;
