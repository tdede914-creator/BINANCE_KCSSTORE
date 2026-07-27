"use client";
import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { Config } from "@/lib/types";
import { WatchlistEditor } from "@/components/WatchlistEditor";

const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"];

// Popular pairs surfaced as one-click chips in the watchlist editors.
// Users can still type any symbol; these are just quick-adds.
const CRYPTO_SUGGESTIONS = [
  "BTCUSDT",
  "ETHUSDT",
  "SOLUSDT",
  "BNBUSDT",
  "XRPUSDT",
  "DOGEUSDT",
  "ADAUSDT",
  "AVAXUSDT",
  "LINKUSDT",
  "SUIUSDT",
  "1000PEPEUSDT",
  "1000SHIBUSDT",
  "OPUSDT",
  "ARBUSDT",
  "APTUSDT",
];
// Preset watchlists for signals-only (Exness / MT5 / TradingView style).
// Each preset can be applied to the Forex watchlist by clicking its chip.
const SIGNAL_PRESETS: { label: string; hint: string; symbols: string[] }[] = [
  {
    label: "FX majors",
    hint: "The 7 most-liquid FX pairs — tightest spreads on Exness.",
    symbols: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"],
  },
  {
    label: "Metals + oil",
    hint: "Gold, silver, WTI and Brent — same tickers TradingView uses.",
    symbols: ["XAUUSD", "XAGUSD", "USOIL", "UKOIL"],
  },
  {
    label: "Global indices",
    hint: "S&P 500, Nasdaq 100, Dow, DAX, FTSE, Nikkei via Exness-style tickers.",
    symbols: ["US500", "US100", "US30", "GER30", "UK100", "JPN225"],
  },
  {
    label: "Popular US stocks",
    hint: "Blue chips — TwelveData quotes them on the free plan.",
    symbols: ["AAPL", "TSLA", "NVDA", "MSFT", "META", "AMZN", "GOOGL"],
  },
];

const FOREX_SUGGESTIONS = [
  // FX majors
  "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
  // Metals & oil
  "XAGUSD", "USOIL", "UKOIL",
  // Indices
  "US500", "US100", "US30", "GER30", "UK100", "JPN225",
  // Popular stocks
  "AAPL", "TSLA", "NVDA", "MSFT",
];

export default function SettingsPage() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

  // API key form state
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [testnet, setTestnet] = useState(true);
  const [tdKey, setTdKey] = useState("");

  const load = useCallback(async () => {
    try {
      const c = await api.getConfig();
      setCfg(c);
      setTestnet(c.binance_testnet);
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const patch = async (u: Partial<Config>) => {
    setSaving(true);
    try {
      const updated = await api.patchConfig(u);
      setCfg(updated);
      setMsg({ text: "Saved.", kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const saveKeys = async () => {
    if (!apiKey.trim() || !apiSecret.trim()) {
      setMsg({ text: "Both API key and secret are required.", kind: "err" });
      return;
    }
    setSaving(true);
    try {
      const c = await api.saveBinanceKeys({
        api_key: apiKey,
        api_secret: apiSecret,
        testnet,
      });
      setCfg(c);
      setApiKey("");
      setApiSecret("");
      setMsg({ text: "Keys saved (encrypted).", kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const testKeys = async () => {
    setSaving(true);
    try {
      const r = await api.testBinanceKeys();
      setMsg({
        text: `OK — ${r.testnet ? "TESTNET" : "MAINNET"} balance: ${r.balance_usdt} USDT`,
        kind: "ok",
      });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const removeKeys = async () => {
    if (!window.confirm("Remove stored Binance API keys?")) return;
    setSaving(true);
    try {
      const c = await api.deleteBinanceKeys();
      setCfg(c);
      setMsg({ text: "Keys removed.", kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const saveTdKey = async () => {
    if (!tdKey.trim()) {
      setMsg({ text: "TwelveData API key is required.", kind: "err" });
      return;
    }
    setSaving(true);
    try {
      const c = await api.saveTwelvedataKey(tdKey.trim());
      setCfg(c);
      setTdKey("");
      setMsg({ text: "TwelveData key saved.", kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const removeTdKey = async () => {
    if (!window.confirm("Remove TwelveData API key?")) return;
    setSaving(true);
    try {
      const c = await api.deleteTwelvedataKey();
      setCfg(c);
      setMsg({ text: "TwelveData key removed.", kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  // Preset configurations tuned per starting balance. Kept here so it's
  // easy to see the whole recommended set at a glance.
  const applyPreset = async (name: "$10" | "$100" | "$1000") => {
    const presets = {
      "$10": {
        paper_balance: 10,
        risk_per_trade_pct: 5.0,   // $0.50 risk per trade — high but needed
        leverage: 10,
        max_concurrent_positions: 2,
        atr_sl_mult: 0.7,          // slightly wider SL for cheaper tokens
      },
      "$100": {
        paper_balance: 100,
        risk_per_trade_pct: 2.0,
        leverage: 5,
        max_concurrent_positions: 3,
        atr_sl_mult: 0.5,
      },
      "$1000": {
        paper_balance: 1000,
        risk_per_trade_pct: 1.0,
        leverage: 5,
        max_concurrent_positions: 3,
        atr_sl_mult: 0.5,
      },
    }[name];
    if (!window.confirm(`Apply the ${name} modal preset?`)) return;
    setSaving(true);
    try {
      const updated = await api.patchConfig(presets);
      setCfg(updated);
      setMsg({ text: `Applied ${name} preset.`, kind: "ok" });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  const resetPaper = async () => {
    if (
      !window.confirm(
        "This deletes ALL paper trades and paper signals and resets P&L to zero.\n\nLive data is untouched.\n\nContinue?",
      )
    ) {
      return;
    }
    const raw = window.prompt(
      "New paper balance in USDT? (leave blank to keep the current balance)",
      "",
    );
    if (raw === null) return; // user cancelled the prompt
    let newBalance: number | null = null;
    if (raw.trim()) {
      const n = Number(raw);
      if (!Number.isFinite(n) || n <= 0) {
        setMsg({ text: "Invalid balance.", kind: "err" });
        return;
      }
      newBalance = n;
    }
    setSaving(true);
    try {
      const r = await api.resetPaper(newBalance);
      setCfg(r.config);
      setMsg({
        text: `Reset OK — deleted ${r.trades_deleted} trades and ${r.signals_deleted} signals.`,
        kind: "ok",
      });
    } catch (e) {
      setMsg({ text: String(e), kind: "err" });
    } finally {
      setSaving(false);
    }
  };

  if (!cfg) return <div>Loading…</div>;

  return (
    <div className="space-y-8 max-w-3xl">
      <h1 className="text-2xl font-semibold">Settings</h1>

      {msg && (
        <div
          className={clsx(
            "border rounded-lg px-4 py-2 text-sm",
            msg.kind === "ok"
              ? "bg-long/10 border-long/40 text-long"
              : "bg-short/10 border-short/40 text-short",
          )}
        >
          {msg.text}
        </div>
      )}

      {/* Telegram notifications */}
      <TelegramSection
        cfg={cfg}
        saving={saving}
        onPatch={patch}
        onReload={async () => {
          const c = await api.getConfig();
          setCfg(c);
        }}
        onMsg={setMsg}
      />

      {/* Binance API keys */}
      <Section title="Binance API Keys" hint="Keys are encrypted (Fernet) before being stored. Grant Futures permission only. NEVER enable withdrawals.">
        <div className="text-sm mb-3">
          Status:{" "}
          {cfg.binance_api_configured ? (
            <span className="text-long font-mono">
              {cfg.binance_api_key_masked} · {cfg.binance_testnet ? "TESTNET" : "MAINNET"}
            </span>
          ) : (
            <span className="text-muted">Not configured</span>
          )}
        </div>
        <div className="grid gap-3">
          <input
            type="password"
            placeholder="API Key"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono"
          />
          <input
            type="password"
            placeholder="API Secret"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono"
          />
          <label className="flex items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={testnet}
              onChange={(e) => setTestnet(e.target.checked)}
            />
            Use Binance Futures Testnet (recommended for first run)
          </label>
          <div className="flex gap-2">
            <button
              onClick={saveKeys}
              disabled={saving}
              className="px-4 py-2 bg-long/20 hover:bg-long/30 text-long rounded text-sm disabled:opacity-50"
            >
              Save keys
            </button>
            <button
              onClick={testKeys}
              disabled={saving || !cfg.binance_api_configured}
              className="px-4 py-2 bg-bg-soft border border-border rounded text-sm disabled:opacity-50"
            >
              Test keys
            </button>
            <button
              onClick={removeKeys}
              disabled={saving || !cfg.binance_api_configured}
              className="px-4 py-2 bg-short/20 hover:bg-short/30 text-short rounded text-sm disabled:opacity-50 ml-auto"
            >
              Remove keys
            </button>
          </div>
        </div>
      </Section>

      {/* TwelveData (Forex) */}
      <Section
        title="TwelveData API Key (Forex mode)"
        hint="Required to use FOREX market mode. Free tier at twelvedata.com gives 800 requests/day. Key is encrypted before being stored."
      >
        <div className="text-sm mb-3">
          Status:{" "}
          {cfg.twelvedata_configured ? (
            <span className="text-long font-mono">Configured ✓</span>
          ) : (
            <span className="text-muted">Not configured</span>
          )}
        </div>
        <div className="grid gap-3">
          <input
            type="password"
            placeholder="TwelveData API key"
            value={tdKey}
            onChange={(e) => setTdKey(e.target.value)}
            className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono"
          />
          <div className="flex gap-2">
            <button
              onClick={saveTdKey}
              disabled={saving || tdKey.length < 4}
              className="px-4 py-2 bg-long/20 hover:bg-long/30 text-long rounded text-sm disabled:opacity-50"
            >
              Save key
            </button>
            <button
              onClick={removeTdKey}
              disabled={saving || !cfg.twelvedata_configured}
              className="px-4 py-2 bg-short/20 hover:bg-short/30 text-short rounded text-sm disabled:opacity-50 ml-auto"
            >
              Remove key
            </button>
          </div>
        </div>
      </Section>

      {/* Trading */}
      <Section title="Trading">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Mode">
            <select
              value={cfg.trading_mode}
              onChange={(e) => patch({ trading_mode: e.target.value as "paper" | "live" })}
              className="bg-bg-soft border border-border rounded px-3 py-2 text-sm w-full"
              disabled={saving}
            >
              <option value="paper">Paper (virtual)</option>
              <option value="live">Live (real orders)</option>
            </select>
          </Field>
          <Field label="Paper start balance (USDT)">
            <NumberInput
              value={cfg.paper_balance}
              onCommit={(v) => patch({ paper_balance: v })}
              step={10}
              min={5}
            />
          </Field>
          <Field label="Risk per trade (%)">
            <NumberInput
              value={cfg.risk_per_trade_pct}
              onCommit={(v) => patch({ risk_per_trade_pct: v })}
              step={0.1}
              min={0.1}
              max={20}
            />
          </Field>
          <Field label="Leverage">
            <NumberInput
              value={cfg.leverage}
              onCommit={(v) => patch({ leverage: v })}
              step={1}
              min={1}
              max={125}
            />
          </Field>
          <Field label="Max concurrent positions">
            <NumberInput
              value={cfg.max_concurrent_positions}
              onCommit={(v) => patch({ max_concurrent_positions: v })}
              step={1}
              min={1}
              max={20}
            />
          </Field>
        </div>

        {/* Reset + presets — only relevant in paper mode */}
        <div className="mt-4 pt-4 border-t border-border">
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => applyPreset("$10")}
              disabled={saving}
              className="px-3 py-1.5 bg-bg-soft border border-border hover:bg-border rounded text-xs disabled:opacity-50"
              title="Aggressive settings tuned for a $10 stress test"
            >
              Preset: $10 modal
            </button>
            <button
              onClick={() => applyPreset("$100")}
              disabled={saving}
              className="px-3 py-1.5 bg-bg-soft border border-border hover:bg-border rounded text-xs disabled:opacity-50"
              title="Balanced settings for a $100 paper start"
            >
              Preset: $100 modal
            </button>
            <button
              onClick={() => applyPreset("$1000")}
              disabled={saving}
              className="px-3 py-1.5 bg-bg-soft border border-border hover:bg-border rounded text-xs disabled:opacity-50"
              title="Conservative default"
            >
              Preset: $1000 modal
            </button>

            <button
              onClick={resetPaper}
              disabled={saving}
              className="ml-auto px-3 py-1.5 bg-short/20 hover:bg-short/30 text-short border border-short/40 rounded text-xs disabled:opacity-50"
              title="Delete ALL paper trades + signals, reset P&L, optionally set a new starting balance"
            >
              🗑 Reset paper data
            </button>
          </div>
          <p className="text-xs text-muted mt-2">
            Presets update Trading params only (balance / risk% / leverage / max
            positions). Reset also wipes every paper trade and paper signal;
            live data is never touched.
          </p>
        </div>
      </Section>

      {/* Watchlist — Crypto */}
      <Section
        title="Crypto Watchlist"
        hint="Binance USDT-M perpetuals scanned when Market Mode = Crypto. Add symbols one at a time, then click Save changes."
      >
        <WatchlistEditor
          initial={cfg.watchlist}
          placeholder="Add a Binance perp (e.g. BTCUSDT)"
          suggestions={CRYPTO_SUGGESTIONS}
          disabled={saving}
          onSave={async (list) => {
            await patch({ watchlist: list });
          }}
        />
      </Section>

      {/* Watchlist — Forex / signals-only */}
      <Section
        title="Signals-only watchlist (Exness / MT5 / TradingView)"
        hint="Symbols scanned when Market Mode = Forex. No auto-execution — the bot only produces signals; you enter each trade manually in Exness, MT5, or TradingView. Supports FX pairs, metals (XAU/XAG), oil (USOIL/UKOIL), indices (US500/US100/US30/GER30/UK100/JPN225) and US stocks via TwelveData."
      >
        <div className="flex flex-wrap gap-1.5 mb-3">
          <span className="text-xs text-muted self-center mr-1">
            Load preset:
          </span>
          {SIGNAL_PRESETS.map((p) => (
            <button
              key={p.label}
              onClick={() =>
                patch({
                  forex_watchlist: Array.from(
                    new Set([...(cfg.forex_watchlist ?? []), ...p.symbols]),
                  ),
                })
              }
              disabled={saving}
              title={p.hint}
              className="text-[11px] font-semibold px-2.5 py-1 rounded border bg-bg-soft border-border text-muted hover:text-white transition-colors disabled:opacity-50"
            >
              + {p.label}
            </button>
          ))}
        </div>
        <WatchlistEditor
          initial={cfg.forex_watchlist}
          placeholder="Add a symbol (e.g. XAUUSD, US500, USOIL, AAPL)"
          suggestions={FOREX_SUGGESTIONS}
          disabled={saving}
          onSave={async (list) => {
            await patch({ forex_watchlist: list });
          }}
        />
      </Section>

      {/* Timeframes */}
      <Section
        title="Multi-Timeframe"
        hint="Bias = higher TF for direction bias. Setup = mid TF for zones. Entry = lower TF for trigger."
      >
        <div className="grid grid-cols-3 gap-4">
          <TFSelect label="Bias" value={cfg.bias_tf} onChange={(v) => patch({ bias_tf: v })} />
          <TFSelect label="Setup" value={cfg.setup_tf} onChange={(v) => patch({ setup_tf: v })} />
          <TFSelect label="Entry" value={cfg.entry_tf} onChange={(v) => patch({ entry_tf: v })} />
        </div>
      </Section>

      {/* Trailing Stop */}
      <Section
        title="Trailing Stop"
        hint="SL that follows price in the favorable direction (never widens). Activates only after price moves in your favor by 'Activation RR' (in units of initial risk)."
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Field label="Mode">
            <select
              value={cfg.trailing_mode}
              onChange={(e) =>
                patch({ trailing_mode: e.target.value as "off" | "atr" | "percent" })
              }
              className="bg-bg-soft border border-border rounded px-3 py-2 text-sm w-full"
              disabled={saving}
            >
              <option value="off">OFF (fixed SL/BE after TP1)</option>
              <option value="atr">ATR-based</option>
              <option value="percent">Percent-based</option>
            </select>
          </Field>
          <Field label="Activation RR">
            <NumberInput
              value={cfg.trailing_activation_rr}
              onCommit={(v) => patch({ trailing_activation_rr: v })}
              step={0.1}
              min={0}
              max={20}
            />
          </Field>
          <Field label="ATR × multiplier">
            <NumberInput
              value={cfg.trailing_atr_mult}
              onCommit={(v) => patch({ trailing_atr_mult: v })}
              step={0.1}
              min={0.1}
              max={10}
            />
          </Field>
          <Field label="Trail distance (%)">
            <NumberInput
              value={cfg.trailing_percent}
              onCommit={(v) => patch({ trailing_percent: v })}
              step={0.1}
              min={0.05}
              max={20}
            />
          </Field>
        </div>
        <p className="text-xs text-muted mt-3">
          <span className="text-long">Tip:</span> <code>Activation RR = 0</code> trails from entry
          immediately. <code>1.0</code> starts trailing once price hits TP1 level.
          ATR mode uses the ATR captured at signal time (entry TF); PERCENT mode uses live price.
        </p>
      </Section>

      {/* Strategy tuning */}
      <Section title="Strategy parameters">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <Field label="EMA fast">
            <NumberInput value={cfg.ema_fast} onCommit={(v) => patch({ ema_fast: v })} step={1} min={5} max={200} />
          </Field>
          <Field label="EMA slow">
            <NumberInput value={cfg.ema_slow} onCommit={(v) => patch({ ema_slow: v })} step={1} min={20} max={500} />
          </Field>
          <Field label="EMA trigger">
            <NumberInput value={cfg.ema_trigger} onCommit={(v) => patch({ ema_trigger: v })} step={1} min={5} max={100} />
          </Field>
          <Field label="RSI period">
            <NumberInput value={cfg.rsi_period} onCommit={(v) => patch({ rsi_period: v })} step={1} min={5} max={50} />
          </Field>
          <Field label="RSI long max">
            <NumberInput value={cfg.rsi_long_max} onCommit={(v) => patch({ rsi_long_max: v })} step={1} min={50} max={90} />
          </Field>
          <Field label="RSI short min">
            <NumberInput value={cfg.rsi_short_min} onCommit={(v) => patch({ rsi_short_min: v })} step={1} min={10} max={50} />
          </Field>
          <Field label="ATR period">
            <NumberInput value={cfg.atr_period} onCommit={(v) => patch({ atr_period: v })} step={1} min={5} max={50} />
          </Field>
          <Field label="ATR SL multiplier">
            <NumberInput value={cfg.atr_sl_mult} onCommit={(v) => patch({ atr_sl_mult: v })} step={0.1} min={0.1} max={3} />
          </Field>
          <Field label="RR TP1">
            <NumberInput value={cfg.rr_tp1} onCommit={(v) => patch({ rr_tp1: v })} step={0.1} min={0.5} max={10} />
          </Field>
          <Field label="RR TP2">
            <NumberInput value={cfg.rr_tp2} onCommit={(v) => patch({ rr_tp2: v })} step={0.1} min={0.5} max={20} />
          </Field>
        </div>
      </Section>

      {/* Strategies enabled + Range Breakout params */}
      <Section
        title="Active strategies"
        hint="Which strategy engines the scanner runs each tick. Both on = MTF Confluence tried first (trend pullback), then Range Breakout picks up any post-consolidation setups it missed."
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <label className="flex items-start gap-2 p-3 bg-bg-soft border border-border rounded cursor-pointer">
            <input
              type="checkbox"
              checked={cfg.mtf_confluence_enabled}
              onChange={(e) => patch({ mtf_confluence_enabled: e.target.checked })}
              className="mt-0.5"
            />
            <div>
              <div className="text-sm font-semibold">MTF Confluence</div>
              <div className="text-xs text-muted mt-1">
                Trend-following pullback. Needs bias EMA alignment across 4h/1h + entry retest. Best in trending markets.
              </div>
            </div>
          </label>
          <label className="flex items-start gap-2 p-3 bg-bg-soft border border-border rounded cursor-pointer">
            <input
              type="checkbox"
              checked={cfg.range_breakout_enabled}
              onChange={(e) => patch({ range_breakout_enabled: e.target.checked })}
              className="mt-0.5"
            />
            <div>
              <div className="text-sm font-semibold">Range Breakout</div>
              <div className="text-xs text-muted mt-1">
                Post-consolidation directional break. Fires LONG or SHORT depending on which side of the box gives way. Best in ranging → expansion phases.
              </div>
            </div>
          </label>
        </div>
      </Section>

      {cfg.range_breakout_enabled && (
        <Section
          title="Range Breakout parameters"
          hint="Tune what qualifies as a 'consolidation ready to break'. Higher squeeze ratio and shorter lookback = more sensitive."
        >
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <Field
              label="Lookback (bars)"
              hint="Bars used to define the box. 30 @ 5m = 2.5h box; 30 @ 15m = 7.5h."
            >
              <NumberInput
                value={cfg.rb_lookback}
                onCommit={(v) => patch({ rb_lookback: v })}
                step={1}
                min={5}
                max={200}
              />
            </Field>
            <Field
              label="Max range width (%)"
              hint="Range height as % of price — bigger = looser 'consolidation'. 3% is typical."
            >
              <NumberInput
                value={cfg.rb_max_range_pct}
                onCommit={(v) => patch({ rb_max_range_pct: v })}
                step={0.1}
                min={0.1}
                max={20}
              />
            </Field>
            <Field
              label="ATR squeeze ratio"
              hint="ATR now ÷ ATR MA(50). Below this = 'compressed'. 0.7 = 30% below usual volatility."
            >
              <NumberInput
                value={cfg.rb_atr_squeeze_ratio}
                onCommit={(v) => patch({ rb_atr_squeeze_ratio: v })}
                step={0.05}
                min={0.1}
                max={2}
              />
            </Field>
            <Field
              label="Breakout buffer (× ATR)"
              hint="How far past the level the close must be — filters wicks. 0.1 = 10% of ATR."
            >
              <NumberInput
                value={cfg.rb_breakout_buffer}
                onCommit={(v) => patch({ rb_breakout_buffer: v })}
                step={0.05}
                min={0}
                max={2}
              />
            </Field>
            <Field
              label="TP1 (× range height)"
              hint="Measured-move target. 1.0 = same distance as the box was tall."
            >
              <NumberInput
                value={cfg.rb_measured_move_tp1}
                onCommit={(v) => patch({ rb_measured_move_tp1: v })}
                step={0.1}
                min={0.1}
                max={10}
              />
            </Field>
            <Field
              label="TP2 (× range height)"
              hint="Runner target. 1.5 = 1.5× the box height."
            >
              <NumberInput
                value={cfg.rb_measured_move_tp2}
                onCommit={(v) => patch({ rb_measured_move_tp2: v })}
                step={0.1}
                min={0.1}
                max={20}
              />
            </Field>
          </div>
        </Section>
      )}

      {/* Regime / volume filters */}
      <Section
        title="Regime & volume filters"
        hint="Skip signals in ranging markets or on candles without volume conviction. Set ADX min or Volume mult to 0 to disable that filter."
      >
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <Field
            label="ADX period"
            hint="14 = Wilder default; longer = smoother, slower to react"
          >
            <NumberInput
              value={cfg.adx_period}
              onCommit={(v) => patch({ adx_period: v })}
              step={1}
              min={2}
              max={100}
            />
          </Field>
          <Field
            label="ADX min (trend strength)"
            hint="20 = classic no-chop threshold; try 25 to be stricter; 0 to disable"
          >
            <NumberInput
              value={cfg.adx_min}
              onCommit={(v) => patch({ adx_min: v })}
              step={1}
              min={0}
              max={60}
            />
          </Field>
          <Field
            label="Volume multiplier"
            hint="Entry volume ≥ mult × MA20. 1.2 = mild filter, 1.5 = spike required, 0 = disable"
          >
            <NumberInput
              value={cfg.volume_mult}
              onCommit={(v) => patch({ volume_mult: v })}
              step={0.1}
              min={0}
              max={5}
            />
          </Field>
        </div>
      </Section>
    </div>
  );
}

/* ---------- small building blocks ---------- */

// ---------------------------------------------------------------------------
// Telegram Notifications
//
// Users only need:
//   1. A bot token from @BotFather in Telegram
//   2. Their chat ID (start a conversation with the bot, then hit
//      https://api.telegram.org/bot<TOKEN>/getUpdates and copy the
//      "chat":{"id": <this>} number)
//
// After that they enable the master toggle + pick which classes of
// events they want to receive (signals, trade updates, hourly balance).
// A "Send test message" button confirms everything works before
// production.
// ---------------------------------------------------------------------------
function TelegramSection({
  cfg,
  saving,
  onPatch,
  onReload,
  onMsg,
}: {
  cfg: Config;
  saving: boolean;
  onPatch: (u: Partial<Config>) => Promise<void>;
  onReload: () => Promise<void>;
  onMsg: (m: { text: string; kind: "ok" | "err" } | null) => void;
}) {
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState(cfg.telegram_chat_id ?? "");
  const [testing, setTesting] = useState(false);
  const [detecting, setDetecting] = useState(false);

  const detectChatId = async () => {
    setDetecting(true);
    try {
      const r = await api.detectTelegramChatId();
      if (r.ok && r.chat_id) {
        setChatId(r.chat_id);
        onMsg({ text: `Chat ID auto-detected: ${r.chat_id}`, kind: "ok" });
        await onReload();
      } else {
        onMsg({ text: r.error ?? "Could not detect chat ID.", kind: "err" });
      }
    } catch (e) {
      onMsg({ text: String(e), kind: "err" });
    } finally {
      setDetecting(false);
    }
  };

  // Reset the chat_id field if the config reloaded from server with a
  // different value (e.g. right after a Save from another tab).
  useEffect(() => {
    setChatId(cfg.telegram_chat_id ?? "");
  }, [cfg.telegram_chat_id]);

  const saveToken = async () => {
    if (!token.trim()) {
      onMsg({ text: "Paste your bot token first.", kind: "err" });
      return;
    }
    try {
      await api.setTelegramToken(token.trim());
      setToken("");
      onMsg({ text: "Bot token saved (encrypted).", kind: "ok" });
      await onReload();
    } catch (e) {
      onMsg({ text: String(e), kind: "err" });
    }
  };

  const clearToken = async () => {
    if (!confirm("Clear the stored Telegram bot token?")) return;
    try {
      await api.deleteTelegramToken();
      onMsg({ text: "Bot token cleared.", kind: "ok" });
      await onReload();
    } catch (e) {
      onMsg({ text: String(e), kind: "err" });
    }
  };

  const saveChatId = async () => {
    try {
      await onPatch({ telegram_chat_id: chatId.trim() });
      onMsg({ text: "Chat ID saved.", kind: "ok" });
    } catch (e) {
      onMsg({ text: String(e), kind: "err" });
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const r = await api.testTelegram();
      if (r.ok) {
        onMsg({ text: "Test message delivered.", kind: "ok" });
      } else {
        onMsg({ text: r.error || "Telegram API rejected the request.", kind: "err" });
      }
    } catch (e) {
      onMsg({ text: String(e), kind: "err" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Section
      title="Telegram Notifications"
      hint="Push signals, trade updates, and an hourly wallet snapshot to a Telegram chat. Bot token is stored encrypted. No incoming commands — this is one-way, bot → you."
    >
      <div className="space-y-4">
        {/* Setup guide */}
        <details className="text-xs text-muted bg-bg-soft border border-border rounded p-3">
          <summary className="cursor-pointer text-white font-semibold">
            How to get bot token + chat ID (3 min)
          </summary>
          <ol className="mt-2 space-y-1 list-decimal pl-4 leading-relaxed">
            <li>
              Open Telegram, search{" "}
              <code className="text-white">@BotFather</code>, send{" "}
              <code className="text-white">/newbot</code>. Give it a name
              and a username ending in <code className="text-white">bot</code>.
              You'll get a token like{" "}
              <code className="text-white">123456:AAE...xyz</code>. Paste it below.
            </li>
            <li>
              Open a chat with your new bot and send{" "}
              <code className="text-white">/start</code>. Anything is fine
              — the bot just needs to have received one message from you.
            </li>
            <li>
              <strong className="text-white">Getting chat ID — easiest:</strong>{" "}
              save the bot token below, then click{" "}
              <em>Auto-detect chat ID</em>. It reads your latest message
              to the bot and fills the field for you.
              <div className="mt-1 text-[10px] text-muted/70">
                Alternatives if that fails: (a) send{" "}
                <code className="text-white">/start</code> to{" "}
                <code className="text-white">@userinfobot</code> — it
                replies with your user ID, which IS your chat ID for
                direct messages. (b) open{" "}
                <code className="text-white">
                  https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates
                </code>{" "}
                and copy the number from{" "}
                <code className="text-white">"chat":&#123;"id": ...&#125;</code>.
              </div>
            </li>
            <li>Enable notifications and click <em>Send test</em>.</li>
          </ol>
        </details>

        {/* Bot token */}
        <div>
          <div className="text-xs text-muted mb-1">
            Bot token{" "}
            <span
              className={clsx(
                "ml-2 px-1.5 py-0.5 rounded text-[10px]",
                cfg.telegram_configured
                  ? "bg-long/20 text-long"
                  : "bg-yellow-500/20 text-yellow-400",
              )}
            >
              {cfg.telegram_configured ? "configured" : "not set"}
            </span>
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              disabled={saving}
              placeholder={
                cfg.telegram_configured
                  ? "•••••••• (paste a new token to replace)"
                  : "123456:AAE..."
              }
              className="flex-1 bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono"
            />
            <button
              onClick={saveToken}
              disabled={saving || !token.trim()}
              className="px-4 py-2 bg-long/20 hover:bg-long/30 text-long border border-long/40 rounded text-sm disabled:opacity-40"
            >
              Save
            </button>
            {cfg.telegram_configured && (
              <button
                onClick={clearToken}
                disabled={saving}
                className="px-3 py-2 bg-bg-soft hover:bg-border text-muted border border-border rounded text-sm"
                title="Delete stored token"
              >
                Clear
              </button>
            )}
          </div>
        </div>

        {/* Chat ID */}
        <div>
          <div className="text-xs text-muted mb-1 flex items-center justify-between">
            <span>Chat ID (same number as your Telegram user ID for DMs)</span>
          </div>
          <div className="flex gap-2">
            <input
              type="text"
              value={chatId}
              onChange={(e) => setChatId(e.target.value)}
              disabled={saving}
              placeholder="e.g. 987654321"
              className="flex-1 bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono"
            />
            <button
              onClick={saveChatId}
              disabled={saving || chatId === cfg.telegram_chat_id}
              className="px-4 py-2 bg-bg-soft hover:bg-border text-white border border-border rounded text-sm disabled:opacity-40"
            >
              Save
            </button>
            <button
              onClick={detectChatId}
              disabled={saving || detecting || !cfg.telegram_configured}
              title="Reads the newest message received by your bot and copies its sender chat ID here"
              className="px-3 py-2 bg-long/15 hover:bg-long/25 text-long border border-long/40 rounded text-sm disabled:opacity-40 whitespace-nowrap"
            >
              {detecting ? "Detecting…" : "Auto-detect"}
            </button>
          </div>
          <div className="text-[10px] text-muted/70 mt-1">
            Auto-detect requires: bot token saved above, and you must
            have sent at least one message (e.g. <code className="text-white">/start</code>) to your bot on Telegram.
          </div>
        </div>

        {/* Master enable + test */}
        <div className="flex flex-wrap items-center gap-3 pt-2 border-t border-border/50">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={cfg.telegram_enabled}
              onChange={(e) => onPatch({ telegram_enabled: e.target.checked })}
              disabled={saving || !cfg.telegram_configured || !cfg.telegram_chat_id}
            />
            <span className="text-sm">
              Enable Telegram notifications
              {(!cfg.telegram_configured || !cfg.telegram_chat_id) && (
                <span className="ml-2 text-[10px] text-muted">
                  (set token + chat ID first)
                </span>
              )}
            </span>
          </label>
          <button
            onClick={sendTest}
            disabled={
              saving ||
              testing ||
              !cfg.telegram_configured ||
              !cfg.telegram_chat_id
            }
            className="px-3 py-1.5 bg-bg-soft hover:bg-border text-white border border-border rounded text-xs disabled:opacity-40"
          >
            {testing ? "Sending…" : "Send test message"}
          </button>
        </div>

        {/* Per-channel opt-ins — only meaningful when the master is on. */}
        {cfg.telegram_enabled && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 pt-3 border-t border-border/50">
            <label className="flex items-start gap-2 p-3 bg-bg-soft border border-border rounded cursor-pointer">
              <input
                type="checkbox"
                checked={cfg.telegram_notify_signals}
                onChange={(e) =>
                  onPatch({ telegram_notify_signals: e.target.checked })
                }
                className="mt-0.5"
              />
              <div>
                <div className="text-sm font-semibold">Signals</div>
                <div className="text-xs text-muted mt-1">
                  Every time a strategy fires (MTF Confluence, Range Breakout, …).
                </div>
              </div>
            </label>
            <label className="flex items-start gap-2 p-3 bg-bg-soft border border-border rounded cursor-pointer">
              <input
                type="checkbox"
                checked={cfg.telegram_notify_trades}
                onChange={(e) =>
                  onPatch({ telegram_notify_trades: e.target.checked })
                }
                className="mt-0.5"
              />
              <div>
                <div className="text-sm font-semibold">Trade updates</div>
                <div className="text-xs text-muted mt-1">
                  TP1 hit, TP2 hit, SL hit, manual close — the whole life cycle.
                </div>
              </div>
            </label>
            <label className="flex items-start gap-2 p-3 bg-bg-soft border border-border rounded cursor-pointer">
              <input
                type="checkbox"
                checked={cfg.telegram_notify_hourly_balance}
                onChange={(e) =>
                  onPatch({ telegram_notify_hourly_balance: e.target.checked })
                }
                className="mt-0.5"
              />
              <div>
                <div className="text-sm font-semibold">
                  Wallet snapshot every {cfg.telegram_balance_interval_min}m
                </div>
                <div className="text-xs text-muted mt-1">
                  Real balance from Binance in LIVE mode, computed paper
                  equity in PAPER mode.
                </div>
              </div>
            </label>
          </div>
        )}

        {cfg.telegram_enabled && cfg.telegram_notify_hourly_balance && (
          <div className="pt-1">
            <Field
              label="Balance snapshot interval (minutes)"
              hint="Min 5, max 1440 (once a day). Default 60."
            >
              <NumberInput
                value={cfg.telegram_balance_interval_min}
                onCommit={(v) => onPatch({ telegram_balance_interval_min: v })}
                step={5}
                min={5}
                max={1440}
              />
            </Field>
          </div>
        )}
      </div>
    </Section>
  );
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-bg-card border border-border rounded-lg p-5">
      <div className="mb-3">
        <h2 className="font-semibold">{title}</h2>
        {hint && <p className="text-xs text-muted mt-1">{hint}</p>}
      </div>
      {children}
    </div>
  );
}

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
      <div className="text-xs text-muted mb-1">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted/70 mt-1 leading-tight">{hint}</div>}
    </label>
  );
}

function NumberInput({
  value,
  onCommit,
  step,
  min,
  max,
}: {
  value: number;
  onCommit: (v: number) => void;
  step?: number;
  min?: number;
  max?: number;
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
        if (!Number.isNaN(n) && n !== value) onCommit(n);
      }}
      className="bg-bg-soft border border-border rounded px-3 py-2 text-sm font-mono w-full"
    />
  );
}

function TextInput({
  value,
  onCommit,
  placeholder,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
}) {
  const [local, setLocal] = useState(value);
  useEffect(() => setLocal(value), [value]);
  return (
    <input
      type="text"
      value={local}
      placeholder={placeholder}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={() => {
        if (local !== value) onCommit(local);
      }}
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
