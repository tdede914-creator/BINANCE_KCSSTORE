"use client";
import clsx from "clsx";
import { useState } from "react";
import type { Trade } from "@/lib/types";
import { api } from "@/lib/api";
import { classFor, formatDate, formatPrice, formatUsdt, pnlClass } from "@/lib/format";

export function TradeRow({
  trade,
  onChanged,
  onSymbolClick,
}: {
  trade: Trade;
  onChanged?: () => void;
  onSymbolClick?: (symbol: string) => void;
}) {
  const [closing, setClosing] = useState(false);
  const open = trade.status === "OPEN" || trade.status === "TP1_HIT";

  const handleClose = async () => {
    if (!window.confirm(`Close ${trade.symbol} ${trade.side} at market?`)) return;
    setClosing(true);
    try {
      await api.closeTrade(trade.id);
      onChanged?.();
    } catch (e) {
      alert(`Failed to close: ${e}`);
    } finally {
      setClosing(false);
    }
  };

  return (
    <tr className="border-b border-border/50 hover:bg-bg-soft/50">
      <td className="py-2 px-3 font-mono text-xs text-muted">
        {formatDate(trade.created_at)}
      </td>
      <td className="py-2 px-3">
        {onSymbolClick ? (
          <button
            onClick={() => onSymbolClick(trade.symbol)}
            className="font-mono hover:underline"
            title="Show on chart"
          >
            {trade.symbol}
          </button>
        ) : (
          <span className="font-mono">{trade.symbol}</span>
        )}
      </td>
      <td className={clsx("py-2 px-3 font-semibold", classFor(trade.side))}>
        {trade.side}
      </td>
      <td className="py-2 px-3 text-xs">
        <span className="px-2 py-0.5 rounded bg-bg-soft">{trade.mode}</span>
      </td>
      <td className="py-2 px-3 font-mono">{formatPrice(trade.entry_price)}</td>
      <td className="py-2 px-3 font-mono text-short">
        {formatPrice(trade.stop_loss)}
      </td>
      <td className="py-2 px-3 font-mono text-long">
        {formatPrice(trade.take_profit_1)} / {formatPrice(trade.take_profit_2)}
      </td>
      <td className="py-2 px-3 font-mono">{formatPrice(trade.quantity)}</td>
      <td className={clsx("py-2 px-3 font-mono", pnlClass(trade.realized_pnl_usdt))}>
        {trade.realized_pnl_usdt !== null
          ? `${trade.realized_pnl_usdt >= 0 ? "+" : ""}${formatUsdt(trade.realized_pnl_usdt)}`
          : "—"}
      </td>
      <td className="py-2 px-3 text-xs">
        <span
          className={clsx(
            "px-2 py-0.5 rounded",
            trade.status === "OPEN" && "bg-long/20 text-long",
            trade.status === "TP1_HIT" && "bg-yellow-500/20 text-yellow-400",
            trade.status === "CLOSED_TP" && "bg-long/20 text-long",
            trade.status === "CLOSED_SL" && "bg-short/20 text-short",
            trade.status === "CLOSED_MANUAL" && "bg-bg-soft",
          )}
        >
          {trade.status}
        </span>
      </td>
      <td className="py-2 px-3 text-right">
        {open && (
          <button
            onClick={handleClose}
            disabled={closing}
            className="px-2 py-1 text-xs bg-short/20 hover:bg-short/30 text-short rounded disabled:opacity-50"
          >
            {closing ? "…" : "Close"}
          </button>
        )}
      </td>
    </tr>
  );
}
