"""@responsibility 백테스트 통계 — Phase 2 게이트용 승률·기대값·손익비·R기대값·PF·MDD 테이블

Backtest statistics — the Phase 2 gate table. Computed over CLOSE rows (one
per position), each carrying the full realized PnL and R multiple. Reports
both USD and R metrics because the strategy source's whole thesis is the
profit ratio (손익비): a positive expectancy in R matters more than win rate.
"""
from __future__ import annotations

import math


def _f(v) -> float:
    return float(v) if v not in ("", None) else 0.0


def compute(closes: list[dict], starting_equity: float,
            final_equity: float) -> dict:
    n = len(closes)
    pnls = [_f(r["pnl_usd"]) for r in closes]
    rs = [_f(r["r_multiple"]) for r in closes if r["r_multiple"] not in ("", None)]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    win_sum = sum(wins)
    loss_sum = abs(sum(losses))
    expectancy = total / n if n else 0.0

    z = None
    if n >= 2:
        var = sum((p - expectancy) ** 2 for p in pnls) / (n - 1)
        if var > 0:
            z = round(expectancy / math.sqrt(var) * math.sqrt(n), 2)

    # equity-path drawdown in USD
    peak = equity = 0.0
    max_dd = 0.0
    streak = worst_streak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "pnl_usd": round(total, 2),
        "return_pct": round((final_equity / starting_equity - 1) * 100, 2)
        if starting_equity else None,
        "expectancy_usd": round(expectancy, 3),
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "avg_win_r": round(sum(r for r in rs if r > 0)
                           / max(1, sum(1 for r in rs if r > 0)), 3) if rs else None,
        "avg_loss_r": round(sum(r for r in rs if r <= 0)
                            / max(1, sum(1 for r in rs if r <= 0)), 3) if rs else None,
        "profit_factor": round(win_sum / loss_sum, 2) if loss_sum else None,
        "z_score": z,
        "max_drawdown_usd": round(max_dd, 2),
        "max_losing_streak": worst_streak,
    }
