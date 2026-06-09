import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "PreMarket Pro",
  description: "Indian pre-market briefing dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full`}>
      <body className="min-h-full flex flex-col antialiased">
        <nav
          style={{ backgroundColor: "var(--surface)", borderBottom: "1px solid var(--border)" }}
          className="sticky top-0 z-50 px-6 py-3 flex items-center gap-6 text-sm"
        >
          <span style={{ color: "var(--text)" }} className="font-semibold text-base mr-4">
            🔔 PreMarket
          </span>
          {[
            { href: "/",          label: "Today"    },
            { href: "/news",      label: "News"     },
            // TEMP: disabled, re-enable later
            // { href: "/history",   label: "History"  },
            // { href: "/edge",      label: "Edge"     },
            { href: "/backtest",  label: "Backtest" },
            { href: "/radar",     label: "Radar"    },
          ].map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              style={{ color: "var(--muted)" }}
              className="hover:text-white transition-colors"
            >
              {label}
            </Link>
          ))}
        </nav>
        <main className="flex-1 w-full mx-auto px-6 py-6" style={{ maxWidth: 1100 }}>
          {children}
        </main>
      </body>
    </html>
  );
}
