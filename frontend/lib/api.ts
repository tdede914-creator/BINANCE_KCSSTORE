import type { Config, Signal, Stats, Trade, TradingMode } from "./types";

export interface Candle {
  time: number; // unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface KlinesResponse {
  symbol: string;
  interval: string;
  candles: Candle[];
}

const API_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // ----- Config -----
  getConfig: () => request<Config>("/api/config"),
  patchConfig: (patch: Partial<Config>) =>
    request<Config>("/api/config", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  // ----- Binance keys -----
  saveBinanceKeys: (payload: {
    api_key: string;
    api_secret: string;
    testnet: boolean;
  }) =>
    request<Config>("/api/config/binance-keys", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteBinanceKeys: () =>
    request<Config>("/api/config/binance-keys", { method: "DELETE" }),
  testBinanceKeys: () =>
    request<{ ok: boolean; balance_usdt: number; testnet: boolean }>(
      "/api/config/binance-keys/test",
      { method: "POST" },
    ),

  // ----- Signals -----
  listSignals: (params: { limit?: number; symbol?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.symbol) qs.set("symbol", params.symbol);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<Signal[]>(`/api/signals${suffix}`);
  },

  // ----- Trades -----
  listTrades: (params: {
    limit?: number;
    mode?: TradingMode;
    symbol?: string;
    open_only?: boolean;
  } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.mode) qs.set("mode", params.mode);
    if (params.symbol) qs.set("symbol", params.symbol);
    if (params.open_only) qs.set("open_only", "true");
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<Trade[]>(`/api/trades${suffix}`);
  },
  tradeStats: (mode?: TradingMode) => {
    const qs = mode ? `?mode=${mode}` : "";
    return request<Stats>(`/api/trades/stats/summary${qs}`);
  },
  closeTrade: (id: number) =>
    request<Trade>(`/api/trades/${id}/close`, { method: "POST" }),

  // ----- Market data -----
  getKlines: (params: { symbol: string; interval?: string; limit?: number }) => {
    const qs = new URLSearchParams({ symbol: params.symbol });
    if (params.interval) qs.set("interval", params.interval);
    if (params.limit) qs.set("limit", String(params.limit));
    return request<KlinesResponse>(`/api/market/klines?${qs.toString()}`);
  },
  getTicker: (symbol: string) =>
    request<{ symbol: string; price: number }>(
      `/api/market/ticker?symbol=${encodeURIComponent(symbol)}`,
    ),
};

export { API_URL };
