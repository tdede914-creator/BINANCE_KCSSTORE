"use client";

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { ScannerDiagnostics, SymbolDiag } from "@/lib/api";

/**
 * Small "why aren't we getting signals" panel.
 *
 * Polls /api/scanner/diagnostics every 5 s and shows, per watchlist
 * symbol, how far it made it through the strategy gates this tick and
 * the human-readable reason it stopped. Helps the user tell the
 * difference between "the bot is broken" and "the market doesn't
 * currently qualify".
 */
export function ScannerPanel() {
  const [diag, setDiag] = useState<ScannerDiagnostics | null>(null);
  const [open, setOpen] = useState(true);

  const refresh = useCallback(async () => {
    try {
      setDiag(await api.getScannerDiagnostics());
    } catch {
      /* backend hasn't scanned yet — leave stale */
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const stageCounts = countStages(diag?.symbols ?? []);
  const lastTick = diag?.last_tick_ts ? new Date(diag.last_tick_ts) : null;

  return (
    <section className="bg-bg-card border border-border rounded-lg">
      <header
        className="p-4 flex items-center justify-between cursor-pointer select-none"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">Scanner status</h2>
          {lastTick && (
            <span className="text-xs text-muted font-mono">
              last tick {formatAgo(lastTick)} · market{" "}
              {(diag?.last_tick_market ?? "—").toUpperCase()}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs font-mono flex-wrap justify-end">
          <StagePill label="executed" count={stageCounts.executed} color="long" />
          <StagePill
            label="rejected"
            count={stageCounts.risk_rejected + stageCounts.exec_failed}
            color="short"
          />
          <StagePill label="trigger" count={stageCounts.trigger} color="yellow" />
          <StagePill label="setup" count={stageCounts.setup} color="blue" />
          <StagePill label="bias" count={stageCounts.bias} color="muted" />
          <StagePill label="warmup" count={stageCounts.warmup} color="muted" />
          <span className="text-muted">{open ? "▾" : "▸"}</span>
        </div>
      </header>

      {open && (
        <div className="border-t border-border overflow-x-auto">
          {diag?.reconcile_error && (
            <div className="m-3 p-3 rounded border border-short/40 bg-short/10 text-xs">
              <div className="font-semibold text-short mb-1">
                ⚠ Executor reconcile failing
              </div>
              <div className="text-short/80 font-mono">
                {diag.reconcile_error}
              </div>
              <div className="text-muted mt-2 leading-relaxed">
                The scanner is still evaluating signals below, but Binance
                position/order sync is blocked. In LIVE mode this usually
                means: bad API key, VPS IP not whitelisted, or Futures
                permission missing on the key. Check{" "}
                <em>Settings → Binance API Keys</em> and hit{" "}
                <em>Test LIVE readiness</em> on the Dashboard.
              </div>
            </div>
          )}
          {(diag?.symbols?.length ?? 0) === 0 ? (
            <div className="p-6 text-center text-muted text-sm">
              Waiting for first scan…
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-muted uppercase">
                <tr className="border-b border-border">
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-left">Stage</th>
                  <th className="py-2 px-3 text-left">Bias</th>
                  <th className="py-2 px-3 text-left">Reason</th>
                  <th className="py-2 px-3 text-left">Last eval</th>
                </tr>
              </thead>
              <tbody>
                {diag!.symbols.map((s) => (
                  <tr key={s.symbol} className="border-b border-border/50">
                    <td className="py-2 px-3 font-mono">{s.symbol}</td>
                    <td className="py-2 px-3">
                      <StageBadge stage={s.stage} />
                    </td>
                    <td
                      className={clsx(
                        "py-2 px-3 font-mono",
                        s.bias_side === "LONG" && "text-long",
                        s.bias_side === "SHORT" && "text-short",
                        !s.bias_side && "text-muted",
                      )}
                    >
                      {s.bias_side ?? "—"}
                    </td>
                    <td className="py-2 px-3 text-muted text-xs">{s.reason ?? "—"}</td>
                    <td className="py-2 px-3 text-muted text-xs font-mono">
                      {s.ts ? formatAgo(new Date(s.ts)) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}

/* ---------------- helpers ---------------- */

type StageKey =
  | "executed"
  | "fired"
  | "risk_rejected"
  | "exec_failed"
  | "trigger"
  | "setup"
  | "bias"
  | "warmup";

function countStages(rows: SymbolDiag[]): Record<StageKey, number> {
  const counts: Record<StageKey, number> = {
    executed: 0,
    fired: 0,
    risk_rejected: 0,
    exec_failed: 0,
    trigger: 0,
    setup: 0,
    bias: 0,
    warmup: 0,
  };
  for (const r of rows) {
    if (r.stage in counts) counts[r.stage as StageKey]++;
  }
  return counts;
}

function StagePill({
  label,
  count,
  color,
}: {
  label: string;
  count: number;
  color: "long" | "short" | "yellow" | "blue" | "muted";
}) {
  return (
    <span
      className={clsx(
        "px-2 py-0.5 rounded border",
        count === 0 && "text-muted border-border bg-bg-soft",
        count > 0 && color === "long" && "text-long border-long/40 bg-long/10",
        count > 0 && color === "short" && "text-short border-short/40 bg-short/10",
        count > 0 &&
          color === "yellow" &&
          "text-yellow-300 border-yellow-400/40 bg-yellow-500/10",
        count > 0 &&
          color === "blue" &&
          "text-blue-300 border-blue-400/40 bg-blue-500/10",
        count > 0 && color === "muted" && "text-muted border-border bg-bg-soft",
      )}
    >
      {label} {count}
    </span>
  );
}

function StageBadge({ stage }: { stage: SymbolDiag["stage"] }) {
  const map: Record<string, { label: string; className: string }> = {
    executed: { label: "EXECUTED ✓", className: "bg-long/30 text-long" },
    fired: { label: "FIRED", className: "bg-long/20 text-long" },
    risk_rejected: {
      label: "risk rejected",
      className: "bg-short/20 text-short",
    },
    exec_failed: {
      label: "exec failed",
      className: "bg-short/30 text-short",
    },
    trigger: {
      label: "trigger",
      className: "bg-yellow-500/20 text-yellow-300",
    },
    setup: { label: "setup", className: "bg-blue-500/20 text-blue-300" },
    bias: { label: "bias", className: "bg-bg-soft text-muted" },
    warmup: { label: "warmup", className: "bg-bg-soft text-muted" },
    unknown: { label: "—", className: "text-muted" },
  };
  const it = map[stage] ?? map.unknown;
  return (
    <span className={clsx("px-2 py-0.5 rounded text-xs font-mono", it.className)}>
      {it.label}
    </span>
  );
}

function formatAgo(d: Date): string {
  const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}
