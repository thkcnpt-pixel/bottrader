"""Phase 3 — Realized Trade Journal & Portfolio Performance Analytics.

Computes institutional-grade performance metrics directly from `settings/account`
(`realized_trades`, `cash_flows`, `cash_balance`, `total_deposited`) and live holdings:
  - Win Rate (%), Profit Factor, Expectancy (VND & %), Max Drawdown (%)
  - P&L breakdown by Ticker and by Month (YYYY-MM)
  - Cumulative Realized Equity Curve
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import numpy as np


def compute_trade_journal_analytics(
    realized_trades: Optional[List[Dict[str, Any]]] = None,
    initial_capital_vnd: float = 0.0,
) -> Dict[str, Any]:
    """Compute realized trading statistics from `settings/account.realized_trades`."""
    trades = [t for t in (realized_trades or []) if isinstance(t, dict)]
    if not trades:
        return {
            "total_closed_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_realized_pnl_vnd": 0,
            "expectancy_vnd": 0,
            "expectancy_pct": 0.0,
            "max_drawdown_vnd": 0,
            "max_drawdown_pct": 0.0,
            "pnl_by_ticker": {},
            "pnl_by_month": {},
            "equity_curve": [],
        }

    # Sort chronologically by sell_date
    sorted_trades = sorted(trades, key=lambda x: str(x.get("sell_date") or ""))
    pnls_vnd = np.array([float(t.get("net_pnl") or 0.0) for t in sorted_trades], dtype=float)
    pnls_pct = np.array([float(t.get("net_pnl_pct") or 0.0) for t in sorted_trades], dtype=float)

    wins_mask = pnls_vnd > 0
    losses_mask = pnls_vnd <= 0
    win_count = int(wins_mask.sum())
    loss_count = int(losses_mask.sum())
    total_count = len(sorted_trades)
    win_rate_pct = (win_count / total_count * 100.0) if total_count > 0 else 0.0

    gross_profit = float(pnls_vnd[wins_mask].sum()) if win_count > 0 else 0.0
    gross_loss = float(abs(pnls_vnd[losses_mask].sum())) if loss_count > 0 else 0.0
    profit_factor = (
        (gross_profit / gross_loss)
        if gross_loss > 0
        else (99.0 if gross_profit > 0 else 0.0)
    )

    total_realized_vnd = float(pnls_vnd.sum())
    expectancy_vnd = float(pnls_vnd.mean())
    expectancy_pct = float(pnls_pct.mean())

    # Cumulative P&L & Drawdown curve
    base_cap = max(float(initial_capital_vnd or 0.0), 1_000_000.0)
    cum_pnl = np.cumsum(pnls_vnd)
    equity_series = base_cap + cum_pnl
    peaks = np.maximum.accumulate(np.insert(equity_series, 0, base_cap))[1:]
    dd_vnd_series = peaks - equity_series
    dd_pct_series = np.where(peaks > 0, (dd_vnd_series / peaks) * 100.0, 0.0)

    max_dd_vnd = float(dd_vnd_series.max()) if len(dd_vnd_series) > 0 else 0.0
    max_dd_pct = float(dd_pct_series.max()) if len(dd_pct_series) > 0 else 0.0

    pnl_by_ticker: Dict[str, int] = {}
    pnl_by_month: Dict[str, int] = {}
    equity_curve: List[Dict[str, Any]] = []

    running_pnl = 0.0
    for t in sorted_trades:
        sym = str(t.get("ticker") or "UNKNOWN").upper()
        sell_date = str(t.get("sell_date") or "")[:10]
        month_key = sell_date[:7] if len(sell_date) >= 7 else "unknown"
        net_val = round(float(t.get("net_pnl") or 0.0))

        pnl_by_ticker[sym] = pnl_by_ticker.get(sym, 0) + net_val
        pnl_by_month[month_key] = pnl_by_month.get(month_key, 0) + net_val
        running_pnl += net_val
        equity_curve.append({
            "date": sell_date,
            "ticker": sym,
            "trade_pnl_vnd": net_val,
            "cumulative_pnl_vnd": round(running_pnl),
        })

    return {
        "total_closed_trades": total_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_rate_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "total_realized_pnl_vnd": round(total_realized_vnd),
        "expectancy_vnd": round(expectancy_vnd),
        "expectancy_pct": round(expectancy_pct, 2),
        "max_drawdown_vnd": round(max_dd_vnd),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "pnl_by_ticker": pnl_by_ticker,
        "pnl_by_month": pnl_by_month,
        "equity_curve": equity_curve[-30:],  # Keep last 30 points compact for Firestore
    }
