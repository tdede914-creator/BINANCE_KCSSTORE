"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { useEventStream } from "@/lib/ws";

const links = [
  { href: "/", label: "Dashboard" },
  { href: "/signals", label: "Signals" },
  { href: "/history", label: "History" },
  { href: "/backtest", label: "Backtest" },
  { href: "/settings", label: "Settings" },
];

export function NavBar() {
  const pathname = usePathname();
  const { connected } = useEventStream();

  return (
    <nav className="sticky top-0 z-40 bg-bg-soft border-b border-border">
      <div className="max-w-7xl mx-auto px-4 py-3 flex items-center gap-6">
        <div className="flex items-center gap-2">
          <span className="text-lg font-semibold tracking-tight">KCS</span>
          <span className="text-xs text-muted hidden sm:inline">
            Futures Signal Bot
          </span>
        </div>

        <div className="flex gap-1 flex-1">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={clsx(
                "px-3 py-1.5 rounded text-sm transition-colors",
                pathname === l.href
                  ? "bg-bg-card text-white"
                  : "text-muted hover:text-white hover:bg-bg-card",
              )}
            >
              {l.label}
            </Link>
          ))}
        </div>

        <div className="flex items-center gap-2 text-xs">
          <span
            className={clsx(
              "inline-block w-2 h-2 rounded-full",
              connected ? "bg-long animate-pulse" : "bg-short",
            )}
          />
          <span className="text-muted">
            {connected ? "Live" : "Disconnected"}
          </span>
        </div>
      </div>
    </nav>
  );
}
