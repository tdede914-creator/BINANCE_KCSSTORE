"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";

import { api } from "@/lib/api";
import type {
  BacktestRequest,
  BacktestResponse,
  BacktestTrade,
  BatchBacktestRequest,
  BatchBacktestResponse,
  BatchBacktestSummary,
} from "@/lib/api";
import { formatUsdt } from "@/lib/format";
import { StatCard } from "@/components/StatCard";

// Timeframes valid on both the strategy and Binance klines endpoint.
const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"];

// Rough runtime estimate so the user isn't surprised by a slow run.
// Numbers include the recent indicator-precompute optimization; if the
// backtest keeps getting slower we should re-measure and update these.
const _RUNTIME_SEC_PER_DAY: Record<string, number> = {
  "1m": 6,
  "3m": 2,
  "5m": 1.3,
  "15m": 0.5,
  "30m": 0.3,
  "1h": 0.15,
  "2h": 0.08,
  "4h": 0.05,
  "1d": 0.02,
};

const runtimeSecondsFor = (tf: string, days: number): number => {
  const sPerDay = _RUNTIME_SEC_PER_DAY[tf] ?? 0.5;
  return Math.max(3, Math.round(sPerDay * days));
};

const runtimeHintFor = (tf: string, days: number): string => {
  const s = runtimeSecondsFor(tf, days);
  if (s < 60) return `~${s}s`;
  const m = s / 60;
  return `~${m.toFixed(1)}m`;
};

export default function BacktestPage() {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [batchResult, setBatchResult] = useState<BatchBacktestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Mode: single symbol vs batch (comma / newline-separated list).
  const [mode, setMode] = useState<"single" | "batch">("single");

  // Form state — sensible defaults for a first-run experiment.
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [symbolsText, setSymbolsText] = useState(
    "BTCUSDT\nETHUSDT\nSOLUSDT\nBNBUSDT\nXRPUSDT\nDOGEUSDT\nADAUSDT\nAVAXUSDT\nLINKUSDT\n1000PEPEUSDT",
  );
  const [biasTf, setBiasTf] = useState("4h");
  const [setupTf, setSetupTf] = useState("1h");
  const [entryTf, setEntryTf] = useState("15m");
  const [days, setDays] = useState(60);
  const [initialBalance, setInitialBalance] = useState(1000);
  const [riskPct, setRiskPct] = useState(1.0);
  const [leverage, setLeverage] = useState(5);

  const parsedSymbols = useMemo(
    () =>
      Array.from(
        new Set(
          symbolsText
            .split(/[\s,]+/)
            .map((s) => s.trim().toUpperCase())
            .filter(Boolean),
        ),
      ).slice(0, 20),
    [symbolsText],
  );

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    setBatchResult(null);
    try {
      if (mode === "batch") {
        if (parsedSymbols.length === 0) {
          throw new Error("Batch symbols list is empty.");
        }
        const req: BatchBacktestRequest = {
          symbols: parsedSymbols,
          bias_tf: biasTf,
          setup_tf: setupTf,
          entry_tf: entryTf,
          days,
          initial_balance: initialBalance,
          risk_per_trade_pct: riskPct,
          leverage,
        };
        const resp = await api.runBatchBacktest(req);
        setBatchResult(resp);
      } else {
        const req: BacktestRequest = {
          symbol: symbol.toUpperCase(),
          bias_tf: biasTf,
          setup_tf: setupTf,
          entry_tf: entryTf,
          days,
          initial_balance: initialBalance,
          risk_per_trade_pct: riskPct,
          leverage,
        };
        const resp = await api.runBacktest(req);
        setResult(resp);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Backtest</h1>
        <p className="text-sm text-muted mt-1">
          Replay MTF Confluence over historical klines. Same strategy code
          the live scanner runs, so results transfer directly (subject to
          slippage, funding, and regime shifts).
        </p>
      </div>

      {/* Mode toggle */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => setMode("single")}
          disabled={running}
          className={clsx(
            "px-3 py-1.5 rounded text-xs font-semibold border",
            mode === "single"
              ? "bg-long/20 border-long/50 text-long"
              : "bg-bg-soft border-border text-muted hover:text-white",
          )}
        >
          Single symbol
        </button>
        <button
          onClick={() => setMode("batch")}
          disabled={running}
          className={clsx(
            "px-3 py-1.5 rounded text-xs font-semibold border",
            mode === "batch"
              ? "bg-long/20 border-long/50 text-long"
              : "bg-bg-soft border-border text-muted hover:text-white",
          )}
        >
          Compare many
        </button>
        {mode === "batch" && (
          <span className="text-xs text-muted">
            · {parsedSymbols.length} symbol{parsedSymbols.length === 1 ? "" : "s"} queued (max 20)
          </span>
        )}
      </div>

      {/* Form */}
      <section className="bg-bg-card border border-border rounded-lg p-5 space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          {mode === "single" ? (
            <Field label="Symbol">
              <input
                type="text"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                disabled={running}
                className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono w-full"
                placeholder="BTCUSDT"
              />
            </Field>
          ) : (
            <div className="col-span-2 md:col-span-1 row-span-3">
              <Field label={`Symbols (one per line, max 20) — ${parsedSymbols.length} queued`}>
                <textarea
                  value={symbolsText}
                  onChange={(e) => setSymbolsText(e.target.value)}
                  disabled={running}
                  rows={9}
                  className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono w-full resize-y"
                  placeholder="BTCUSDT&#10;ETHUSDT&#10;SOLUSDT"
                />
              </Field>
            </div>
          )}
          <Field label="Days back">
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              disabled={running}
              className="bg-bg-soft border border-border rounded px-3 py-2 text-sm w-full"
            >
              {[7, 14, 30, 60, 90, 180, 365].map((d) => (
                <option key={d} value={d}>
                  {d} days
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Est. runtime"
            hint="approximate, based on candle count"
          >
            <div className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono text-muted">
              {runtimeHintFor(entryTf, days)}
            </div>
          </Field>

          <TFSelect label="Bias TF" value={biasTf} onChange={setBiasTf} />
          <TFSelect label="Setup TF" value={setupTf} onChange={setSetupTf} />
          <TFSelect label="Entry TF" value={entryTf} onChange={setEntryTf} />

          <Field label="Initial balance (USDT)">
            <NumberInput
              value={initialBalance}
              onChange={setInitialBalance}
              step={100}
              min={1}
              disabled={running}
            />
          </Field>
          <Field label="Risk / trade (%)">
            <NumberInput
              value={riskPct}
              onChange={setRiskPct}
              step={0.1}
              min={0.1}
              max={20}
              disabled={running}
            />
          </Field>
          <Field label="Leverage">
            <NumberInput
              value={leverage}
              onChange={setLeverage}
              step={1}
              min={1}
              max={125}
              disabled={running}
            />
          </Field>
        </div>

        <div className="flex flex-col gap-2 pt-2 border-t border-border/50">
          {(() => {
            const perSymSec = runtimeSecondsFor(entryTf, days);
            const totalSec =
              mode === "batch" ? perSymSec * Math.max(1, parsedSymbols.length) : perSymSec;
            const hint =
              totalSec < 60
                ? `~${totalSec}s`
                : `~${(totalSec / 60).toFixed(1)}m`;
            if (totalSec <= 90) return null;
            return (
              <div className="text-xs text-yellow-300 bg-yellow-500/10 border border-yellow-500/30 rounded px-3 py-2">
                Estimated runtime {hint}
                {mode === "batch" &&
                  ` (${parsedSymbols.length} × ${runtimeHintFor(entryTf, days)})`}
                . Heavy but doable. Tip: try Entry TF 15m or 1h for a
                faster first-pass; drop to 5m only when tuning promising
                setups.
              </div>
            );
          })()}
          <div className="flex items-center gap-3">
            <button
              onClick={run}
              disabled={
                running ||
                (mode === "single" ? !symbol : parsedSymbols.length === 0)
              }
              className="px-4 py-2 bg-long/20 hover:bg-long/30 text-long border border-long/40 rounded text-sm font-semibold disabled:opacity-50"
            >
              {running
                ? "Running…"
                : mode === "batch"
                  ? `Run batch (${parsedSymbols.length})`
                  : "Run backtest"}
            </button>
            {running && (
              <span className="text-xs text-muted animate-pulse">
                {mode === "batch"
                  ? "Running each symbol sequentially… watch backend logs for backtest.batch.symbol."
                  : "Fetching klines, precomputing indicators, replaying the strategy… Backend logs backtest.progress every 500 bars."}
              </span>
            )}
          </div>
        </div>
      </section>

      {error && (
        <div className="bg-short/10 border border-short/40 text-short rounded-lg p-4 text-sm">
          {error}
        </div>
      )}

      {result && <BacktestResults result={result} />}
      {batchResult && <BatchResults result={batchResult} />}
    </div>
  );
}

/* ================================================================== */
/*  Batch results                                                      */
/* ================================================================== */

type SortKey =
  | "symbol"
  | "total_trades"
  | "win_rate_pct"
  | "total_return_pct"
  | "profit_factor"
  | "max_drawdown_pct"
  | "avg_rr"
  | "total_fees_usdt";

function BatchResults({ result }: { result: BatchBacktestResponse }) {
  const [sortKey, setSortKey] = useState<SortKey>("total_return_pct");
  const [asc, setAsc] = useState(false);

  const sorted = useMemo(() => {
    const arr = result.summaries.slice();
    arr.sort((a, b) => {
      const av = a[sortKey] as number | string;
      const bv = b[sortKey] as number | string;
      if (typeof av === "number" && typeof bv === "number") {
        return asc ? av - bv : bv - av;
      }
      return asc
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    return arr;
  }, [result.summaries, sortKey, asc]);

  const clickHeader = (k: SortKey) => {
    if (k === sortKey) setAsc((v) => !v);
    else {
      setSortKey(k);
      setAsc(false); // desc by default (biggest return first, biggest DD first, etc)
    }
  };

  const valid = sorted.filter((s) => !s.error);
  const winners = valid.filter((s) => s.total_return_usdt > 0);
  const losers = valid.filter((s) => s.total_return_usdt <= 0);
  const avgReturn =
    valid.length > 0
      ? valid.reduce((a, s) => a + s.total_return_pct, 0) / valid.length
      : 0;

  return (
    <div className="space-y-4">
      {result.period_from && result.period_to && (
        <div className="text-xs text-muted font-mono">
          {result.days} days · {result.entry_tf} entry ·{" "}
          {new Date(result.period_from).toLocaleDateString()} →{" "}
          {new Date(result.period_to).toLocaleDateString()}
        </div>
      )}

      {/* High-level roll-up */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Symbols tested"
          value={result.summaries.length}
          hint={`${valid.length} ok · ${result.summaries.length - valid.length} failed`}
        />
        <StatCard
          label="Profitable pairs"
          value={`${winners.length} / ${valid.length}`}
          hint={winners.map((w) => w.symbol).slice(0, 3).join(", ") || "—"}
          className="border-long/40 text-long"
        />
        <StatCard
          label="Losing pairs"
          value={`${losers.length} / ${valid.length}`}
          hint={losers.map((w) => w.symbol).slice(0, 3).join(", ") || "—"}
          className="border-short/40 text-short"
        />
        <StatCard
          label="Avg return / pair"
          value={`${avgReturn >= 0 ? "+" : ""}${avgReturn.toFixed(2)}%`}
          hint="Simple mean — assumes equal allocation"
          className={
            avgReturn >= 0
              ? "border-long/40 text-long"
              : "border-short/40 text-short"
          }
        />
      </div>

      {/* Comparison table */}
      <section className="bg-bg-card border border-border rounded-lg overflow-x-auto">
        <div className="p-4 border-b border-border">
          <h2 className="text-lg font-semibold">Comparison</h2>
          <p className="text-xs text-muted mt-1">
            Click column headers to sort. Sorted by {sortKey} {asc ? "asc" : "desc"}.
          </p>
        </div>
        <table className="w-full text-sm">
          <thead className="text-xs text-muted uppercase">
            <tr className="border-b border-border">
              <Th k="symbol" active={sortKey} asc={asc} onClick={clickHeader}>Symbol</Th>
              <Th k="total_trades" active={sortKey} asc={asc} onClick={clickHeader} align="right">Trades</Th>
              <Th k="win_rate_pct" active={sortKey} asc={asc} onClick={clickHeader} align="right">Win %</Th>
              <Th k="total_return_pct" active={sortKey} asc={asc} onClick={clickHeader} align="right">Return</Th>
              <Th k="profit_factor" active={sortKey} asc={asc} onClick={clickHeader} align="right">PF</Th>
              <Th k="max_drawdown_pct" active={sortKey} asc={asc} onClick={clickHeader} align="right">Max DD</Th>
              <Th k="avg_rr" active={sortKey} asc={asc} onClick={clickHeader} align="right">Avg RR</Th>
              <Th k="total_fees_usdt" active={sortKey} asc={asc} onClick={clickHeader} align="right">Fees</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => (
              <BatchRow key={s.symbol} s={s} />
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function Th({
  k,
  active,
  asc,
  onClick,
  align,
  children,
}: {
  k: SortKey;
  active: SortKey;
  asc: boolean;
  onClick: (k: SortKey) => void;
  align?: "left" | "right";
  children: React.ReactNode;
}) {
  const isActive = k === active;
  return (
    <th
      onClick={() => onClick(k)}
      className={clsx(
        "py-2 px-3 cursor-pointer select-none hover:text-white transition-colors",
        align === "right" ? "text-right" : "text-left",
        isActive && "text-white",
      )}
    >
      {children}
      {isActive && <span className="ml-1 text-[10px]">{asc ? "▲" : "▼"}</span>}
    </th>
  );
}

function BatchRow({ s }: { s: BatchBacktestSummary }) {
  if (s.error) {
    return (
      <tr className="border-b border-border/50">
        <td className="py-2 px-3 font-mono text-xs">{s.symbol}</td>
        <td colSpan={7} className="py-2 px-3 text-xs text-short">
          error: {s.error}
        </td>
      </tr>
    );
  }
  const pos = s.total_return_usdt >= 0;
  return (
    <tr className="border-b border-border/50 hover:bg-bg-soft/50">
      <td className="py-2 px-3 font-mono text-xs">{s.symbol}</td>
      <td className="py-2 px-3 text-xs text-right font-mono">{s.total_trades}</td>
      <td className="py-2 px-3 text-xs text-right font-mono">
        {s.win_rate_pct.toFixed(1)}%
      </td>
      <td
        className={clsx(
          "py-2 px-3 text-xs text-right font-mono",
          pos ? "text-long" : "text-short",
        )}
      >
        {pos ? "+" : ""}
        {s.total_return_pct.toFixed(2)}%
        <span className="ml-1 text-muted text-[10px]">
          ({pos ? "+" : ""}${s.total_return_usdt.toFixed(2)})
        </span>
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono">
        {s.profit_factor.toFixed(2)}
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono text-yellow-300/80">
        {s.max_drawdown_pct.toFixed(2)}%
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono">
        {s.avg_rr.toFixed(2)}
      </td>
      <td className="py-2 px-3 text-xs text-right font-mono text-muted">
        ${s.total_fees_usdt.toFixed(2)}
      </td>
    </tr>
  );
}

/* ================================================================== */
/*  Results                                                            */
/* ================================================================== */

function BacktestResults({ result }: { result: BacktestResponse }) {
  const m = result.metrics;
  const positive = m.total_return_usdt >= 0;

  return (
    <div className="space-y-6">
      {/* Summary meta */}
      <div className="text-xs text-muted font-mono">
        {result.symbol} · {result.entry_tf} · {result.total_bars} bars ·{" "}
        {new Date(result.period_from).toLocaleDateString()} →{" "}
        {new Date(result.period_to).toLocaleDateString()}
      </div>

      {/* Headline metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Total return"
          value={`${positive ? "+" : ""}${formatUsdt(m.total_return_usdt)}`}
          hint={`${positive ? "+" : ""}${m.total_return_pct.toFixed(2)}% · ${formatUsdt(m.initial_balance)} → ${formatUsdt(m.final_balance)}`}
          className={
            positive
              ? "border-long/40 text-long"
              : "border-short/40 text-short"
          }
        />
        <StatCard
          label="Win rate"
          value={`${m.win_rate_pct}%`}
          hint={`${m.wins}W / ${m.losses}L / ${m.breakevens}BE`}
        />
        <StatCard
          label="Max drawdown"
          value={`${m.max_drawdown_pct.toFixed(2)}%`}
          hint="peak-to-trough on equity curve"
        />
        <StatCard
          label="Profit factor"
          value={m.profit_factor.toFixed(2)}
          hint={`Sharpe ${m.sharpe_ratio.toFixed(2)} · Avg RR ${m.avg_rr.toFixed(2)}`}
        />
      </div>

      {/* Second-row metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Total trades"
          value={m.total_trades}
          hint={`TP2 ${m.exits_tp2} · SL ${m.exits_sl} · EOP ${m.exits_eop}`}
        />
        <StatCard
          label="Avg win / loss"
          value={`+${formatUsdt(m.avg_win_usdt)} / ${formatUsdt(m.avg_loss_usdt)}`}
        />
        <StatCard
          label="Best / worst"
          value={`+${formatUsdt(m.best_trade_usdt)} / ${formatUsdt(m.worst_trade_usdt)}`}
        />
        <StatCard
          label="Total fees"
          value={`$${formatUsdt(m.total_fees_usdt)}`}
          hint={`${((m.total_fees_usdt / m.initial_balance) * 100).toFixed(2)}% of initial`}
        />
      </div>

      {/* Equity curve */}
      <section className="bg-bg-card border border-border rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3">Equity curve</h2>
        <EquityCurve
          points={result.equity_curve}
          initial={m.initial_balance}
        />
      </section>

      {/* Trades table */}
      <section className="bg-bg-card border border-border rounded-lg overflow-x-auto">
        <div className="p-4 border-b border-border">
          <h2 className="text-lg font-semibold">
            Trades ({result.trades.length})
          </h2>
        </div>
        <table className="w-full text-sm">
          <thead className="text-xs text-muted uppercase">
            <tr className="border-b border-border">
              <th className="py-2 px-3 text-left">Open</th>
              <th className="py-2 px-3 text-left">Side</th>
              <th className="py-2 px-3 text-left">Entry</th>
              <th className="py-2 px-3 text-left">SL</th>
              <th className="py-2 px-3 text-left">TP1 / TP2</th>
              <th className="py-2 px-3 text-left">Exit</th>
              <th className="py-2 px-3 text-left">Duration</th>
              <th className="py-2 px-3 text-right">Fees</th>
              <th className="py-2 px-3 text-right">Net P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {result.trades.length === 0 ? (
              <tr>
                <td
                  colSpan={9}
                  className="text-center text-muted p-6 text-sm"
                >
                  No trades fired in this period.
                </td>
              </tr>
            ) : (
              result.trades.map((t, i) => <TradeRow key={i} trade={t} />)
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function TradeRow({ trade: t }: { trade: BacktestTrade }) {
  const win = t.realized_pnl > 0;
  const duration = t.close_time
    ? Math.round(
        (new Date(t.close_time).getTime() - new Date(t.open_time).getTime()) /
          60000,
      )
    : null;
  return (
    <tr className="border-b border-border/50 hover:bg-bg-soft/50">
      <td className="py-2 px-3 font-mono text-xs">
        {new Date(t.open_time).toLocaleString()}
      </td>
      <td
        className={clsx(
          "py-2 px-3 font-semibold text-xs",
          t.side === "LONG" ? "text-long" : "text-short",
        )}
      >
        {t.side}
      </td>
      <td className="py-2 px-3 font-mono text-xs">
        {trimNum(t.entry_price)}
      </td>
      <td className="py-2 px-3 font-mono text-xs text-short">
        {trimNum(t.initial_sl)}
      </td>
      <td className="py-2 px-3 font-mono text-xs text-long">
        {trimNum(t.take_profit_1)} / {trimNum(t.take_profit_2)}
      </td>
      <td className="py-2 px-3 text-xs">
        <span
          className={clsx(
            "px-1.5 py-0.5 rounded",
            t.close_reason === "TP2" && "bg-long/20 text-long",
            t.close_reason === "TP1" && "bg-yellow-500/20 text-yellow-300",
            t.close_reason === "SL" && "bg-short/20 text-short",
            t.close_reason === "EOP" && "bg-bg-soft text-muted",
          )}
        >
          {t.close_reason ?? "?"}
        </span>
      </td>
      <td className="py-2 px-3 text-xs text-muted">
        {duration != null
          ? duration < 60
            ? `${duration}m`
            : `${(duration / 60).toFixed(1)}h`
          : "—"}
      </td>
      <td className="py-2 px-3 text-xs text-muted font-mono text-right">
        {formatUsdt(t.total_fees)}
      </td>
      <td
        className={clsx(
          "py-2 px-3 font-mono text-right",
          win ? "text-long" : "text-short",
        )}
      >
        {win ? "+" : ""}
        {formatUsdt(t.realized_pnl)}
      </td>
    </tr>
  );
}

function trimNum(n: number): string {
  if (!isFinite(n)) return "—";
  const abs = Math.abs(n);
  const dec = abs >= 1000 ? 2 : abs >= 10 ? 3 : abs >= 1 ? 4 : abs >= 0.1 ? 5 : 7;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: dec,
    maximumFractionDigits: dec,
  });
}

/* ================================================================== */
/*  Equity curve chart (lightweight-charts)                            */
/* ================================================================== */

function EquityCurve({
  points,
  initial,
}: {
  points: { time: string; equity: number }[];
  initial: number;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void) | null = null;

    (async () => {
      if (!containerRef.current) return;
      // Dynamic import — lightweight-charts touches the DOM.
      const {
        createChart,
        ColorType,
        LineStyle,
      } = await import("lightweight-charts");
      if (cancelled || !containerRef.current) return;

      const chart = createChart(containerRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: "#131a2b" },
          textColor: "#8892b0",
        },
        grid: {
          vertLines: { color: "#1f2942" },
          horzLines: { color: "#1f2942" },
        },
        timeScale: {
          borderColor: "#26304a",
          timeVisible: true,
          secondsVisible: false,
        },
        rightPriceScale: { borderColor: "#26304a" },
        autoSize: true,
        height: 320,
      });

      const line = chart.addLineSeries({
        color: "#14b8a6",
        lineWidth: 2,
        priceLineVisible: false,
      });
      // Baseline at initial balance so profit/loss is obvious.
      line.createPriceLine({
        price: initial,
        color: "#8892b0",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "start",
      });

      const data = points.map((p) => ({
        time: Math.floor(new Date(p.time).getTime() / 1000) as never,
        value: p.equity,
      }));
      // Ensure monotonically increasing time (lightweight-charts requires it).
      const sorted = data.slice().sort((a, b) => (a.time as unknown as number) - (b.time as unknown as number));
      line.setData(sorted);
      chart.timeScale().fitContent();

      const ro = new ResizeObserver(() => {
        chart.applyOptions({ height: 320 });
      });
      ro.observe(containerRef.current);

      cleanup = () => {
        ro.disconnect();
        chart.remove();
      };
    })();

    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [points, initial]);

  if (points.length === 0) {
    return (
      <div className="h-[320px] flex items-center justify-center text-muted text-sm">
        No equity movement — no trades were closed in this period.
      </div>
    );
  }
  return <div ref={containerRef} className="h-[320px] w-full rounded" />;
}

/* ================================================================== */
/*  Small form helpers                                                 */
/* ================================================================== */

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-xs text-muted mb-1">
        {label}
        {hint && <span className="ml-2 text-muted/70">· {hint}</span>}
      </div>
      {children}
    </label>
  );
}

function NumberInput({
  value,
  onChange,
  step,
  min,
  max,
  disabled,
}: {
  value: number;
  onChange: (v: number) => void;
  step?: number;
  min?: number;
  max?: number;
  disabled?: boolean;
}) {
  const [local, setLocal] = useState(String(value));
  useEffect(() => setLocal(String(value)), [value]);
  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        const n = Number(local);
        if (!Number.isNaN(n) && n !== value) onChange(n);
      }}
      disabled={disabled}
      className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono w-full"
    />
  );
}

function TFSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Field label={label}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-bg-soft border border-border rounded px-3 py-2 text-sm w-full"
      >
        {TIMEFRAMES.map((tf) => (
          <option key={tf} value={tf}>
            {tf}
          </option>
        ))}
      </select>
    </Field>
  );
}
