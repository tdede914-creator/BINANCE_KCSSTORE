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

export interface ChannelPoint {
  time: number; // unix seconds
  price: number;
}

export interface ChannelLine {
  start: ChannelPoint;
  end: ChannelPoint;
}

export interface ChannelResponse {
  symbol: string;
  interval: string;
  lookback: number;
  upper: ChannelLine;
  midline: ChannelLine;
  lower: ChannelLine;
  slope_per_bar: number;
  slope_pct_total: number;
  stddev: number;
  width_pct: number;
  algorithm: "pivot" | "regression";
}

export interface SRLevel {
  price: number;
  kind: "support" | "resistance";
  touches: number;
  last_touch_time: number; // unix seconds
}

export interface SRResponse {
  symbol: string;
  interval: string;
  lookback: number;
  current_price: number;
  levels: SRLevel[];
}

export interface SymbolDiag {
  symbol: string;
  stage:
    | "warmup"
    | "bias"
    | "setup"
    | "trigger"
    | "fired"
    | "risk_rejected"
    | "executed"
    | "exec_failed"
    | "unknown";
  reason: string | null;
  ts: string | null;
  market: string | null;
  bias_side: string | null;
}

export interface ScannerDiagnostics {
  last_tick_ts: string | null;
  last_tick_market: string | null;
  symbols: SymbolDiag[];
}

/**
 * Resolve the backend URL.
 *
 * NEXT_PUBLIC_API_URL is baked into the client bundle at build time. When
 * unset (the usual case for self-hosted deployments), we derive the URL
 * from the page's own host at runtime so the frontend can be accessed
 * from any IP / domain without a rebuild. The port matches the one the
 * backend service listens on in docker-compose (8000).
 */
function resolveApiUrl(): string {
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return "http://localhost:8000";
}

const API_URL = resolveApiUrl();

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  // Only add Content-Type when there's a body. Bodyless POSTs (e.g.
  // /api/trades/:id/close) previously included Content-Type: application/json
  // which triggered a CORS preflight for no reason and made the browser
  // more likely to surface an unhelpful 'TypeError: Failed to fetch'.
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  if (init?.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
  } catch (e) {
    // Network-level failure: connection refused, DNS, timeout, CORS-block…
    // Give the caller something more useful than a bare 'Failed to fetch'.
    throw new Error(
      `Network error contacting ${API_URL}${path}: ${
        e instanceof Error ? e.message : String(e)
      }`,
    );
  }
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

  // ----- TwelveData (Forex) -----
  saveTwelvedataKey: (api_key: string) =>
    request<Config>("/api/config/twelvedata-key", {
      method: "POST",
      body: JSON.stringify({ api_key }),
    }),
  deleteTwelvedataKey: () =>
    request<Config>("/api/config/twelvedata-key", { method: "DELETE" }),

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

  // ----- Scanner introspection -----
  getScannerDiagnostics: () =>
    request<ScannerDiagnostics>("/api/scanner/diagnostics"),

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
  getChannel: (params: {
    symbol: string;
    interval: string;
    lookback?: number;
  }) => {
    const qs = new URLSearchParams({
      symbol: params.symbol,
      interval: params.interval,
    });
    if (params.lookback) qs.set("lookback", String(params.lookback));
    return request<ChannelResponse>(`/api/market/channel?${qs.toString()}`);
  },
  getSR: (params: {
    symbol: string;
    interval: string;
    lookback?: number;
    maxLevels?: number;
    minTouches?: number;
  }) => {
    const qs = new URLSearchParams({
      symbol: params.symbol,
      interval: params.interval,
    });
    if (params.lookback) qs.set("lookback", String(params.lookback));
    if (params.maxLevels) qs.set("max_levels", String(params.maxLevels));
    if (params.minTouches) qs.set("min_touches", String(params.minTouches));
    return request<SRResponse>(`/api/market/sr?${qs.toString()}`);
  },
};

export { API_URL };
