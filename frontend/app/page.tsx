"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/ws";
import type { Config, Signal, Stats, Trade } from "@/lib/types";
import { StatCard } from "@/components/StatCard";
import { SignalCard } from "@/components/SignalCard";
import { TradeRow } from "@/components/TradeRow";
import { formatUsdt } from "@/lib/format";
import type { ChannelInfo } from "@/components/PriceChart";

// lightweight-charts touches window/ResizeObserver — load it browser-only.
const PriceChart = dynamic(
  () => import("@/components/PriceChart").then((m) => m.PriceChart),
  {
    ssr: false,
    loading: () => (
      <div className="bg-bg-soft rounded-lg h-[460px] flex items-center justify-center text-muted text-sm">
        Loading chart…
      </div>
    ),
  },
);

const CHART_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"];

export default function DashboardPage() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Chart local UI state — persisted to localStorage so the user's last
  // chart context survives a page reload.
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const [chartTf, setChartTf] = useState<string | null>(null);
  const [showChannel, setShowChannel] = useState(false);
  const [channelInfo, setChannelInfo] = useState<ChannelInfo | null>(null);
  const [showSR, setShowSR] = useState(false);
  const [srCount, setSrCount] = useState<number | null>(null);
  const [showMTF, setShowMTF] = useState(false);

  useEffect(() => {
    const s = localStorage.getItem("kcs.chart.symbol");
    const tf = localStorage.getItem("kcs.chart.tf");
    const ch = localStorage.getItem("kcs.chart.showChannel");
    const sr = localStorage.getItem("kcs.chart.showSR");
    const mtf = localStorage.getItem("kcs.chart.showMTF");
    if (s) setChartSymbol(s);
    if (tf) setChartTf(tf);
    if (ch === "1") setShowChannel(true);
    if (sr === "1") setShowSR(true);
    if (mtf === "1") setShowMTF(true);
  }, []);

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

      // Default chart selection once config is loaded.
      setChartSymbol((cur) => cur ?? c.watchlist[0] ?? "BTCUSDT");
      setChartTf((cur) => cur ?? c.entry_tf);
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

  const handleSymbolChange = (s: string) => {
    setChartSymbol(s);
    localStorage.setItem("kcs.chart.symbol", s);
  };
  const handleTfChange = (tf: string) => {
    setChartTf(tf);
    localStorage.setItem("kcs.chart.tf", tf);
  };
  const toggleChannel = () => {
    setShowChannel((cur) => {
      const next = !cur;
      localStorage.setItem("kcs.chart.showChannel", next ? "1" : "0");
      if (!next) setChannelInfo(null);
      return next;
    });
  };
  const toggleSR = () => {
    setShowSR((cur) => {
      const next = !cur;
      localStorage.setItem("kcs.chart.showSR", next ? "1" : "0");
      if (!next) setSrCount(null);
      return next;
    });
  };
  const toggleMTF = () => {
    setShowMTF((cur) => {
      const next = !cur;
      localStorage.setItem("kcs.chart.showMTF", next ? "1" : "0");
      return next;
    });
  };

  // Stable callbacks so PriceChart doesn't refetch on every dashboard tick.
  const handleChannelInfo = useCallback((info: ChannelInfo | null) => {
    setChannelInfo(info);
  }, []);
  const handleSRInfo = useCallback((n: number | null) => {
    setSrCount(n);
  }, []);

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

  // Trades filtered by the chart symbol so PriceChart only draws relevant lines
  // (PriceChart also filters internally, but this saves prop churn).
  const openTradesForSymbol = useMemo(
    () => openTrades.filter((t) => t.symbol === chartSymbol),
    [openTrades, chartSymbol],
  );

  // Higher timeframes to overlay when MTF toggle is on. Picked so the user
  // sees a "zoom out" of trend context on their current view.
  const mtfIntervals = useMemo(
    () => (showMTF && chartTf ? higherTimeframes(chartTf) : []),
    [showMTF, chartTf],
  );

  // Watchlist + any open-trade symbols not already in it → chart selector options.
  const chartSymbols = useMemo(() => {
    const set = new Set<string>(cfg?.watchlist ?? []);
    for (const t of openTrades) set.add(t.symbol);
    if (chartSymbol) set.add(chartSymbol);
    return Array.from(set);
  }, [cfg, openTrades, chartSymbol]);

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

      {/* Chart */}
      <section className="bg-bg-card border border-border rounded-lg p-4">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold">Chart</h2>
            {chartSymbol && chartTf && (
              <span className="text-xs text-muted font-mono">
                {chartSymbol} · {chartTf}
                {openTradesForSymbol.length > 0 && (
                  <> · {openTradesForSymbol.length} open</>
                )}
              </span>
            )}
            {showChannel && channelInfo && (
              <ChannelBadge info={channelInfo} />
            )}
            {showSR && srCount !== null && (
              <span
                className="text-xs font-mono px-2 py-0.5 rounded border bg-bg-soft text-muted border-border"
                title="Auto-detected S/R levels from swing clusters"
              >
                S/R × {srCount}
              </span>
            )}
            {showMTF && mtfIntervals.length > 0 && (
              <span
                className="text-xs font-mono px-2 py-0.5 rounded border bg-pink-500/10 text-pink-300 border-pink-400/40"
                title="Higher-timeframe channels overlaid on the current chart"
              >
                MTF: {mtfIntervals.join(" · ")}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <select
              value={chartSymbol ?? ""}
              onChange={(e) => handleSymbolChange(e.target.value)}
              className="bg-bg-soft border border-border rounded px-3 py-1.5 text-sm font-mono min-w-[130px]"
            >
              {chartSymbols.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <div className="flex gap-1 flex-wrap">
              {CHART_TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  onClick={() => handleTfChange(tf)}
                  className={clsx(
                    "px-2.5 py-1 rounded text-xs font-mono",
                    chartTf === tf
                      ? "bg-bg-soft text-white border border-border"
                      : "text-muted hover:text-white",
                  )}
                >
                  {tf}
                </button>
              ))}
            </div>
            <button
              onClick={toggleChannel}
              className={clsx(
                "px-2.5 py-1 rounded text-xs font-mono border",
                showChannel
                  ? "bg-teal-500/20 text-teal-300 border-teal-400/40"
                  : "text-muted hover:text-white border-border",
              )}
              title="Auto-draw a regression parallel channel over the last 100 candles"
            >
              Channel
            </button>
            <button
              onClick={toggleSR}
              className={clsx(
                "px-2.5 py-1 rounded text-xs font-mono border",
                showSR
                  ? "bg-yellow-500/20 text-yellow-300 border-yellow-400/40"
                  : "text-muted hover:text-white border-border",
              )}
              title="Auto-detect and draw horizontal Support/Resistance levels"
            >
              S/R
            </button>
            <button
              onClick={toggleMTF}
              className={clsx(
                "px-2.5 py-1 rounded text-xs font-mono border",
                showMTF
                  ? "bg-pink-500/20 text-pink-300 border-pink-400/40"
                  : "text-muted hover:text-white border-border",
              )}
              title="Overlay parallel channels from higher timeframes"
            >
              MTF
            </button>
          </div>
        </div>

        {cfg && chartSymbol && chartTf ? (
          <PriceChart
            symbol={chartSymbol}
            interval={chartTf}
            emaFast={cfg.ema_fast}
            emaSlow={cfg.ema_slow}
            emaTrigger={cfg.ema_trigger}
            openTrades={openTradesForSymbol}
            testnet={cfg.binance_testnet}
            height={480}
            showChannel={showChannel}
            onChannelInfo={handleChannelInfo}
            showSR={showSR}
            onSRInfo={handleSRInfo}
            mtfIntervals={mtfIntervals}
          />
        ) : (
          <div className="h-[480px] bg-bg-soft rounded flex items-center justify-center text-muted text-sm">
            Loading chart…
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
          <LegendDot color="#f1c40f" label={`EMA ${cfg?.ema_fast ?? ""}`} />
          <LegendDot color="#9b59b6" label={`EMA ${cfg?.ema_slow ?? ""}`} />
          <LegendDot color="#3498db" label={`EMA ${cfg?.ema_trigger ?? ""}`} />
          <LegendDot color="#8892b0" label="Entry (dashed)" />
          <LegendDot color="#e74c3c" label="SL" />
          <LegendDot color="#2ecc71" label="TP1/TP2" />
          {showChannel && (
            <LegendDot color="#14b8a6" label="Regression channel" />
          )}
          {showSR && (
            <>
              <LegendDot color="#2ecc71" label="Support" />
              <LegendDot color="#e74c3c" label="Resistance" />
            </>
          )}
          {showMTF &&
            mtfIntervals.map((iv, idx) => (
              <LegendDot
                key={iv}
                color={MTF_LEGEND_COLORS[idx % MTF_LEGEND_COLORS.length]}
                label={`MTF ${iv}`}
              />
            ))}
        </div>
      </section>

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
              <SignalCard
                key={s.id}
                signal={s}
                onSymbolClick={handleSymbolChange}
              />
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
                  <TradeRow
                    key={t.id}
                    trade={t}
                    onChanged={refresh}
                    onSymbolClick={handleSymbolChange}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

const MTF_LEGEND_COLORS = ["#ff9800", "#06b6d4", "#ec4899"];

/**
 * Given a base timeframe, return up to 3 higher timeframes to overlay.
 * Keeps the selection sensible (avoids drawing 4h channel on a 1m chart —
 * that data span would be days off-screen and produce a near-horizontal
 * line) and consistent across the app.
 */
function higherTimeframes(current: string): string[] {
  const map: Record<string, string[]> = {
    "1m":  ["15m", "1h", "4h"],
    "3m":  ["15m", "1h", "4h"],
    "5m":  ["30m", "1h", "4h"],
    "15m": ["1h", "4h", "1d"],
    "30m": ["1h", "4h", "1d"],
    "1h":  ["4h", "1d"],
    "2h":  ["4h", "1d"],
    "4h":  ["1d"],
    "1d":  [],
  };
  return map[current] ?? [];
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block w-2.5 h-2.5 rounded-full"
        style={{ backgroundColor: color }}
      />
      {label}
    </span>
  );
}

function ChannelBadge({ info }: { info: ChannelInfo }) {
  const up = info.slope_pct_total >= 0;
  const arrow = up ? "▲" : "▼";
  return (
    <span
      className={clsx(
        "text-xs font-mono px-2 py-0.5 rounded border",
        up
          ? "bg-long/10 text-long border-long/40"
          : "bg-short/10 text-short border-short/40",
      )}
      title={`Regression channel over ${info.lookback} candles`}
    >
      {arrow} {info.slope_pct_total >= 0 ? "+" : ""}
      {info.slope_pct_total.toFixed(2)}% · width {info.width_pct.toFixed(2)}%
    </span>
  );
}
