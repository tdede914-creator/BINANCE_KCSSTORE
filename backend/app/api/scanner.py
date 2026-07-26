"""Scanner introspection endpoints.

Exposes the in-memory diagnostics the scanner keeps for every watchlist
symbol so the frontend can show *why* a signal didn't fire this tick.
No auth is required because the scanner is a singleton on the same
process as the API.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class SymbolDiag(BaseModel):
    symbol: str
    stage: str                    # 'warmup' | 'bias' | 'setup' | 'trigger' | 'fired'
    reason: str | None = None
    ts: str | None = None         # ISO timestamp of the evaluation
    market: str | None = None
    bias_side: str | None = None  # 'LONG' | 'SHORT' when bias passed


class ScannerDiagnostics(BaseModel):
    last_tick_ts: datetime | None
    last_tick_market: str | None
    symbols: list[SymbolDiag]


@router.get("/diagnostics", response_model=ScannerDiagnostics)
async def get_diagnostics(request: Request) -> ScannerDiagnostics:
    scanner = getattr(request.app.state, "scanner", None)
    if scanner is None:
        return ScannerDiagnostics(last_tick_ts=None, last_tick_market=None, symbols=[])

    raw: dict[str, dict] = getattr(scanner, "_diagnostics", {}) or {}
    market = getattr(scanner, "_last_tick_market", None)

    symbols: list[SymbolDiag] = []
    for sym, d in raw.items():
        symbols.append(
            SymbolDiag(
                symbol=sym,
                stage=str(d.get("stage", "unknown")),
                reason=d.get("reason"),
                ts=d.get("ts"),
                market=d.get("market"),
                bias_side=d.get("bias_side"),
            )
        )
    # Sort: fired first, then trigger, setup, bias, warmup — so the user
    # sees the most-progressed symbols at the top.
    stage_order = {"fired": 0, "trigger": 1, "setup": 2, "bias": 3, "warmup": 4}
    symbols.sort(key=lambda s: (stage_order.get(s.stage, 99), s.symbol))

    return ScannerDiagnostics(
        last_tick_ts=getattr(scanner, "_last_tick_ts", None),
        last_tick_market=market.value if hasattr(market, "value") else market,
        symbols=symbols,
    )
