"use client";
import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/ws";
import type { Config, Signal, Stats, Trade } from "@/lib/types";
import { StatCard } from "@/components/StatCard";
import { SignalCard } from "@/components/SignalCard";
import { TradeRow } from "@/components/TradeRow";
import { formatUsdt } from "@/lib/format";

export default function DashboardPage() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [c, s, sig, trades] = await Promise.all([
        api.getConfig(),
        api.tradeStats(),
        api.listSignals({ limit: 8 }),
        api.listTrades({ open_only: true, limit: 20 }),
      ]);
      setCfg(c);
      setStats(s);
      setSignals(sig);
      setOpenTrades(trades);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  useEventStream((ev) => {
    if (ev.type === "signal.new" || ev.type === "trade.update") {
      refresh();
    }
  });

  const toggleScanner = async () => {
    if (!cfg) return;
    setBusy(true);
    try {
      const updated = await api.patchConfig({
        scanner_enabled: !cfg.scanner_enabled,
      });
      setCfg(updated);
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleMode = async () => {
    if (!cfg) return;
    if (cfg.trading_mode === "paper") {
      if (
        !window.confirm(
          "Switch to LIVE mode? Real orders will be placed with real money. Continue?",
        )
      )
        return;
    }
    setBusy(true);
    try {
      const updated = await api.patchConfig({
        trading_mode: cfg.trading_mode === "paper" ? "live" : "paper",
      });
      setCfg(updated);
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (err && !cfg) {
    return (
      <div className="bg-short/10 border border-short text-short rounded-lg p-4">
        Backend unreachable: {err}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header controls */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="text-sm text-muted">
            {cfg
              ? `Mode: ${cfg.trading_mode.toUpperCase()} · Scanner ${
                  cfg.scanner_enabled ? "ON" : "OFF"
                } · TFs ${cfg.bias_tf}/${cfg.setup_tf}/${cfg.entry_tf}`
              : "Loading…"}
          </p>
        </div>

        <div className="flex items-center gap-3">
          {cfg && (
            <>
              <button
                onClick={toggleMode}
                disabled={busy}
                className={clsx(
                  "px-3 py-2 rounded font-semibold text-sm",
                  cfg.trading_mode === "live"
                    ? "bg-short/20 text-short border border-short/40"
                    : "bg-bg-card text-muted border border-border",
                )}
              >
                {cfg.trading_mode === "live" ? "LIVE" : "PAPER"}
              </button>
              <button
                onClick={toggleScanner}
                disabled={busy}
                className={clsx(
                  "px-4 py-2 rounded font-semibold text-sm",
                  cfg.scanner_enabled
                    ? "bg-long/20 text-long border border-long/40"
                    : "bg-bg-card border border-border",
                )}
              >
                {cfg.scanner_enabled ? "Scanner ON" : "Scanner OFF"}
              </button>
            </>
          )}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard label="Total P&L" value={
          stats ? `${stats.total_pnl_usdt >= 0 ? "+" : ""}${formatUsdt(stats.total_pnl_usdt)} USDT` : "—"
        } />
        <StatCard label="Win rate" value={stats ? `${stats.win_rate_pct}%` : "—"} />
        <StatCard label="Total trades" value={stats?.total_trades ?? "—"} />
        <StatCard label="Open" value={stats?.open_trades ?? "—"} />
        <StatCard
          label="Paper balance"
          value={cfg ? `$${formatUsdt(cfg.paper_balance)}` : "—"}
        />
      </div>

      {/* Live signals */}
      <section>
        <h2 className="text-lg font-semibold mb-3">Recent signals</h2>
        {signals.length === 0 ? (
          <div className="text-sm text-muted bg-bg-card border border-border rounded-lg p-6 text-center">
            No signals yet. Enable the scanner and wait for the market.
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {signals.map((s) => (
              <SignalCard key={s.id} signal={s} />
            ))}
          </div>
        )}
      </section>

      {/* Open trades */}
      <section>
        <h2 className="text-lg font-semibold mb-3">Open positions</h2>
        {openTrades.length === 0 ? (
          <div className="text-sm text-muted bg-bg-card border border-border rounded-lg p-6 text-center">
            No open positions.
          </div>
        ) : (
          <div className="bg-bg-card border border-border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted uppercase">
                <tr className="border-b border-border">
                  <th className="py-2 px-3 text-left">Time</th>
                  <th className="py-2 px-3 text-left">Symbol</th>
                  <th className="py-2 px-3 text-left">Side</th>
                  <th className="py-2 px-3 text-left">Mode</th>
                  <th className="py-2 px-3 text-left">Entry</th>
                  <th className="py-2 px-3 text-left">SL</th>
                  <th className="py-2 px-3 text-left">TP1 / TP2</th>
                  <th className="py-2 px-3 text-left">Qty</th>
                  <th className="py-2 px-3 text-left">P&L</th>
                  <th className="py-2 px-3 text-left">Status</th>
                  <th className="py-2 px-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.map((t) => (
                  <TradeRow key={t.id} trade={t} onChanged={refresh} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
