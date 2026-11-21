"""Pure-Python technical indicators, setup detectors, and portfolio risk rules.

All technical indicator calculations MUST run on adjusted OHLCV series,
while user-facing display prices use actual (unadjusted) close prices.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from utils.config import (
    BREAKOUT_BASE_MAX_DAYS,
    BREAKOUT_BASE_MAX_RANGE,
    BREAKOUT_BASE_MIN_DAYS,
    BREAKOUT_RSI_MAX,
    BREAKOUT_RSI_MIN,
    BREAKOUT_VOL_MULTIPLIER,
    INTRADAY_CEILING_SURGE_PCT,
    INTRADAY_DROP_THRESHOLD_PCT,
    INTRADAY_FLOOR_HNX_PCT,
    INTRADAY_FLOOR_HOSE_PCT,
    INTRADAY_MA50_BREACH_PCT,
    INTRADAY_VOL_SPIKE_RATIO,
    MAX_PRICE_BAND_HNX_PCT,
    MAX_PRICE_BAND_HOSE_PCT,
    MAX_PRICE_BAND_UPCOM_PCT,
    PULLBACK_MA20_HIGH_RATIO,
    PULLBACK_MA20_LOW_RATIO,
    PULLBACK_RSI_MAX,
    PULLBACK_RSI_MIN,
    TAKE_PROFIT_HALF_PCT,
    TRAILING_STOP_DROP_PCT,
)


def compute_sma(series: pd.Series, window: int) -> pd.Series:
    """Simple Moving Average."""
    return series.astype(float).rolling(window=window, min_periods=window).mean()


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index (RSI)."""
    close_f = close.astype(float)
    delta = close_f.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0 and avg_gain > 0, RSI is 100; when both 0, RSI is 50
    rsi = rsi.where(avg_loss != 0.0, np.where(avg_gain > 0.0, 100.0, 50.0))
    return rsi


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range (ATR)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach MA20, MA50, MA200, RSI14, ATR14, Vol_MA20 to an OHLCV DataFrame (sorted ascending by time)."""
    out = df.copy()
    out["ma20"] = compute_sma(out["close"], 20)
    out["ma50"] = compute_sma(out["close"], 50)
    out["ma200"] = compute_sma(out["close"], 200)
    out["rsi14"] = compute_rsi(out["close"], 14)
    if all(col in out.columns for col in ("high", "low", "close")):
        out["atr14"] = compute_atr(out, 14)
    else:
        out["atr14"] = np.nan
    out["vol_ma20"] = compute_sma(out["volume"], 20)
    return out


def _has_demand_candle(curr: pd.Series, prev: pd.Series) -> bool:
    """Check if the current candle shows buying demand / bounce / lower-wick rejection."""
    open_p = float(curr["open"])
    high_p = float(curr["high"])
    low_p = float(curr["low"])
    close_p = float(curr["close"])
    prev_close = float(prev["close"])

    candle_range = max(high_p - low_p, 1e-9)
    lower_wick = min(open_p, close_p) - low_p
    close_position = (close_p - low_p) / candle_range

    is_green_bounce = close_p > open_p and close_p >= prev_close
    is_hammer_rejection = (lower_wick / candle_range) >= 0.35 and close_position >= 0.50
    is_up_day = close_p > prev_close * 1.005
    return bool(is_green_bounce or is_hammer_rejection or is_up_day)


def _is_pullback_volume_contracting(df: pd.DataFrame) -> bool:
    """Verify prior 3-5 pullback sessions have contracting/below-average volume."""
    if len(df) < 6:
        return False
    prior = df.iloc[-5:-1]  # 4 sessions prior to current bounce
    vol_ma20 = float(df.iloc[-1]["vol_ma20"])
    if np.isnan(vol_ma20) or vol_ma20 <= 0:
        return False

    avg_prior_vol = float(prior["volume"].mean())
    # Contracting if average prior volume < Vol_MA20 or last 2 prior days < earlier 2 prior days
    if avg_prior_vol <= vol_ma20 * 1.05:
        return True
    first_half = float(prior.iloc[:2]["volume"].mean())
    second_half = float(prior.iloc[2:]["volume"].mean())
    return second_half < first_half


def detect_buy_signal(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Evaluate Tier 2 technical buy setups on an adjusted OHLCV DataFrame.

    Mandatory base trend:
      - Close > MA50 AND MA20 > MA50

    Setups (priority A > B):
      - Type A (Pullback):
          0.98 * MA20 <= Close <= 1.03 * MA20
          Demand candle (bounce / lower wick)
          45 <= RSI(14) <= 62
          Prior pullback sessions show contracting volume
      - Type B (Breakout nền chặt):
          10-15 prior sessions tight base: (Max - Min) / Min <= 8%
          Current Close breaks above base high
          Volume >= 1.5 * Vol_MA20
          55 <= RSI(14) <= 68
    """
    if df is None or len(df) < 55:
        return None

    data = add_technical_indicators(df)
    curr = data.iloc[-1]
    prev = data.iloc[-2]

    close_p = float(curr["close"])
    ma20 = float(curr["ma20"])
    ma50 = float(curr["ma50"])
    rsi14 = float(curr["rsi14"])
    atr14 = float(curr["atr14"]) if ("atr14" in curr and not np.isnan(curr["atr14"])) else 0.0
    vol = float(curr["volume"])
    vol_ma20 = float(curr["vol_ma20"])

    if any(np.isnan(x) for x in (close_p, ma20, ma50, rsi14, vol_ma20)) or vol_ma20 <= 0:
        return None

    # Mandatory base trend
    if not (close_p > ma50 and ma20 > ma50):
        return None

    vol_ratio = vol / vol_ma20
    tail_20 = data.tail(20)
    tail_60 = data.tail(min(60, len(data)))
    levels = {
        "high_20d": round(float(tail_20["high"].max()), 2),
        "low_20d": round(float(tail_20["low"].min()), 2),
        "high_60d": round(float(tail_60["high"].max()), 2),
        "low_60d": round(float(tail_60["low"].min()), 2),
    }

    # --- Type A: Pullback (Higher priority) ---
    in_pullback_zone = (PULLBACK_MA20_LOW_RATIO * ma20) <= close_p <= (PULLBACK_MA20_HIGH_RATIO * ma20)
    rsi_ok_a = PULLBACK_RSI_MIN <= rsi14 <= PULLBACK_RSI_MAX
    if in_pullback_zone and rsi_ok_a and _has_demand_candle(curr, prev) and _is_pullback_volume_contracting(data):
        # Technical score (0-100) for ranking
        dist_from_ma20 = abs(close_p / ma20 - 1.0)
        proximity_pts = max(0.0, 40.0 * (1.0 - dist_from_ma20 / 0.03))
        rsi_pts = 30.0 - abs(rsi14 - 53.5) * 1.5
        trend_spread = min((ma20 / ma50 - 1.0) * 100.0, 15.0)
        trend_pts = max(10.0, min(30.0, 15.0 + trend_spread))
        tech_score = round(min(100.0, max(50.0, proximity_pts + max(10.0, rsi_pts) + trend_pts)), 1)

        return {
            "signal_type": "Pullback",
            "signal_priority": 1,  # Priority A
            "technical_score": tech_score,
            "close_adj": round(close_p, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "rsi14": round(rsi14, 2),
            "atr14": round(atr14, 2),
            "vol_ratio": round(vol_ratio, 2),
            **levels,
            "reason": (
                f"Pullback vùng MA20 ({close_p/ma20 - 1:+.1%}), "
                f"RSI={rsi14:.1f}, lực cầu hồi phục sau nhịp chỉnh cạn cung."
            ),
        }

    # --- Type B: Tight-Base Breakout ---
    rsi_ok_b = BREAKOUT_RSI_MIN <= rsi14 <= BREAKOUT_RSI_MAX
    if rsi_ok_b and vol_ratio >= BREAKOUT_VOL_MULTIPLIER:
        best_base = None
        for window in range(BREAKOUT_BASE_MAX_DAYS, BREAKOUT_BASE_MIN_DAYS - 1, -1):
            if len(data) < window + 2:
                continue
            base_slice = data.iloc[-(window + 1):-1]
            base_high = float(base_slice["high"].max())
            base_low = float(base_slice["low"].min())
            if base_low <= 0:
                continue
            base_range = (base_high - base_low) / base_low
            if base_range <= BREAKOUT_BASE_MAX_RANGE and close_p > base_high:
                best_base = (window, base_range, base_high)
                break

        if best_base is not None:
            window, base_range, base_high = best_base
            tightness_pts = max(15.0, 35.0 * (1.0 - base_range / BREAKOUT_BASE_MAX_RANGE))
            vol_pts = min(35.0, 20.0 + (vol_ratio - BREAKOUT_VOL_MULTIPLIER) * 10.0)
            rsi_pts = max(10.0, 30.0 - abs(rsi14 - 61.5) * 2.0)
            tech_score = round(min(100.0, max(50.0, tightness_pts + vol_pts + rsi_pts)), 1)

            return {
                "signal_type": "Breakout",
                "signal_priority": 2,  # Priority B
                "technical_score": tech_score,
                "close_adj": round(close_p, 2),
                "ma20": round(ma20, 2),
                "ma50": round(ma50, 2),
                "rsi14": round(rsi14, 2),
                "atr14": round(atr14, 2),
                "vol_ratio": round(vol_ratio, 2),
                **levels,
                "reason": (
                    f"Breakout nền chặt {window} phiên (biên độ {base_range:.1%}), "
                    f"vượt đỉnh nền với Vol={vol_ratio:.2f}x MA20, RSI={rsi14:.1f}."
                ),
            }

    return None


def detect_watchlist_candidate(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Identify strong-fundamental stocks above MA50 that are close to a buy setup (Watchlist)."""
    if df is None or len(df) < 55:
        return None

    data = add_technical_indicators(df)
    curr = data.iloc[-1]

    close_p = float(curr["close"])
    ma20 = float(curr["ma20"])
    ma50 = float(curr["ma50"])
    rsi14 = float(curr["rsi14"])
    vol = float(curr["volume"])
    vol_ma20 = float(curr["vol_ma20"])

    if any(np.isnan(x) for x in (close_p, ma20, ma50, rsi14, vol_ma20)) or ma20 <= 0 or ma50 <= 0 or vol_ma20 <= 0:
        return None

    if close_p <= ma50:
        return None

    trend_aligned = bool(ma20 > ma50)
    dist_to_ma20_pct = round((close_p / ma20 - 1.0) * 100.0, 2)
    dist_to_ma50_pct = round((close_p / ma50 - 1.0) * 100.0, 2)
    vol_ratio = round(vol / vol_ma20, 2)

    if trend_aligned and dist_to_ma20_pct > 3.0:
        watch_reason = (
            f"Xu hướng tốt trên MA20/MA50 (cách MA20 +{dist_to_ma20_pct:.1f}%) — "
            f"chờ nhịp chỉnh về gần MA20 hoặc tích lũy nền chặt."
        )
    elif trend_aligned and -2.0 <= dist_to_ma20_pct <= 3.0:
        watch_reason = (
            f"Đang sát vùng MA20 ({dist_to_ma20_pct:+.1f}%), RSI={rsi14:.1f} — "
            f"chờ nến cầu xác nhận hồi phục hoặc phiên bứt phá thanh khoản."
        )
    elif trend_aligned:
        watch_reason = (
            f"Đang chỉnh dưới MA20 ({dist_to_ma20_pct:+.1f}%) nhưng giữ vững MA50 — "
            f"chờ giá lấy lại MA20."
        )
    else:
        watch_reason = (
            f"Giá đã vượt MA50 (+{dist_to_ma50_pct:.1f}%), RSI={rsi14:.1f} — "
            f"chờ MA20 cắt lên MA50 để xác nhận xu hướng tăng."
        )

    return {
        "close_adj": round(close_p, 2),
        "ma20": round(ma20, 2),
        "ma50": round(ma50, 2),
        "rsi14": round(rsi14, 2),
        "vol_ratio": vol_ratio,
        "trend_aligned": trend_aligned,
        "dist_to_ma20_pct": dist_to_ma20_pct,
        "dist_to_ma50_pct": dist_to_ma50_pct,
        "watch_reason": watch_reason,
    }


def evaluate_portfolio_position(
    position: Dict[str, Any],
    df_adj: pd.DataFrame,
    actual_close: float,
    fundamental_doc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate Tier 3 daily portfolio risk rules for a single holding position."""
    ticker = position["ticker"]
    buy_price = float(position["buy_price"])
    highest_price = max(float(position.get("highest_price") or buy_price), actual_close)
    sold_half = bool(position.get("sold_half", False))

    data = add_technical_indicators(df_adj)
    curr = data.iloc[-1]
    close_adj = float(curr["close"])
    ma50_adj = float(curr["ma50"]) if not np.isnan(curr["ma50"]) else 0.0
    ma20_adj = float(curr["ma20"]) if not np.isnan(curr["ma20"]) else 0.0
    atr14_adj = float(curr["atr14"]) if ("atr14" in curr and not np.isnan(curr["atr14"])) else 0.0

    # Recent swing low: lowest low of prior 20 sessions (excluding today)
    recent_low_adj = float(data.iloc[-21:-1]["low"].min()) if len(data) >= 21 else float(data.iloc[:-1]["low"].min())

    # Convert MA50/MA20/ATR14 to actual-price scale using close_actual / close_adj ratio
    price_scale = (actual_close / close_adj) if close_adj > 0 else 1.0
    ma50_actual = round(ma50_adj * price_scale, 0)
    ma20_actual = round(ma20_adj * price_scale, 0)
    atr14_actual = round(atr14_adj * price_scale, 0)
    chandelier_stop = round(max(0.0, highest_price - 2.5 * atr14_actual), 0) if atr14_actual > 0 else 0.0

    pnl_pct = ((actual_close - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
    drawdown_from_high_pct = (
        ((actual_close - highest_price) / highest_price * 100.0) if highest_price > 0 else 0.0
    )

    alerts: List[Dict[str, Any]] = []
    action = "HOLD"

    # 1. Stop-loss / Trend exit (Sell All)
    if ma50_adj > 0 and close_adj < ma50_adj:
        action = "SELL_ALL"
        alerts.append({
            "severity": "critical",
            "type": "BELOW_MA50",
            "message": f"{ticker} đóng cửa ({actual_close:,.0f}) dưới MA50 ({ma50_actual:,.0f}) — Thoát xu hướng, bán toàn bộ.",
        })

    if recent_low_adj > 0 and close_adj < recent_low_adj:
        action = "SELL_ALL"
        alerts.append({
            "severity": "critical",
            "type": "BREAK_RECENT_LOW",
            "message": f"{ticker} thủng đáy 20 phiên gần nhất — Cắt lỗ / bảo toàn vốn.",
        })

    # Check fundamental deterioration (latest quarter negative growth or negative OCF)
    if fundamental_doc:
        metrics = fundamental_doc.get("metrics") or {}
        profit_yoy = metrics.get("profit_yoy_pct")
        ocf = metrics.get("ocf_ttm")
        sector_group = fundamental_doc.get("sector_group", "non_financial")
        deteriorated = False
        reasons = []
        if profit_yoy is not None and profit_yoy < -15.0:
            deteriorated = True
            reasons.append(f"LNST YoY giảm sâu ({profit_yoy:+.1f}%)")
        if sector_group == "non_financial" and ocf is not None and ocf < 0:
            deteriorated = True
            reasons.append("Dòng tiền kinh doanh (OCF) âm")
        if deteriorated:
            action = "SELL_ALL"
            alerts.append({
                "severity": "critical",
                "type": "FUNDAMENTAL_DETERIORATION",
                "message": f"{ticker} suy giảm cơ bản BCTC ({', '.join(reasons)}) — Cân nhắc bán toàn bộ.",
            })

    # 2. Trailing stop after selling half (> 8% drop from highest_price)
    if sold_half and drawdown_from_high_pct <= -TRAILING_STOP_DROP_PCT:
        action = "SELL_ALL"
        alerts.append({
            "severity": "high",
            "type": "TRAILING_STOP_8PCT",
            "message": (
                f"{ticker} đã chốt 1/2 và rơi {drawdown_from_high_pct:.1f}% từ đỉnh "
                f"({highest_price:,.0f}) — Kích hoạt Trailing Stop bán phần còn lại."
            ),
        })

    # 3. Partial take-profit (+15% and not yet sold half)
    if action != "SELL_ALL" and not sold_half and pnl_pct >= TAKE_PROFIT_HALF_PCT:
        action = "TAKE_PROFIT_HALF"
        alerts.append({
            "severity": "info",
            "type": "TAKE_PROFIT_HALF",
            "message": (
                f"{ticker} đạt lãi {pnl_pct:+.1f}% (>= +{TAKE_PROFIT_HALF_PCT:.0f}%) — "
                f"Gợi ý chốt 1/2 khóa lãi, 1/2 còn lại gồng theo MA50 ({ma50_actual:,.0f})."
            ),
        })

    return {
        "ticker": ticker,
        "actual_close": round(actual_close, 0),
        "buy_price": round(buy_price, 0),
        "highest_price": round(highest_price, 0),
        "sold_half": sold_half,
        "pnl_pct": round(pnl_pct, 2),
        "drawdown_from_high_pct": round(drawdown_from_high_pct, 2),
        "ma20": ma20_actual,
        "ma50": ma50_actual,
        "atr14": atr14_actual,
        "chandelier_stop": chandelier_stop,
        "action": action,
        "alerts": alerts,
    }


def is_valid_price_tick(
    current_price: float,
    ref_price: float = 0.0,
    exchange: str = "HOSE",
) -> bool:
    """Validate an intraday price tick against exchange daily price-limit bands.

    Prevents bad API ticks (e.g., 0, negative, x1000 unit glitch, or spikes outside
    daily ceiling/floor limits) from corrupting `highest_price` trailing stops or
    triggering false intraday alerts.
    """
    if current_price is None or np.isnan(current_price) or current_price <= 0:
        return False
    if ref_price is None or np.isnan(ref_price) or ref_price <= 0:
        return True

    ex = str(exchange or "HOSE").upper().strip()
    if ex == "HOSE":
        max_band_pct = MAX_PRICE_BAND_HOSE_PCT
    elif ex == "HNX":
        max_band_pct = MAX_PRICE_BAND_HNX_PCT
    else:
        max_band_pct = MAX_PRICE_BAND_UPCOM_PCT

    change_pct = abs(current_price - ref_price) / ref_price * 100.0
    return bool(change_pct <= max_band_pct)


def evaluate_intraday_alert(
    ticker: str,
    exchange: str,
    current_price: float,
    ref_price: float,
    current_volume: float,
    vol_ma20: float,
    ma50_actual: float,
    session_progress: float = 0.5,
) -> List[Dict[str, Any]]:
    """Evaluate Tier 3 intraday urgent alert conditions (runs every 30 mins).

    Triggers if any of:
      1. Drop <= -3.5% vs ref_price WITH abnormally high volume.
      2. Price > 2% below MA50 OR near/at floor price.
      3. Surge >= +6.5% (ceiling alert / proactive take-profit reminder).
    """
    if ref_price <= 0 or current_price <= 0:
        return []
    if not is_valid_price_tick(current_price=current_price, ref_price=ref_price, exchange=exchange):
        return []

    change_pct = (current_price - ref_price) / ref_price * 100.0
    progress = max(0.15, min(1.0, session_progress))
    expected_vol = max(vol_ma20 * progress, 1.0)
    vol_spike_ratio = (current_volume / expected_vol) if vol_ma20 > 0 else 0.0

    triggered: List[Dict[str, Any]] = []

    # Condition 1: Sharp drop <= -3.5% with abnormal selling volume
    if change_pct <= INTRADAY_DROP_THRESHOLD_PCT and (
        vol_spike_ratio >= INTRADAY_VOL_SPIKE_RATIO or (vol_ma20 > 0 and current_volume >= 1.2 * vol_ma20)
    ):
        triggered.append({
            "alert_type": "SHARP_DROP_HIGH_VOL",
            "severity": "critical",
            "title": f"🚨 {ticker} giảm mạnh {change_pct:.2f}% kèm Vol cao!",
            "body": (
                f"Giá hiện tại: {current_price:,.0f} đ ({change_pct:+.2f}%). "
                f"Khối lượng đột biến ({vol_spike_ratio:.1f}x TB khung giờ). Kiểm tra cắt lỗ ngay!"
            ),
            "change_pct": round(change_pct, 2),
            "current_price": round(current_price, 0),
        })

    # Condition 2: Deep breach > 2% below MA50 OR floor price
    ma50_diff_pct = ((current_price - ma50_actual) / ma50_actual * 100.0) if ma50_actual > 0 else 0.0
    floor_limit = INTRADAY_FLOOR_HNX_PCT if str(exchange).upper() == "HNX" else INTRADAY_FLOOR_HOSE_PCT
    is_floor = change_pct <= floor_limit
    is_deep_ma50 = ma50_actual > 0 and ma50_diff_pct <= INTRADAY_MA50_BREACH_PCT

    if is_floor or is_deep_ma50:
        reason = "chạm giá SÀN" if is_floor else f"thủng sâu MA50 ({ma50_diff_pct:.1f}%)"
        triggered.append({
            "alert_type": "DEEP_MA50_OR_FLOOR",
            "severity": "critical",
            "title": f"⚠️ KHẨN: {ticker} {reason}!",
            "body": (
                f"Giá: {current_price:,.0f} đ ({change_pct:+.2f}%), "
                f"MA50: {ma50_actual:,.0f} đ. Cân nhắc xử lý vị thế ngay."
            ),
            "change_pct": round(change_pct, 2),
            "current_price": round(current_price, 0),
        })

    # Condition 3: Ceiling surge >= +6.5%
    if change_pct >= INTRADAY_CEILING_SURGE_PCT:
        triggered.append({
            "alert_type": "CEILING_SURGE",
            "severity": "info",
            "title": f"🚀 {ticker} tăng trần đột biến ({change_pct:+.2f}%)!",
            "body": (
                f"Giá: {current_price:,.0f} đ ({change_pct:+.2f}%). "
                f"Nhắc nhở: Cân nhắc chốt lời chủ động theo kế hoạch."
            ),
            "change_pct": round(change_pct, 2),
            "current_price": round(current_price, 0),
        })

    return triggered
