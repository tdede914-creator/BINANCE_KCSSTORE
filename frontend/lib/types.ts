export type TradingMode = "paper" | "live";

export type SignalSide = "LONG" | "SHORT";

export type SignalStatus =
  | "PENDING"
  | "OPEN"
  | "TP1_HIT"
  | "CLOSED_TP"
  | "CLOSED_SL"
  | "CLOSED_MANUAL"
  | "CANCELLED"
  | "EXPIRED";

export type TrailingMode = "off" | "atr" | "percent";

export interface Config {
  trading_mode: TradingMode;
  scanner_enabled: boolean;
  binance_api_key_masked: string;
  binance_api_configured: boolean;
  binance_testnet: boolean;
  watchlist: string[];
  bias_tf: string;
  setup_tf: string;
  entry_tf: string;
  risk_per_trade_pct: number;
  leverage: number;
  max_concurrent_positions: number;
  ema_fast: number;
  ema_slow: number;
  ema_trigger: number;
  rsi_period: number;
  rsi_long_max: number;
  rsi_short_min: number;
  atr_period: number;
  atr_sl_mult: number;
  rr_tp1: number;
  rr_tp2: number;
  // Trailing stop
  trailing_mode: TrailingMode;
  trailing_activation_rr: number;
  trailing_atr_mult: number;
  trailing_percent: number;
  paper_balance: number;
}

export interface Signal {
  id: number;
  created_at: string;
  symbol: string;
  side: SignalSide;
  status: SignalStatus;
  mode: TradingMode;
  bias_tf: string;
  setup_tf: string;
  entry_tf: string;
  entry_price: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  leverage: number;
  quantity: number;
  risk_amount_usdt: number;
  confidence: number;
  reason: string;
  diagnostics: Record<string, unknown>;
  trade_id: number | null;
}

export interface Trade {
  id: number;
  created_at: string;
  closed_at: string | null;
  signal_id: number | null;
  mode: TradingMode;
  symbol: string;
  side: SignalSide;
  leverage: number;
  entry_price: number;
  quantity: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  exit_price: number | null;
  realized_pnl_usdt: number | null;
  realized_pnl_pct: number | null;
  fee_usdt: number;
  entry_order_id: string | null;
  sl_order_id: string | null;
  tp1_order_id: string | null;
  tp2_order_id: string | null;
  status: SignalStatus;
  notes: string;
  // Trailing state (optional; may be absent on older records)
  trailing_mode?: TrailingMode;
  trailing_active?: boolean;
  highest_price?: number | null;
  lowest_price?: number | null;
}

export interface Stats {
  total_trades: number;
  open_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  total_pnl_usdt: number;
}
