"use client";
import { useState } from "react";
import clsx from "clsx";
import type { Signal } from "@/lib/types";
import { classFor, formatDate, formatPrice, formatUsdt } from "@/lib/format";

// ---------------------------------------------------------------------------
// TradingView deep link + Exness ticker resolution.
//
// Some symbols we route via TwelveData use "compact" internal codes that
// TradingView doesn't recognise directly (e.g. we store SPX but TradingView
// wants OANDA:SPX500USD). We keep an override map so the "Chart on
// TradingView" button always opens a chart the user recognises.
// ---------------------------------------------------------------------------
const TV_SYMBOL: Record<string, string> = {
  // Indices — use OANDA feed since Exness spreads mirror it closely.
  SPX: "OANDA:SPX500USD",
  US500: "OANDA:SPX500USD",
  NDX: "OANDA:NAS100USD",
  US100: "OANDA:NAS100USD",
  NAS100: "OANDA:NAS100USD",
  DJI: "OANDA:US30USD",
  US30: "OANDA:US30USD",
  DAX: "OANDA:DE30EUR",
  GER30: "OANDA:DE30EUR",
  GER40: "OANDA:DE30EUR",
  FTSE: "OANDA:UK100GBP",
  UK100: "OANDA:UK100GBP",
  N225: "OANDA:JP225USD",
  JPN225: "OANDA:JP225USD",
  // Energies
  WTI: "OANDA:WTICOUSD",
  USOIL: "OANDA:WTICOUSD",
  BRENT: "OANDA:BCOUSD",
  UKOIL: "OANDA:BCOUSD",
  // Metals
  XAUUSD: "OANDA:XAUUSD",
  XAGUSD: "OANDA:XAGUSD",
  GOLD: "OANDA:XAUUSD",
  SILVER: "OANDA:XAGUSD",
};

function tradingViewSymbol(symbol: string): string {
  const s = symbol.toUpperCase();
  if (TV_SYMBOL[s]) return TV_SYMBOL[s];
  // USDT perpetuals live on Binance
  if (s.endsWith("USDT")) return `BINANCE:${s}.P`;
  // Slash-style FX (EUR/USD) — collapse for TradingView (which wants EURUSD)
  const compact = s.replace("/", "");
  // Six-char FX pair → OANDA
  if (/^[A-Z]{6}$/.test(compact)) return `OANDA:${compact}`;
  // Fallback — let TradingView figure it out
  return compact;
}

function tradingViewURL(symbol: string): string {
  return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(
    tradingViewSymbol(symbol),
  )}`;
}

/**
 * Format a signal as human-readable text for pasting into Telegram / notepad /
 * MT5 order dialog. Matches the shape of typical crypto/forex "call" posts.
 */
function formatSignalForClipboard(signal: Signal): string {
  const emoji = signal.side === "LONG" ? "🟢 LONG" : "🔴 SHORT";
  const lines = [
    `${emoji} ${signal.symbol} · ${signal.entry_tf}`,
    "",
    `📍 Entry:  ${formatPrice(signal.entry_price)}`,
    `❌ SL:     ${formatPrice(signal.stop_loss)}`,
    `🎯 TP1:    ${formatPrice(signal.take_profit_1)}`,
    `🎯 TP2:    ${formatPrice(signal.take_profit_2)}`,
    ...(signal.take_profit_3
      ? [`🎯 TP3:    ${formatPrice(signal.take_profit_3)}`]
      : []),
    "",
    `Leverage:   ${signal.leverage}x`,
    `Confidence: ${(signal.confidence * 100).toFixed(0)}%`,
  ];
  if (signal.reason) {
    lines.push("", `Setup: ${signal.reason}`);
  }
  return lines.join("\n");
}

export function SignalCard({
  signal,
  onSymbolClick,
}: {
  signal: Signal;
  onSymbolClick?: (symbol: string) => void;
}) {
  const long = signal.side === "LONG";
  const [copied, setCopied] = useState(false);

  const doCopy = async () => {
    try {
      await navigator.clipboard.writeText(formatSignalForClipboard(signal));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Older browsers / mobile in-app webviews may not have clipboard API.
      // Fall back to a select-and-prompt so the user can copy manually.
      window.prompt("Copy this signal:", formatSignalForClipboard(signal));
    }
  };

  return (
    <div className="bg-bg-card border border-border rounded-lg overflow-hidden">
      <div
        className={clsx(
          "px-4 py-2 flex items-center justify-between text-sm border-b border-border",
          long ? "bg-long/10" : "bg-short/10",
        )}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className={clsx("font-semibold text-lg", classFor(signal.side))}>
            {signal.side}
          </span>
          {onSymbolClick ? (
            <button
              onClick={() => onSymbolClick(signal.symbol)}
              className="font-mono hover:underline"
              title="Show on chart"
            >
              {signal.symbol}
            </button>
          ) : (
            <span className="font-mono">{signal.symbol}</span>
          )}
          <span className="text-xs text-muted px-2 py-0.5 rounded bg-bg-soft">
            {signal.entry_tf}
          </span>
          {signal.strategy && signal.strategy !== "mtf_confluence" && (
            <span
              className="text-[10px] text-muted px-1.5 py-0.5 rounded bg-bg-soft border border-border"
              title="Which strategy fired this signal"
            >
              {signal.strategy}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-muted whitespace-nowrap">
          <span>conf {(signal.confidence * 100).toFixed(0)}%</span>
          <span>{formatDate(signal.created_at)}</span>
        </div>
      </div>

      <div
        className={clsx(
          "p-4 grid gap-3 text-sm font-mono",
          signal.take_profit_3 ? "grid-cols-5" : "grid-cols-4",
        )}
      >
        <PriceCell label="Entry" value={signal.entry_price} />
        <PriceCell label="SL" value={signal.stop_loss} tone="short" />
        <PriceCell label="TP1" value={signal.take_profit_1} tone="long" />
        <PriceCell label="TP2" value={signal.take_profit_2} tone="long" />
        {signal.take_profit_3 != null && signal.take_profit_3 > 0 && (
          <PriceCell label="TP3" value={signal.take_profit_3} tone="long" />
        )}
      </div>

      {/* Action bar: Exness/TradingView helpers.
          Rendered for every signal but especially useful for signals-only
          (forex / indices / metals) where the user must manually execute
          on Exness or MT5. */}
      <div className="px-4 py-2 border-t border-border flex items-center gap-2 flex-wrap">
        {signal.status === "PENDING" && (
          <button
            onClick={async () => {
              if (!confirm("Cancel this pending signal? Auto-execute will be skipped.")) return;
              try {
                await (await import("@/lib/api")).api.cancelSignal(signal.id);
              } catch (e) {
                alert(`Cancel failed: ${e}`);
              }
            }}
            className="text-xs font-semibold px-2.5 py-1 rounded border bg-short/15 border-short/40 text-short hover:bg-short/25"
            title="Skip the delayed auto-execute for this signal"
          >
            ✗ Cancel
          </button>
        )}
        <button
          onClick={doCopy}
          className={clsx(
            "text-xs font-semibold px-2.5 py-1 rounded border transition-colors",
            copied
              ? "bg-long/20 border-long/50 text-long"
              : "bg-bg-soft border-border text-muted hover:text-white",
          )}
          title="Copy formatted signal for Telegram / MT5 / notepad"
        >
          {copied ? "✓ Copied" : "📋 Copy"}
        </button>
        <a
          href={tradingViewURL(signal.symbol)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs font-semibold px-2.5 py-1 rounded border bg-bg-soft border-border text-muted hover:text-white transition-colors"
          title="Open the chart on TradingView so you can execute in Exness / MT5 alongside"
        >
          🔗 TradingView
        </a>
        <span className="text-[10px] text-muted/60 ml-1">
          Signals-only — no auto-execution
        </span>
      </div>

      <div className="px-4 py-3 border-t border-border text-xs text-muted flex items-center justify-between">
        <span>
          Qty {formatPrice(signal.quantity)} · Risk ${formatUsdt(signal.risk_amount_usdt)} ·{" "}
          {signal.leverage}x
        </span>
        <span
          className={clsx(
            "px-2 py-0.5 rounded",
            signal.status === "OPEN"
              ? "bg-long/20 text-long"
              : signal.status === "PENDING"
                ? "bg-yellow-500/20 text-yellow-400"
                : "bg-bg-soft",
          )}
        >
          {signal.status}
        </span>
      </div>

      {signal.reason && (
        <div className="px-4 py-2 border-t border-border text-xs text-muted">
          {signal.reason}
        </div>
      )}
    </div>
  );
}

function PriceCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "long" | "short";
}) {
  return (
    <div>
      <div className="text-[10px] text-muted uppercase">{label}</div>
      <div
        className={clsx(
          "text-sm",
          tone === "long" && "text-long",
          tone === "short" && "text-short",
        )}
      >
        {formatPrice(value)}
      </div>
    </div>
  );
}
