"use client";

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { MemeScreenerRow, MemeScreenerResponse } from "@/lib/api";
import { formatUsdt } from "@/lib/format";

export default function ScreenerPage() {
  const [data, setData] = useState<MemeScreenerResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(await api.memecoinScreener());
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const addToWatchlist = async (symbol: string) => {
    try {
      const cfg = await api.getConfig();
      if (cfg.watchlist.includes(symbol)) {
        alert(`${symbol} is already in the watchlist.`);
        return;
      }
      await api.patchConfig({ watchlist: [...cfg.watchlist, symbol] });
      alert(`${symbol} added to watchlist. Scanner will pick it up on the next tick.`);
    } catch (e) {
      alert(`Failed to add: ${e}`);
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-4">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold">Meme Coin Screener</h1>
          <p className="text-xs text-muted mt-1 max-w-2xl leading-relaxed">
            Ranks Binance USDT-M meme perpetuals by a composite of
            metrics that correlate with the pre-pump phase — volume
            spike, momentum, volatility squeeze, and funding-rate
            pressure. <strong>Not a pump prediction.</strong> Meme
            moves are driven by non-technical factors (viral moments,
            whale coordination, listings). Use the top rows to decide
            which coins to add to your strategy watchlist, not to
            YOLO in blind.
          </p>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="px-3 py-1.5 rounded border bg-bg-soft border-border text-xs font-semibold hover:text-white disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {err && (
        <div className="text-xs text-short bg-short/10 border border-short/40 rounded p-3">
          {err}
        </div>
      )}

      {data && (
        <>
          <div className="text-[11px] text-muted">
            Generated {new Date(data.generated_at).toLocaleString()}
          </div>

          <div className="bg-bg-card border border-border rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted uppercase border-b border-border">
                <tr>
                  <th className="text-left py-2 px-3">#</th>
                  <th className="text-left py-2 px-3">Symbol</th>
                  <th className="text-right py-2 px-3">Score</th>
                  <th className="text-right py-2 px-3">Price</th>
                  <th className="text-right py-2 px-3">24h %</th>
                  <th className="text-right py-2 px-3">7d %</th>
                  <th className="text-right py-2 px-3">Vol 24h</th>
                  <th className="text-right py-2 px-3">Vol ratio</th>
                  <th className="text-right py-2 px-3">Squeeze</th>
                  <th className="text-right py-2 px-3">Funding</th>
                  <th className="text-left py-2 px-3">Why</th>
                  <th className="py-2 px-3"></th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r, i) => (
                  <Row
                    key={r.symbol}
                    rank={i + 1}
                    row={r}
                    onAdd={() => addToWatchlist(r.symbol)}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <div className="text-[10px] text-muted/70 leading-relaxed max-w-2xl">
            <strong className="text-yellow-300/80">⚠ Risk warning:</strong>{" "}
            {data.disclaimer}
          </div>
        </>
      )}
    </div>
  );
}

function Row({
  rank,
  row,
  onAdd,
}: {
  rank: number;
  row: MemeScreenerRow;
  onAdd: () => void;
}) {
  const posColor = (v: number) => (v > 0 ? "text-long" : v < 0 ? "text-short" : "text-muted");
  const scoreColor =
    row.score >= 0.6
      ? "text-long"
      : row.score >= 0.4
        ? "text-yellow-300"
        : "text-muted";

  if (row.error) {
    return (
      <tr className="border-b border-border/50">
        <td className="py-2 px-3 text-xs text-muted">{rank}</td>
        <td className="py-2 px-3 font-mono text-xs">{row.symbol}</td>
        <td colSpan={9} className="py-2 px-3 text-xs text-short">
          error: {row.error}
        </td>
        <td />
      </tr>
    );
  }

  return (
    <tr className="border-b border-border/50 hover:bg-bg-soft/40">
      <td className="py-2 px-3 text-xs text-muted">{rank}</td>
      <td className="py-2 px-3 font-mono text-xs">{row.symbol}</td>
      <td className={clsx("py-2 px-3 text-xs text-right font-mono font-semibold", scoreColor)}>
        {row.score.toFixed(3)}
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono">{row.price}</td>
      <td className={clsx("py-2 px-3 text-xs text-right font-mono", posColor(row.price_change_24h_pct))}>
        {row.price_change_24h_pct > 0 ? "+" : ""}
        {row.price_change_24h_pct.toFixed(2)}%
      </td>
      <td className={clsx("py-2 px-3 text-xs text-right font-mono", posColor(row.price_change_7d_pct))}>
        {row.price_change_7d_pct > 0 ? "+" : ""}
        {row.price_change_7d_pct.toFixed(2)}%
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono text-muted">
        ${formatUsdt(row.volume_24h_usdt)}
      </td>
      <td
        className={clsx(
          "py-2 px-3 text-xs text-right font-mono",
          row.vol_ratio >= 2 ? "text-long" : row.vol_ratio < 0.7 ? "text-muted/60" : "text-muted",
        )}
      >
        {row.vol_ratio.toFixed(2)}×
      </td>
      <td
        className={clsx(
          "py-2 px-3 text-xs text-right font-mono",
          row.squeeze_ratio < 0.7 ? "text-long" : "text-muted",
        )}
      >
        {row.squeeze_ratio.toFixed(2)}
      </td>
      <td
        className={clsx(
          "py-2 px-3 text-xs text-right font-mono",
          row.funding_rate_pct < -0.05 ? "text-long" : "text-muted",
        )}
      >
        {row.funding_rate_pct > 0 ? "+" : ""}
        {row.funding_rate_pct.toFixed(3)}%
      </td>
      <td className="py-2 px-3 text-[11px] text-muted max-w-[220px]">{row.reason}</td>
      <td className="py-2 px-3">
        <button
          onClick={onAdd}
          className="text-[11px] font-semibold px-2 py-1 rounded border bg-long/10 border-long/40 text-long hover:bg-long/20"
        >
          + Watchlist
        </button>
      </td>
    </tr>
  );
}
