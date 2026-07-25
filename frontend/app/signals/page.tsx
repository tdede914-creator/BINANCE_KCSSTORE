"use client";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/ws";
import { SignalCard } from "@/components/SignalCard";
import type { Signal } from "@/lib/types";

export default function SignalsPage() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [symbol, setSymbol] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const rows = await api.listSignals({
        limit: 100,
        symbol: symbol || undefined,
      });
      setSignals(rows);
    } catch (e) {
      console.error(e);
    }
  }, [symbol]);

  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  useEventStream((ev) => {
    if (ev.type === "signal.new") load();
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Signals</h1>
        <input
          type="text"
          placeholder="Filter by symbol (BTCUSDT)"
          className="bg-bg-card border border-border rounded px-3 py-1.5 text-sm w-64"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
        />
      </div>
      {signals.length === 0 ? (
        <div className="text-sm text-muted bg-bg-card border border-border rounded-lg p-6 text-center">
          No signals yet.
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {signals.map((s) => (
            <SignalCard key={s.id} signal={s} />
          ))}
        </div>
      )}
    </div>
  );
}
