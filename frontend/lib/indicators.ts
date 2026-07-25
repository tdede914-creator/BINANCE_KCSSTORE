/**
 * Client-side technical indicators for the chart overlays.
 *
 * These mirror the backend `app/strategy/indicators.py` implementations
 * closely so the chart matches what the scanner sees. Kept tiny and pure
 * so they're trivial to test.
 */

import type { Candle } from "./api";

export interface LinePoint {
  time: number; // unix seconds — same unit as Candle.time
  value: number;
}

/**
 * Exponential moving average.
 *
 * Alpha = 2 / (period + 1), seeded from the first close so the line
 * starts from candle 0 (matches lightweight-charts expectations).
 */
export function ema(candles: Candle[], period: number): LinePoint[] {
  if (candles.length === 0 || period <= 0) return [];
  const alpha = 2 / (period + 1);
  const out: LinePoint[] = [];
  let prev = candles[0].close;
  out.push({ time: candles[0].time, value: prev });
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i].close;
    prev = alpha * c + (1 - alpha) * prev;
    out.push({ time: candles[i].time, value: prev });
  }
  return out;
}

/**
 * Update the last EMA point in-place when a new candle close arrives.
 * Faster than re-running `ema()` over the entire history.
 */
export function emaStep(
  prevValue: number,
  newClose: number,
  period: number,
): number {
  const alpha = 2 / (period + 1);
  return alpha * newClose + (1 - alpha) * prevValue;
}

/**
 * Wilder's RSI. Returned as points on the same time axis as the input.
 * We start emitting values after `period` bars so lightweight-charts
 * won't draw noise at the start.
 */
export function rsi(candles: Candle[], period = 14): LinePoint[] {
  if (candles.length <= period) return [];
  const out: LinePoint[] = [];
  let avgGain = 0;
  let avgLoss = 0;

  // Warm-up (simple average of first `period` gains/losses).
  for (let i = 1; i <= period; i++) {
    const diff = candles[i].close - candles[i - 1].close;
    if (diff > 0) avgGain += diff;
    else avgLoss -= diff;
  }
  avgGain /= period;
  avgLoss /= period;

  const push = (i: number) => {
    const rs = avgLoss === 0 ? Infinity : avgGain / avgLoss;
    const value = 100 - 100 / (1 + rs);
    out.push({ time: candles[i].time, value });
  };
  push(period);

  for (let i = period + 1; i < candles.length; i++) {
    const diff = candles[i].close - candles[i - 1].close;
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    push(i);
  }
  return out;
}
