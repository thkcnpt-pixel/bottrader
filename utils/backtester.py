"""Phase 2 — Strategy Backtesting Engine for VN Stock Swing Setups (Pullback & Breakout).

Simulates Tier 2 entry signals (Type A Pullback & Type B Tight-Base Breakout) and Tier 3
exit rules (T+2.5 settlement constraint, +15% partial take-profit, -8% / Chandelier trailing
stop, MA50 trend exit, 20-day low stop-loss, and VN transaction fees/taxes).
"""

from __future__ import annotations

from typing import Any, Dict, List
import numpy as np
import pandas as pd

from utils.config import TAKE_PROFIT_HALF_PCT, TRAILING_STOP_DROP_PCT
from utils.indicators import add_technical_indicators, detect_buy_signal


def _net_leg_return_pct(
    buy_price: float,
    sell_price: float,
    fee_buy_pct: float,
    fee_sell_pct: float,
    tax_sell_pct: float,
) -> float:
    """Compute net percentage return for a trade leg after buy fee, sell fee, and sell tax."""
    if buy_price <= 0:
        return 0.0
    cost = buy_price * (1.0 + fee_buy_pct / 100.0)
    proceeds = sell_price * (1.0 - (fee_sell_pct + tax_sell_pct) / 100.0)
    return (proceeds - cost) / cost * 100.0


def backtest_symbol(
    df_ohlcv: pd.DataFrame,
    ticker: str = "SYM",
    fee_buy_pct: float = 0.15,
    fee_sell_pct: float = 0.15,
    tax_sell_pct: float = 0.10,
    t_plus_settlement: int = 2,
    max_hold_days: int = 60,
) -> Dict[str, Any]:
    """Run historical backtest on a single ticker's adjusted OHLCV DataFrame."""
    if df_ohlcv is None or len(df_ohlcv) < 60:
        return {"ticker": ticker, "trades": [], "summary": summarize_backtest_trades([])}

    data = add_technical_indicators(df_ohlcv).reset_index(drop=True)
    n = len(data)
    trades: List[Dict[str, Any]] = []

    in_position = False
    entry_idx = 0
    entry_price = 0.0
    entry_date = ""
    signal_type = ""
    highest_price = 0.0
    sold_half = False
    first_half_net_pct = 0.0

    for i in range(55, n):
        row = data.iloc[i]
        close_p = float(row["close"])
        ma20 = float(row["ma20"]) if not np.isnan(row["ma20"]) else 0.0
        ma50 = float(row["ma50"]) if not np.isnan(row["ma50"]) else 0.0
        date_label = str(row["time"])[:10] if "time" in data.columns else f"bar_{i}"

        if not in_position:
            # Fast pre-filter before calling detect_buy_signal on slice
            if ma50 <= 0 or not (close_p > ma50 and ma20 > ma50):
                continue
            window_df = data.iloc[max(0, i - 65) : i + 1]
            sig = detect_buy_signal(window_df)
            if sig is not None:
                in_position = True
                entry_idx = i
                entry_price = close_p
                entry_date = date_label
                signal_type = str(sig["signal_type"])
                highest_price = close_p
                sold_half = False
                first_half_net_pct = 0.0
            continue

        # Currently holding position
        highest_price = max(highest_price, close_p)
        hold_days = i - entry_idx

        # Enforce VN T+2.5 settlement constraint
        if hold_days < t_plus_settlement:
            continue

        recent_low = float(data.iloc[max(0, i - 20) : i]["low"].min())
        gross_pnl_pct = (close_p - entry_price) / entry_price * 100.0
        drawdown_from_high_pct = (close_p - highest_price) / highest_price * 100.0

        exit_reason = ""
        if ma50 > 0 and close_p < ma50:
            exit_reason = "BELOW_MA50"
        elif recent_low > 0 and close_p < recent_low:
            exit_reason = "BREAK_RECENT_LOW"
        elif sold_half and drawdown_from_high_pct <= -TRAILING_STOP_DROP_PCT:
            exit_reason = "TRAILING_STOP_8PCT"
        elif hold_days >= max_hold_days:
            exit_reason = "MAX_HOLD_DAYS"
        elif i == n - 1:
            exit_reason = "END_OF_DATA"

        if exit_reason:
            rem_net_pct = _net_leg_return_pct(
                entry_price, close_p, fee_buy_pct, fee_sell_pct, tax_sell_pct
            )
            blended_net_pct = (
                0.5 * first_half_net_pct + 0.5 * rem_net_pct if sold_half else rem_net_pct
            )
            trades.append({
                "ticker": ticker,
                "signal_type": signal_type,
                "entry_date": entry_date,
                "exit_date": date_label,
                "entry_price": round(entry_price, 2),
                "exit_price": round(close_p, 2),
                "holding_days": hold_days,
                "sold_half": sold_half,
                "net_return_pct": round(blended_net_pct, 2),
                "exit_reason": exit_reason,
            })
            in_position = False
            continue

        # Check partial take-profit (+15%) if not exiting all
        if not sold_half and gross_pnl_pct >= TAKE_PROFIT_HALF_PCT:
            first_half_net_pct = _net_leg_return_pct(
                entry_price, close_p, fee_buy_pct, fee_sell_pct, tax_sell_pct
            )
            sold_half = True

    return {
        "ticker": ticker,
        "trades": trades,
        "summary": summarize_backtest_trades(trades),
    }


def summarize_backtest_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute aggregate performance metrics from a list of backtested trades."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate_pct": 0.0,
            "expectancy_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "total_compounded_return_pct": 0.0,
            "avg_holding_days": 0.0,
            "by_signal_type": {},
        }

    returns = np.array([float(t["net_return_pct"]) for t in trades], dtype=float)
    wins = returns[returns > 0]
    losses = returns[returns <= 0]

    total_trades = len(returns)
    win_rate_pct = float(len(wins) / total_trades * 100.0)
    expectancy_pct = float(returns.mean())

    gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    # Compounded equity curve & max drawdown
    equity = np.cumprod(1.0 + returns / 100.0)
    peaks = np.maximum.accumulate(np.insert(equity, 0, 1.0))[1:]
    drawdowns = (equity - peaks) / peaks * 100.0
    max_dd_pct = float(abs(drawdowns.min())) if len(drawdowns) > 0 else 0.0
    total_compounded_pct = float((equity[-1] - 1.0) * 100.0)

    avg_days = float(np.mean([float(t["holding_days"]) for t in trades]))

    by_sig: Dict[str, Dict[str, Any]] = {}
    for sig_name in ("Pullback", "Breakout"):
        sub = [float(t["net_return_pct"]) for t in trades if t.get("signal_type") == sig_name]
        if sub:
            sub_arr = np.array(sub, dtype=float)
            by_sig[sig_name] = {
                "count": len(sub),
                "win_rate_pct": round(float((sub_arr > 0).mean() * 100.0), 2),
                "expectancy_pct": round(float(sub_arr.mean()), 2),
            }

    return {
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate_pct, 2),
        "expectancy_pct": round(expectancy_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "total_compounded_return_pct": round(total_compounded_pct, 2),
        "avg_holding_days": round(avg_days, 1),
        "by_signal_type": by_sig,
    }


def backtest_portfolio_universe(
    ohlcv_map: Dict[str, pd.DataFrame],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run backtest across multiple tickers and aggregate all trades."""
    all_trades: List[Dict[str, Any]] = []
    per_ticker: Dict[str, Dict[str, Any]] = {}

    for ticker, df in sorted(ohlcv_map.items()):
        res = backtest_symbol(df, ticker=ticker, **kwargs)
        per_ticker[ticker] = res["summary"]
        all_trades.extend(res["trades"])

    all_trades.sort(key=lambda t: str(t.get("entry_date", "")))
    return {
        "overall": summarize_backtest_trades(all_trades),
        "per_ticker": per_ticker,
        "trades": all_trades,
    }
