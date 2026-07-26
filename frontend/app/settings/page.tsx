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
const FOREX_SUGGESTIONS = [
  "XAUUSD",
  "EURUSD",
  "GBPUSD",
  "GBPJPY",
  "AUDUSD",
  "USDJPY",
  "USDCAD",
  "NZDUSD",
  "EURJPY",
  "AUDJPY",
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

      {/* Watchlist — Forex */}
      <Section
        title="Forex Watchlist"
        hint="FX / commodity pairs (MT5-style, no slash) scanned when Market Mode = Forex. TwelveData supports popular pairs like XAUUSD, EURUSD, GBPUSD, GBPJPY, AUDUSD, USDJPY, USDCAD, NZDUSD, and more."
      >
        <WatchlistEditor
          initial={cfg.forex_watchlist}
          placeholder="Add an FX pair (e.g. XAUUSD)"
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs text-muted mb-1">{label}</div>
      {children}
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
