"""Backtest performance metrics.

Given a list of :class:`SimTrade` and an equity curve, compute the
usual set of numbers a trader wants to see: return %, win rate, max
drawdown, Sharpe, profit factor, best/worst trade, breakdown by exit
reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.backtest.engine import BacktestResult, SimTrade  # noqa: F401


@dataclass(slots=True)
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    breakevens: int
    win_rate_pct: float

    initial_balance: float
    final_balance: float
    total_return_usdt: float
    total_return_pct: float
    max_drawdown_pct: float

    # Trade-level stats
    avg_win_usdt: float
    avg_loss_usdt: float
    avg_rr: float                # realised risk-reward ratio
    profit_factor: float         # sum(wins) / |sum(losses)|
    sharpe_ratio: float          # trade-level Sharpe, ann. by sqrt(N)

    best_trade_usdt: float
    worst_trade_usdt: float
    total_fees_usdt: float

    # Breakdown of exit reasons
    exits_tp2: int
    exits_sl: int
    exits_eop: int               # end-of-period force close


def compute_metrics(result: "BacktestResult") -> BacktestMetrics:
    trades = result.trades

    initial = result.config.initial_balance
    final = result.equity_curve[-1].equity if result.equity_curve else initial

    if not trades:
        return BacktestMetrics(
            total_trades=0,
            wins=0,
            losses=0,
            breakevens=0,
            win_rate_pct=0.0,
            initial_balance=initial,
            final_balance=final,
            total_return_usdt=0.0,
            total_return_pct=0.0,
            max_drawdown_pct=0.0,
            avg_win_usdt=0.0,
            avg_loss_usdt=0.0,
            avg_rr=0.0,
            profit_factor=0.0,
            sharpe_ratio=0.0,
            best_trade_usdt=0.0,
            worst_trade_usdt=0.0,
            total_fees_usdt=0.0,
            exits_tp2=0,
            exits_sl=0,
            exits_eop=0,
        )

    pnls = [t.realized_pnl for t in trades]
    wins = [p for p in pnls if p > 1e-6]
    losses = [p for p in pnls if p < -1e-6]
    breakevens = len(trades) - len(wins) - len(losses)

    total_return_usdt = sum(pnls)
    total_return_pct = (total_return_usdt / initial) * 100.0 if initial else 0.0

    # Max drawdown from equity curve.
    peak = initial
    max_dd_pct = 0.0
    for pt in result.equity_curve:
        peak = max(peak, pt.equity)
        if peak > 0:
            dd_pct = (peak - pt.equity) / peak * 100.0
            max_dd_pct = max(max_dd_pct, dd_pct)

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
    )

    # Trade-level Sharpe: mean / std of trade returns, annualised by sqrt(N).
    n = len(pnls)
    mean_pnl = sum(pnls) / n if n else 0.0
    if n > 1:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
        std = sqrt(var)
        sharpe = (mean_pnl / std) * sqrt(n) if std > 1e-9 else 0.0
    else:
        sharpe = 0.0

    # Realised risk:reward = avg_win / |avg_loss| — how much a winner
    # beats a loser on average.
    avg_rr = (avg_win / abs(avg_loss)) if avg_loss else 0.0

    exits_tp2 = sum(1 for t in trades if t.close_reason == "TP2")
    exits_sl = sum(1 for t in trades if t.close_reason == "SL")
    exits_eop = sum(1 for t in trades if t.close_reason == "EOP")

    total_fees = sum(t.total_fees for t in trades)

    win_rate = (len(wins) / n * 100.0) if n else 0.0

    return BacktestMetrics(
        total_trades=n,
        wins=len(wins),
        losses=len(losses),
        breakevens=breakevens,
        win_rate_pct=round(win_rate, 2),
        initial_balance=round(initial, 4),
        final_balance=round(final, 4),
        total_return_usdt=round(total_return_usdt, 4),
        total_return_pct=round(total_return_pct, 4),
        max_drawdown_pct=round(max_dd_pct, 4),
        avg_win_usdt=round(avg_win, 4),
        avg_loss_usdt=round(avg_loss, 4),
        avg_rr=round(avg_rr, 4),
        profit_factor=(
            round(profit_factor, 4)
            if profit_factor != float("inf")
            else 999.0
        ),
        sharpe_ratio=round(sharpe, 4),
        best_trade_usdt=round(max(pnls), 4),
        worst_trade_usdt=round(min(pnls), 4),
        total_fees_usdt=round(total_fees, 4),
        exits_tp2=exits_tp2,
        exits_sl=exits_sl,
        exits_eop=exits_eop,
    )
