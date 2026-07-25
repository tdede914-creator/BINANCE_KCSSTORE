"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Resolve the backend WebSocket URL.
 *
 * See ``resolveApiUrl`` in ``lib/api.ts`` for the reasoning; same logic
 * applies, but we swap http(s) → ws(s).
 */
function resolveWsUrl(): string {
  if (process.env.NEXT_PUBLIC_WS_URL) return process.env.NEXT_PUBLIC_WS_URL;
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.hostname}:8000`;
  }
  return "ws://localhost:8000";
}

const WS_URL = resolveWsUrl();

export interface WsEvent {
  type: string;
  data?: Record<string, unknown>;
  trade?: Record<string, unknown>;
  timestamp?: string;
}

export function useEventStream(onEvent?: (ev: WsEvent) => void) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let retry = 1000;

    const connect = () => {
      const ws = new WebSocket(`${WS_URL}/ws/events`);
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) return;
        setConnected(true);
        retry = 1000;
      };
      ws.onmessage = (msg) => {
        try {
          const payload = JSON.parse(msg.data) as WsEvent;
          onEvent?.(payload);
        } catch {
          /* ignore malformed */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (cancelled) return;
        setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* noop */
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { connected };
}
