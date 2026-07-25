import clsx from "clsx";
import type { Signal } from "@/lib/types";
import { classFor, formatDate, formatPrice, formatUsdt } from "@/lib/format";

export function SignalCard({
  signal,
  onSymbolClick,
}: {
  signal: Signal;
  onSymbolClick?: (symbol: string) => void;
}) {
  const long = signal.side === "LONG";
  return (
    <div className="bg-bg-card border border-border rounded-lg overflow-hidden">
      <div
        className={clsx(
          "px-4 py-2 flex items-center justify-between text-sm border-b border-border",
          long ? "bg-long/10" : "bg-short/10",
        )}
      >
        <div className="flex items-center gap-2">
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
        </div>
        <div className="flex items-center gap-3 text-xs text-muted">
          <span>conf {(signal.confidence * 100).toFixed(0)}%</span>
          <span>{formatDate(signal.created_at)}</span>
        </div>
      </div>

      <div className="p-4 grid grid-cols-4 gap-3 text-sm font-mono">
        <PriceCell label="Entry" value={signal.entry_price} />
        <PriceCell label="SL" value={signal.stop_loss} tone="short" />
        <PriceCell label="TP1" value={signal.take_profit_1} tone="long" />
        <PriceCell label="TP2" value={signal.take_profit_2} tone="long" />
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
