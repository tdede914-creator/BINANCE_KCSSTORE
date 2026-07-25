"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Stats, Trade, TradingMode } from "@/lib/types";
import { TradeRow } from "@/components/TradeRow";
import { StatCard } from "@/components/StatCard";
import { formatUsdt } from "@/lib/format";

export default function HistoryPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [mode, setMode] = useState<TradingMode | "">("");

  const load = useCallback(async () => {
    try {
      const [rows, s] = await Promise.all([
        api.listTrades({ limit: 200, mode: (mode || undefined) as TradingMode | undefined }),
        api.tradeStats((mode || undefined) as TradingMode | undefined),
      ]);
      setTrades(rows);
      setStats(s);
    } catch (e) {
      console.error(e);
    }
  }, [mode]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Trade History</h1>
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as TradingMode | "")}
          className="bg-bg-card border border-border rounded px-3 py-1.5 text-sm"
        >
          <option value="">All modes</option>
          <option value="paper">Paper only</option>
          <option value="live">Live only</option>
        </select>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard
          label="Total P&L"
          value={
            stats
              ? `${stats.total_pnl_usdt >= 0 ? "+" : ""}${formatUsdt(stats.total_pnl_usdt)}`
              : "—"
          }
        />
        <StatCard label="Win rate" value={stats ? `${stats.win_rate_pct}%` : "—"} />
        <StatCard label="Wins" value={stats?.wins ?? "—"} />
        <StatCard label="Losses" value={stats?.losses ?? "—"} />
        <StatCard label="Total" value={stats?.total_trades ?? "—"} />
      </div>

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
            {trades.length === 0 ? (
              <tr>
                <td colSpan={11} className="text-center text-muted p-6">
                  No trades yet.
                </td>
              </tr>
            ) : (
              trades.map((t) => (
                <TradeRow key={t.id} trade={t} onChanged={load} />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
