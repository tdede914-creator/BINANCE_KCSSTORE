"""KCS Signal Bot — MT5 Bridge for Exness (Windows).

This script runs on a Windows VPS that has:
  * MetaTrader 5 terminal installed (Exness build)
  * An Exness account logged in inside MT5
  * "AutoTrading" toggle enabled (Ctrl+E in MT5)
  * Python 3.11+ with the MetaTrader5 package installed

It polls the Linux backend for new forex signals, places the orders via
MT5's Python API, and reports the fills / closures back so the web
dashboard stays in sync.

Setup:
  1. pip install -r requirements.txt
  2. Copy config.example.py to config.py and fill in:
       - BACKEND_URL: e.g. http://43.128.118.89:8000
       - BRIDGE_SECRET: the mt5_bridge_secret shown in the web Settings
       - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER: your Exness account
       - RISK_PER_TRADE_PCT: e.g. 1.0 (=1% of account balance per trade)
  3. python bridge.py

The bridge auto-detects Exness's symbol suffix (some accounts use
"EURUSDm", some "EURUSDc" for cent accounts, etc.).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit(
        "MetaTrader5 package not installed. Run: pip install MetaTrader5"
    )

try:
    from config import (
        BACKEND_URL,
        BRIDGE_SECRET,
        MT5_LOGIN,
        MT5_PASSWORD,
        MT5_SERVER,
        RISK_PER_TRADE_PCT,
        POLL_INTERVAL_SECONDS,
        MAX_LOT_PER_TRADE,
        SYMBOLS_ALLOWLIST,
        DRY_RUN,
    )
except ImportError:
    raise SystemExit(
        "config.py not found. Copy config.example.py to config.py and edit it."
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bridge")


BASE_HEADERS = {"X-Bridge-Secret": BRIDGE_SECRET}


# --------------------------------------------------------------------------
# Symbol mapping — Exness suffixes vary per account tier.
# --------------------------------------------------------------------------


class SymbolMap:
    """Translate our internal names (EURUSD, XAUUSD, US500) to what MT5 sees.

    Exness reference symbols:
        - Standard      : EURUSD, XAUUSD, USDJPY, ...
        - Standard Cent : EURUSDc, XAUUSDc, ...
        - Pro           : EURUSD (no suffix, but different specs)
        - Zero          : EURUSDz, ...
    We probe available symbols once at startup and cache the map.
    """

    # Common Exness cash indices / commodities aliases
    INDEX_ALIASES = {
        "US500": ["US500", "SPX500", "SP500m", "US500m"],
        "US100": ["US100", "NAS100m", "USTECm", "NAS100"],
        "US30":  ["US30",  "DJ30m",   "US30m"],
        "GER30": ["GER30", "GER40",   "DE30m", "DAXm"],
        "UK100": ["UK100", "UK100m",  "FTSE100m"],
        "JPN225": ["JPN225", "JP225m", "N225m"],
        "USOIL": ["USOIL", "USOILm", "XTIUSDm", "USOUSDm"],
        "UKOIL": ["UKOIL", "UKOILm", "XBRUSDm", "BRENTm"],
    }

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def build(self) -> None:
        all_syms = mt5.symbols_get() or []
        names = {s.name for s in all_syms}
        log.info("MT5 lists %d symbols", len(names))

        # 1) Try aliases first (indices/commodities are broker-specific).
        for base, aliases in self.INDEX_ALIASES.items():
            for a in aliases:
                if a in names:
                    self._map[base] = a
                    break

        # 2) FX + metals — probe suffixes.
        fx_and_metals = [
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
            "USDCAD", "NZDUSD", "USDCHF", "EURJPY",
            "GBPJPY", "AUDJPY",
            "XAUUSD", "XAGUSD", "XPTUSD",
        ]
        for base in fx_and_metals:
            for suffix in ["", "m", "c", ".raw", "z", ".pro"]:
                candidate = base + suffix
                if candidate in names:
                    self._map[base] = candidate
                    break

        log.info("Symbol map built: %d entries", len(self._map))
        for k, v in self._map.items():
            log.info("  %-10s -> %s", k, v)

    def translate(self, base: str) -> str | None:
        return self._map.get(base.upper())


# --------------------------------------------------------------------------
# Position sizing (lot calculation from risk-per-trade + SL distance)
# --------------------------------------------------------------------------


def calc_lot(mt5_symbol: str, entry: float, stop_loss: float, account_balance: float) -> float:
    """How many lots to trade so a full SL hit loses RISK_PER_TRADE_PCT.

    Uses MT5's ``symbol_info`` for the true tick value + step size so the
    math is correct for every symbol type (fx, gold, indices).
    """
    info = mt5.symbol_info(mt5_symbol)
    if info is None:
        raise RuntimeError(f"symbol_info returned None for {mt5_symbol}")

    tick_size = info.trade_tick_size or info.point
    tick_value = info.trade_tick_value  # USD gain per tick per 1 lot

    price_distance = abs(entry - stop_loss)
    if price_distance <= 0 or tick_size <= 0 or tick_value <= 0:
        raise RuntimeError(
            f"bad price/tick math: dist={price_distance} tick_size={tick_size} tick_value={tick_value}"
        )

    ticks_to_sl = price_distance / tick_size
    risk_usd = account_balance * (RISK_PER_TRADE_PCT / 100.0)
    lot = risk_usd / (ticks_to_sl * tick_value)

    # Round to lot step
    step = info.volume_step or 0.01
    lot = round(lot / step) * step

    # Clamp to broker limits + user cap
    lot = max(info.volume_min, min(lot, info.volume_max, MAX_LOT_PER_TRADE))
    return round(lot, 2)


# --------------------------------------------------------------------------
# Order placement
# --------------------------------------------------------------------------


@dataclass
class OrderResult:
    ok: bool
    ticket: str | None = None
    fill_price: float | None = None
    lot: float | None = None
    mt5_symbol: str | None = None
    error: str | None = None


def place_order(
    signal: dict, mt5_symbol: str, account_balance: float
) -> OrderResult:
    """Send a MARKET order to MT5 with native SL + TP1 attached."""
    side = signal["side"].upper()
    is_long = side in ("LONG", "BUY")

    lot = calc_lot(
        mt5_symbol,
        entry=signal["entry_price"],
        stop_loss=signal["stop_loss"],
        account_balance=account_balance,
    )

    tick = mt5.symbol_info_tick(mt5_symbol)
    if tick is None:
        return OrderResult(ok=False, error=f"no tick for {mt5_symbol}")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if is_long else tick.bid,
        "sl": signal["stop_loss"],
        # MT5 supports only ONE TP per order slot. We use TP1 as the
        # native TP; TP2 / TP3 stay display-only unless the bridge is
        # extended to open two positions (out of scope for v1).
        "tp": signal["take_profit_1"],
        "deviation": 20,
        "magic": 234000,
        "comment": f"KCS #{signal['id']}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if DRY_RUN:
        log.warning("DRY_RUN: would send %s", request)
        return OrderResult(
            ok=True,
            ticket=f"DRY-{signal['id']}",
            fill_price=request["price"],
            lot=lot,
            mt5_symbol=mt5_symbol,
        )

    result = mt5.order_send(request)
    if result is None:
        return OrderResult(
            ok=False, error=f"order_send returned None: {mt5.last_error()}"
        )
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(
            ok=False,
            error=f"retcode={result.retcode} comment={result.comment}",
        )

    return OrderResult(
        ok=True,
        ticket=str(result.order),
        fill_price=result.price,
        lot=lot,
        mt5_symbol=mt5_symbol,
    )


# --------------------------------------------------------------------------
# Backend API helpers
# --------------------------------------------------------------------------


def get_pending() -> list[dict]:
    r = requests.get(
        f"{BACKEND_URL}/api/mt5/pending", headers=BASE_HEADERS, timeout=10
    )
    r.raise_for_status()
    return r.json().get("signals", [])


def report_execution(payload: dict) -> None:
    r = requests.post(
        f"{BACKEND_URL}/api/mt5/report",
        headers=BASE_HEADERS,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()


def report_close(payload: dict) -> None:
    r = requests.post(
        f"{BACKEND_URL}/api/mt5/close-report",
        headers=BASE_HEADERS,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()


def send_heartbeat() -> None:
    try:
        requests.post(
            f"{BACKEND_URL}/api/mt5/heartbeat",
            headers=BASE_HEADERS,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("heartbeat failed: %s", e)


# --------------------------------------------------------------------------
# Position tracking (poll MT5 for closures)
# --------------------------------------------------------------------------


class PositionTracker:
    """Remember open tickets we've reported to the backend, so we can
    detect when MT5 closes them (SL/TP hit) and report back."""

    def __init__(self) -> None:
        self._known: dict[str, dict] = {}   # ticket -> last-seen snapshot

    def add(self, ticket: str, symbol: str, side: str, sl: float, tp: float) -> None:
        self._known[ticket] = {"symbol": symbol, "side": side, "sl": sl, "tp": tp}

    def poll(self) -> None:
        if not self._known:
            return
        positions = mt5.positions_get() or []
        open_tickets = {str(p.ticket) for p in positions}
        # Anything we knew about that isn't in the current position list
        # was closed (SL, TP, or manual). Look up the closing deal to
        # find the exit price and reason.
        closed = [t for t in list(self._known.keys()) if t not in open_tickets]
        for ticket in closed:
            self._report_closed(ticket)

    def _report_closed(self, ticket: str) -> None:
        info = self._known.pop(ticket, None)
        if info is None:
            return
        # Find the deal(s) that closed the position by ticket ID.
        deals = mt5.history_deals_get(position=int(ticket))
        exit_price = 0.0
        reason = "MANUAL"
        realized_pnl = 0.0
        if deals:
            close_deal = deals[-1]
            exit_price = float(close_deal.price)
            realized_pnl = float(close_deal.profit)
            # Rough reason detection: was the fill at (or beyond) SL / TP?
            if info["side"] in ("LONG", "BUY"):
                if exit_price <= info["sl"] * 1.001:
                    reason = "SL"
                elif exit_price >= info["tp"] * 0.999:
                    reason = "TP"
            else:  # short
                if exit_price >= info["sl"] * 0.999:
                    reason = "SL"
                elif exit_price <= info["tp"] * 1.001:
                    reason = "TP"

        try:
            report_close({
                "ticket": ticket,
                "exit_price": exit_price,
                "reason": reason,
                "realized_pnl": realized_pnl,
            })
            log.info("reported close: ticket=%s reason=%s pnl=%.2f", ticket, reason, realized_pnl)
        except Exception as e:  # noqa: BLE001
            log.error("close-report failed: %s", e)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


def main() -> None:
    log.info("KCS MT5 bridge starting")
    log.info("Backend: %s", BACKEND_URL)
    if DRY_RUN:
        log.warning("DRY_RUN=True — no real orders will be sent")

    # --- Init MT5 ---
    if not mt5.initialize(
        login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER
    ):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

    acct = mt5.account_info()
    if acct is None:
        raise SystemExit("MT5 account_info returned None — check credentials")
    log.info(
        "Logged in: login=%s server=%s balance=%.2f %s",
        acct.login, acct.server, acct.balance, acct.currency,
    )
    if not acct.trade_allowed:
        log.error("!! MT5 says trading NOT allowed. Enable AutoTrading (Ctrl+E).")

    # --- Build symbol map ---
    smap = SymbolMap()
    smap.build()

    tracker = PositionTracker()
    processed: set[int] = set()   # in-memory guard so we don't retry same signal

    while True:
        try:
            send_heartbeat()

            # 1) Pick up any new signals
            for sig in get_pending():
                sid = int(sig["id"])
                if sid in processed:
                    continue
                processed.add(sid)

                # Optional whitelist — skip symbols not in the user's list
                if SYMBOLS_ALLOWLIST and sig["symbol"] not in SYMBOLS_ALLOWLIST:
                    log.info("skipping %s — not in SYMBOLS_ALLOWLIST", sig["symbol"])
                    report_execution({
                        "signal_id": sid, "ok": False,
                        "error": f"symbol not in bridge allowlist",
                    })
                    continue

                mt5_symbol = smap.translate(sig["symbol"])
                if mt5_symbol is None:
                    log.warning("no MT5 symbol found for %s", sig["symbol"])
                    report_execution({
                        "signal_id": sid, "ok": False,
                        "error": f"symbol {sig['symbol']} not found in MT5",
                    })
                    continue

                # Refresh balance right before sizing so we always trade
                # at correct %-of-current.
                acct = mt5.account_info()
                balance = float(acct.balance) if acct else 0.0

                result = place_order(sig, mt5_symbol, balance)
                if result.ok:
                    tracker.add(
                        ticket=result.ticket,
                        symbol=mt5_symbol,
                        side=sig["side"],
                        sl=sig["stop_loss"],
                        tp=sig["take_profit_1"],
                    )
                    report_execution({
                        "signal_id": sid,
                        "ok": True,
                        "ticket": result.ticket,
                        "fill_price": result.fill_price,
                        "lot": result.lot,
                        "mt5_symbol": result.mt5_symbol,
                    })
                    log.info(
                        "FILLED signal #%d %s %s lot=%.2f @ %.5f ticket=%s",
                        sid, sig["side"], mt5_symbol,
                        result.lot, result.fill_price, result.ticket,
                    )
                else:
                    report_execution({
                        "signal_id": sid, "ok": False, "error": result.error,
                    })
                    log.error("FAILED signal #%d: %s", sid, result.error)

            # 2) Poll open positions for closures
            tracker.poll()

        except requests.exceptions.RequestException as e:
            log.error("backend HTTP error: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("main-loop error: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
