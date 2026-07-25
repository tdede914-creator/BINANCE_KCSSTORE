"use client";

import { useEffect, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";

import { api, type Candle } from "@/lib/api";
import { ema, emaStep } from "@/lib/indicators";
import type { Trade } from "@/lib/types";

// --------------------------------------------------------------------------
// Public props
// --------------------------------------------------------------------------

export interface ChannelInfo {
  slope_pct_total: number;
  width_pct: number;
  lookback: number;
}

export interface PriceChartProps {
  symbol: string;
  interval: string;
  emaFast: number;
  emaSlow: number;
  emaTrigger: number;
  /** Open trades to render as price lines. Only those matching `symbol` are drawn. */
  openTrades?: Trade[];
  testnet?: boolean;
  height?: number;
  /** When true, fetch and draw an auto parallel channel over the chart. */
  showChannel?: boolean;
  /** Number of candles the regression is fit over. Default 100. */
  channelLookback?: number;
  /** Callback with slope% / width% for the parent to show a badge. */
  onChannelInfo?: (info: ChannelInfo | null) => void;
}

// --------------------------------------------------------------------------
// Binance public WebSocket URLs. These are called *directly from the browser*
// (Binance allows CORS on public streams), which keeps latency low and avoids
// running a relay through our backend.
// --------------------------------------------------------------------------

const WS_MAINNET = "wss://fstream.binance.com/ws";
const WS_TESTNET = "wss://stream.binancefuture.com/ws";

// --------------------------------------------------------------------------
// Component
// --------------------------------------------------------------------------

export function PriceChart({
  symbol,
  interval,
  emaFast,
  emaSlow,
  emaTrigger,
  openTrades = [],
  testnet = false,
  height = 460,
  showChannel = false,
  channelLookback = 100,
  onChannelInfo,
}: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaFastSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const emaSlowSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const emaTriggerSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);

  // Channel series refs (created lazily when showChannel toggles on).
  const channelUpperRef = useRef<ISeriesApi<"Line"> | null>(null);
  const channelMidRef = useRef<ISeriesApi<"Line"> | null>(null);
  const channelLowerRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Keep the onChannelInfo callback in a ref so we don't have to include it
  // in the effect's deps (avoids re-fetching when the parent re-renders).
  const onChannelInfoRef = useRef(onChannelInfo);
  onChannelInfoRef.current = onChannelInfo;

  // Live-updated EMA state (mutable so we don't recompute the whole array per tick).
  const lastEmaFast = useRef<number | null>(null);
  const lastEmaSlow = useRef<number | null>(null);
  const lastEmaTrigger = useRef<number | null>(null);

  // The most recent completed candle (used to decide when to advance EMAs).
  const lastClosedTime = useRef<number | null>(null);

  // ---- 1. Build chart on mount ----------------------------------------
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#131a2b" },
        textColor: "#8892b0",
      },
      grid: {
        vertLines: { color: "#1f2942" },
        horzLines: { color: "#1f2942" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#26304a" },
      timeScale: {
        borderColor: "#26304a",
        timeVisible: true,
        secondsVisible: false,
      },
      autoSize: true,
      height,
    });

    const candles = chart.addCandlestickSeries({
      upColor: "#2ecc71",
      downColor: "#e74c3c",
      wickUpColor: "#2ecc71",
      wickDownColor: "#e74c3c",
      borderVisible: false,
    });

    const fastLine = chart.addLineSeries({
      color: "#f1c40f",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      title: `EMA${emaFast}`,
    });
    const slowLine = chart.addLineSeries({
      color: "#9b59b6",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      title: `EMA${emaSlow}`,
    });
    const trigLine = chart.addLineSeries({
      color: "#3498db",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      title: `EMA${emaTrigger}`,
    });

    chartRef.current = chart;
    candleSeriesRef.current = candles;
    emaFastSeriesRef.current = fastLine;
    emaSlowSeriesRef.current = slowLine;
    emaTriggerSeriesRef.current = trigLine;

    // Resize observer for responsive layout
    const ro = new ResizeObserver(() => {
      chart.applyOptions({ height });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      emaFastSeriesRef.current = null;
      emaSlowSeriesRef.current = null;
      emaTriggerSeriesRef.current = null;
      channelUpperRef.current = null;
      channelMidRef.current = null;
      channelLowerRef.current = null;
      priceLinesRef.current = [];
    };
    // We intentionally only build the chart once; EMA period changes flow into
    // the reload effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [height]);

  // ---- 2. Load candles + subscribe to WS whenever symbol/interval changes -------
  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let wsRetryDelay = 1000;

    async function boot() {
      if (
        !candleSeriesRef.current ||
        !emaFastSeriesRef.current ||
        !emaSlowSeriesRef.current ||
        !emaTriggerSeriesRef.current
      )
        return;

      try {
        const resp = await api.getKlines({ symbol, interval, limit: 500 });
        if (cancelled) return;

        const candlesData: CandlestickData[] = resp.candles.map(toCandlestick);
        candleSeriesRef.current.setData(candlesData);

        const fastData = ema(resp.candles, emaFast);
        const slowData = ema(resp.candles, emaSlow);
        const trigData = ema(resp.candles, emaTrigger);
        emaFastSeriesRef.current.setData(fastData.map(toLineData));
        emaSlowSeriesRef.current.setData(slowData.map(toLineData));
        emaTriggerSeriesRef.current.setData(trigData.map(toLineData));

        lastEmaFast.current = fastData.at(-1)?.value ?? null;
        lastEmaSlow.current = slowData.at(-1)?.value ?? null;
        lastEmaTrigger.current = trigData.at(-1)?.value ?? null;
        lastClosedTime.current = resp.candles.at(-1)?.time ?? null;

        chartRef.current?.timeScale().fitContent();
      } catch (e) {
        console.error("chart.load_klines_failed", e);
      }

      connectWs();
    }

    function connectWs() {
      if (cancelled) return;
      const base = testnet ? WS_TESTNET : WS_MAINNET;
      const url = `${base}/${symbol.toLowerCase()}@kline_${interval}`;
      ws = new WebSocket(url);

      ws.onopen = () => {
        wsRetryDelay = 1000;
      };
      ws.onmessage = (msg) => {
        try {
          const payload = JSON.parse(msg.data) as BinanceKlineEvent;
          handleWsTick(payload);
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        if (cancelled) return;
        wsReconnectTimer = setTimeout(connectWs, wsRetryDelay);
        wsRetryDelay = Math.min(wsRetryDelay * 2, 15_000);
      };
      ws.onerror = () => {
        ws?.close();
      };
    }

    function handleWsTick(payload: BinanceKlineEvent) {
      if (
        !payload?.k ||
        !candleSeriesRef.current ||
        !emaFastSeriesRef.current ||
        !emaSlowSeriesRef.current ||
        !emaTriggerSeriesRef.current
      )
        return;

      const k = payload.k;
      const t = Math.floor(k.t / 1000); // ms → seconds
      const bar: CandlestickData = {
        time: t as UTCTimestamp,
        open: parseFloat(k.o),
        high: parseFloat(k.h),
        low: parseFloat(k.l),
        close: parseFloat(k.c),
      };

      // Update the candle series with the streaming bar (either forming or closed).
      candleSeriesRef.current.update(bar);

      // Live-update EMAs: while the bar is forming, we don't advance the EMA
      // memory (avoids drift). When x=true the bar closed → advance memory.
      const close = bar.close;
      if (lastEmaFast.current !== null && lastEmaSlow.current !== null && lastEmaTrigger.current !== null) {
        const provisionalFast = emaStep(lastEmaFast.current, close, emaFast);
        const provisionalSlow = emaStep(lastEmaSlow.current, close, emaSlow);
        const provisionalTrig = emaStep(lastEmaTrigger.current, close, emaTrigger);

        emaFastSeriesRef.current.update({ time: t as UTCTimestamp, value: provisionalFast });
        emaSlowSeriesRef.current.update({ time: t as UTCTimestamp, value: provisionalSlow });
        emaTriggerSeriesRef.current.update({ time: t as UTCTimestamp, value: provisionalTrig });

        if (k.x) {
          // Bar closed — persist the EMA memory so the next bar starts here.
          lastEmaFast.current = provisionalFast;
          lastEmaSlow.current = provisionalSlow;
          lastEmaTrigger.current = provisionalTrig;
          lastClosedTime.current = t;
        }
      }
    }

    boot();
    return () => {
      cancelled = true;
      if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
      ws?.close();
    };
  }, [symbol, interval, emaFast, emaSlow, emaTrigger, testnet]);

  // ---- 3. Draw / redraw trade level lines when openTrades changes -------
  useEffect(() => {
    if (!candleSeriesRef.current) return;
    // Clear existing lines
    for (const line of priceLinesRef.current) {
      candleSeriesRef.current.removePriceLine(line);
    }
    priceLinesRef.current = [];

    const relevant = openTrades.filter(
      (t) => t.symbol === symbol && (t.status === "OPEN" || t.status === "TP1_HIT"),
    );

    for (const t of relevant) {
      priceLinesRef.current.push(
        candleSeriesRef.current.createPriceLine({
          price: t.entry_price,
          color: "#8892b0",
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: `${t.side} #${t.id} entry`,
        }),
        candleSeriesRef.current.createPriceLine({
          price: t.stop_loss,
          color: "#e74c3c",
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          axisLabelVisible: true,
          title: `SL #${t.id}`,
        }),
        candleSeriesRef.current.createPriceLine({
          price: t.take_profit_1,
          color: "#2ecc71",
          lineWidth: 1,
          lineStyle: LineStyle.Dotted,
          axisLabelVisible: true,
          title: `TP1 #${t.id}`,
        }),
        candleSeriesRef.current.createPriceLine({
          price: t.take_profit_2,
          color: "#2ecc71",
          lineWidth: 2,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: `TP2 #${t.id}`,
        }),
      );
    }
  }, [openTrades, symbol]);

  // ---- 4. Auto parallel channel (regression) ---------------------------
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    // Helper — remove any existing channel series and clear the badge.
    const clearChannel = () => {
      for (const r of [channelUpperRef, channelMidRef, channelLowerRef]) {
        if (r.current) {
          try {
            chart.removeSeries(r.current);
          } catch {
            /* series already removed */
          }
          r.current = null;
        }
      }
      onChannelInfoRef.current?.(null);
    };

    if (!showChannel) {
      clearChannel();
      return;
    }

    let cancelled = false;

    (async () => {
      try {
        const resp = await api.getChannel({
          symbol,
          interval,
          lookback: channelLookback,
        });
        if (cancelled || !chartRef.current) return;

        // Create the 3 line series lazily on first show.
        if (!channelUpperRef.current) {
          channelUpperRef.current = chart.addLineSeries({
            color: "#14b8a6",
            lineWidth: 2,
            lineStyle: LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
            title: "Ch Up",
          });
        }
        if (!channelMidRef.current) {
          channelMidRef.current = chart.addLineSeries({
            color: "#14b8a6",
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            priceLineVisible: false,
            lastValueVisible: false,
            title: "Ch Mid",
          });
        }
        if (!channelLowerRef.current) {
          channelLowerRef.current = chart.addLineSeries({
            color: "#14b8a6",
            lineWidth: 2,
            lineStyle: LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
            title: "Ch Lo",
          });
        }

        const toPts = (ln: {
          start: { time: number; price: number };
          end: { time: number; price: number };
        }): LineData[] => [
          { time: ln.start.time as UTCTimestamp, value: ln.start.price },
          { time: ln.end.time as UTCTimestamp, value: ln.end.price },
        ];

        channelUpperRef.current.setData(toPts(resp.upper));
        channelMidRef.current.setData(toPts(resp.midline));
        channelLowerRef.current.setData(toPts(resp.lower));

        onChannelInfoRef.current?.({
          slope_pct_total: resp.slope_pct_total,
          width_pct: resp.width_pct,
          lookback: resp.lookback,
        });
      } catch (e) {
        console.error("chart.load_channel_failed", e);
        onChannelInfoRef.current?.(null);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [showChannel, symbol, interval, channelLookback]);

  return (
    <div
      ref={containerRef}
      style={{ height, width: "100%" }}
      className="bg-bg-soft rounded-lg overflow-hidden"
    />
  );
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function toCandlestick(c: Candle): CandlestickData {
  return {
    time: c.time as UTCTimestamp,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  };
}

function toLineData(p: { time: number; value: number }): LineData {
  return { time: p.time as UTCTimestamp, value: p.value };
}

// --------------------------------------------------------------------------
// Binance WS payload shape (kline stream)
// --------------------------------------------------------------------------

interface BinanceKlineEvent {
  e: string;
  E: number;
  s: string;
  k: {
    t: number; // start time (ms)
    T: number; // close time (ms)
    s: string;
    i: string;
    o: string;
    c: string;
    h: string;
    l: string;
    v: string;
    x: boolean; // is closed
  };
}
