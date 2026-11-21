"""Phase 2 — Quantitative Risk Management: Market Regime, ATR Stops & Position Sizing.

Provides deterministic, math-based risk controls that complement the 3-AI Gemini council:
  1. Multi-tier VN-Index Market Regime (`BULL_STRONG`, `CAUTION_PULLBACK`,
     `SIDEWAYS_ACCUMULATION`, `BEAR_DEFENSIVE`) using MA20, MA50, MA200, and RSI14.
  2. ATR-based Dynamic Stop-Loss, Chandelier Trailing Stop, and R:R Target calculation.
  3. Risk-based Position Sizing with Sector Exposure Cap & Single-Position Cap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from utils.config import (
    ATR_INITIAL_STOP_MULT,
    ATR_TRAILING_STOP_MULT,
    DEFAULT_RISK_PER_TRADE_PCT,
    MAX_SECTOR_EXPOSURE_NAV_PCT,
    MAX_SINGLE_POSITION_NAV_PCT,
)
from utils.indicators import add_technical_indicators


def evaluate_market_regime(df_vnindex: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """Evaluate multi-tier VN-Index market regime and recommended exposure scale (0.3 - 1.0)."""
    if df_vnindex is None or len(df_vnindex) < 50:
        return {
            "regime": "UNKNOWN",
            "exposure_scale": 0.7,
            "ma20": 0.0,
            "ma50": 0.0,
            "ma200": 0.0,
            "rsi14": 50.0,
            "above_ma20": True,
            "above_ma50": True,
            "above_ma200": True,
        }

    data = add_technical_indicators(df_vnindex)
    curr = data.iloc[-1]
    close_val = float(curr["close"])
    ma20 = float(curr["ma20"]) if not np.isnan(curr["ma20"]) else close_val
    ma50 = float(curr["ma50"]) if not np.isnan(curr["ma50"]) else close_val
    ma200 = (
        float(curr["ma200"])
        if ("ma200" in curr and not np.isnan(curr["ma200"]))
        else ma50
    )
    rsi14 = float(curr["rsi14"]) if not np.isnan(curr["rsi14"]) else 50.0

    above_ma20 = close_val >= ma20
    above_ma50 = close_val >= ma50
    above_ma200 = close_val >= ma200

    if not above_ma50:
        regime = "BEAR_DEFENSIVE"
        exposure_scale = 0.30 if not above_ma200 else 0.45
    elif above_ma20 and ma20 >= ma50:
        regime = "BULL_STRONG"
        exposure_scale = 1.00
    elif not above_ma20 and ma20 >= ma50:
        regime = "CAUTION_PULLBACK"
        exposure_scale = 0.65
    else:
        regime = "SIDEWAYS_ACCUMULATION"
        exposure_scale = 0.75

    return {
        "regime": regime,
        "exposure_scale": round(exposure_scale, 2),
        "ma20": round(ma20, 2),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "rsi14": round(rsi14, 2),
        "above_ma20": above_ma20,
        "above_ma50": above_ma50,
        "above_ma200": above_ma200,
    }


def compute_atr_stops(
    entry_price: float,
    atr_actual: float,
    ma50_actual: float = 0.0,
    highest_price: float = 0.0,
) -> Dict[str, float]:
    """Compute initial ATR stop-loss, R:R 1:2 take-profit target, and Chandelier trailing stop."""
    if entry_price <= 0:
        return {
            "stop_loss_vnd": 0.0,
            "stop_distance_pct": 0.0,
            "take_profit_target_vnd": 0.0,
            "chandelier_trailing_stop_vnd": 0.0,
        }

    eff_atr = atr_actual if atr_actual > 0 else entry_price * 0.025
    raw_atr_stop = entry_price - ATR_INITIAL_STOP_MULT * eff_atr

    # Clamp initial stop distance between 3.0% and 8.0% below entry
    min_stop = entry_price * 0.92
    max_stop = entry_price * 0.97
    candidates = [raw_atr_stop]
    if 0 < ma50_actual < entry_price:
        candidates.append(ma50_actual * 0.99)

    stop_loss = max(min_stop, min(max_stop, max(candidates)))
    risk_per_share = max(entry_price - stop_loss, entry_price * 0.03)
    take_profit = entry_price + 2.0 * risk_per_share

    ref_high = max(highest_price, entry_price)
    chandelier_stop = max(0.0, ref_high - ATR_TRAILING_STOP_MULT * eff_atr)
    stop_dist_pct = (entry_price - stop_loss) / entry_price * 100.0

    return {
        "stop_loss_vnd": round(stop_loss, 0),
        "stop_distance_pct": round(stop_dist_pct, 2),
        "take_profit_target_vnd": round(take_profit, 0),
        "chandelier_trailing_stop_vnd": round(chandelier_stop, 0),
    }


def compute_quant_position_sizing(
    entry_price: float,
    stop_loss_price: float,
    available_cash_vnd: float,
    total_nav_vnd: float,
    sector_group: str = "non_financial",
    holdings: Optional[List[Dict[str, Any]]] = None,
    actual_prices_map: Optional[Dict[str, Dict[str, float]]] = None,
    fundamentals_map: Optional[Dict[str, Dict[str, Any]]] = None,
    fee_buy_pct: float = 0.0,
    market_exposure_scale: float = 1.0,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
    max_position_nav_pct: float = MAX_SINGLE_POSITION_NAV_PCT,
    max_sector_nav_pct: float = MAX_SECTOR_EXPOSURE_NAV_PCT,
) -> Dict[str, Any]:
    """Calculate risk-adjusted share quantity with single-stock and sector exposure limits."""
    unit_cost = entry_price * (1.0 + max(0.0, fee_buy_pct) / 100.0)
    if entry_price <= 0 or unit_cost <= 0 or available_cash_vnd <= 0:
        return {
            "recommended_shares": 0,
            "recommended_value_vnd": 0,
            "max_cash_shares": 0,
            "risk_budget_vnd": 0,
            "sector_exposure_pct": 0.0,
            "sector_cap_reached": False,
        }

    nav = max(total_nav_vnd, available_cash_vnd)
    max_cash_shares = int(available_cash_vnd // unit_cost)

    # 1. Risk-budget sizing: risk at most `risk_per_trade_pct`% of NAV if stop-loss is hit
    stop_dist = max(entry_price - stop_loss_price, entry_price * 0.03)
    risk_budget_vnd = nav * (risk_per_trade_pct / 100.0) * max(0.3, min(1.0, market_exposure_scale))
    risk_based_shares = int(risk_budget_vnd // stop_dist) if stop_dist > 0 else max_cash_shares

    # 2. Single-position NAV cap (e.g. max 35% of NAV; for small accounts < 2M VND allow up to 100%)
    effective_pos_cap_pct = 100.0 if nav < 2_000_000 else max_position_nav_pct
    max_pos_value = nav * (effective_pos_cap_pct / 100.0) * max(0.3, min(1.0, market_exposure_scale))
    pos_cap_shares = int(max_pos_value // unit_cost)

    # 3. Sector exposure cap check
    current_sector_value = 0.0
    for pos in holdings or []:
        t = str(pos.get("ticker") or "")
        qty = float(pos.get("quantity") or 0.0)
        if not t or qty <= 0:
            continue
        pos_sector = (
            (fundamentals_map or {}).get(t, {}).get("sector_group")
            or pos.get("sector_group")
            or "non_financial"
        )
        if pos_sector == sector_group:
            px = float(
                (actual_prices_map or {}).get(t, {}).get("actual_close")
                or pos.get("latest_price")
                or pos.get("buy_price")
                or 0.0
            )
            current_sector_value += px * qty

    sector_exposure_pct = (current_sector_value / nav * 100.0) if nav > 0 else 0.0
    effective_sector_cap_pct = 100.0 if nav < 2_000_000 else max_sector_nav_pct
    remaining_sector_budget = max(0.0, nav * (effective_sector_cap_pct / 100.0) - current_sector_value)
    sector_cap_shares = int(remaining_sector_budget // unit_cost)
    sector_cap_reached = remaining_sector_budget < unit_cost and nav >= 2_000_000

    # For small accounts (< 2M VND), risk_budget_vnd (1.5% of 500k = 7,500 VND) may be smaller
    # than 1-2 shares' stop distance; ensure small accounts can still size up to cash/position cap
    if nav < 2_000_000 and risk_based_shares < max_cash_shares:
        raw_recommended = min(max_cash_shares, max(1, pos_cap_shares))
    else:
        raw_recommended = min(max_cash_shares, risk_based_shares, pos_cap_shares, sector_cap_shares)

    recommended_shares = max(0, int(raw_recommended))
    recommended_value_vnd = round(recommended_shares * entry_price)

    return {
        "recommended_shares": recommended_shares,
        "recommended_value_vnd": recommended_value_vnd,
        "max_cash_shares": max_cash_shares,
        "risk_budget_vnd": round(risk_budget_vnd),
        "sector_exposure_pct": round(sector_exposure_pct, 1),
        "sector_cap_reached": bool(sector_cap_reached),
    }
