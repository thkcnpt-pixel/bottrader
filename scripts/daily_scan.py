#!/usr/bin/env python3
"""Tier 2, 3 & 4 — Daily End-of-Day Technical Scan, Portfolio Risk Check & Gemini Critique.

Runs Mon-Fri at 18:00 UTC+7 via GitHub Actions (`daily_scan.yml`).
Saves report to Firestore `daily_reports/{YYYY-MM-DD}` and sends FCM Push Notification.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import (  # noqa: E402
    FIT_500K_MAX_PRICE_VND,
    FIT_500K_MIN_AVG_VALUE_VND,
    LOG_LEVEL,
    MAX_BUY_CANDIDATES,
    MIN_FUNDAMENTAL_SCORE,
    vn_today_str,
)
from utils.firebase_client import FirebaseClient  # noqa: E402
from utils.gemini_analyst import GeminiAnalyst  # noqa: E402
from utils.indicators import (  # noqa: E402
    add_technical_indicators,
    detect_buy_signal,
    evaluate_portfolio_position,
)
from utils.news_scraper import collect_ticker_news  # noqa: E402
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daily_scan")


def check_vnindex_regime(vn: VnstockClient) -> Dict[str, Any]:
    """Check VN-Index close vs MA50. Never halts scanning; sets warning_flag if below MA50."""
    df = vn.get_ohlcv("VNINDEX", days=130, is_index=True)
    if df is None or len(df) < 50:
        logger.warning("Could not fetch sufficient VNINDEX history.")
        return {
            "close": 0.0,
            "change_pct": 0.0,
            "ma50": 0.0,
            "above_ma50": True,
            "warning_flag": False,
            "warning_message": "",
        }

    data = add_technical_indicators(df)
    curr = data.iloc[-1]
    prev = data.iloc[-2]
    close_val = float(curr["close"])
    prev_close = float(prev["close"])
    ma50_val = float(curr["ma50"]) if not np.isnan(curr["ma50"]) else 0.0
    change_pct = ((close_val - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    above_ma50 = bool(ma50_val <= 0 or close_val >= ma50_val)
    warning_flag = not above_ma50

    return {
        "close": round(close_val, 2),
        "change_pct": round(change_pct, 2),
        "ma50": round(ma50_val, 2),
        "above_ma50": above_ma50,
        "warning_flag": warning_flag,
        "warning_message": "Cảnh báo: VN-Index dưới MA50 — Rủi ro cao" if warning_flag else "",
    }


def run_daily_scan() -> None:
    """Main entry point for daily scan workflow."""
    today_str = vn_today_str()
    vn = VnstockClient()
    fb = FirebaseClient()
    gemini = GeminiAnalyst()

    logger.info("=== Starting Daily Scan for %s ===", today_str)

    # 1. Market regime check (VN-Index vs MA50)
    vnindex_info = check_vnindex_regime(vn)
    logger.info(
        "VN-Index: %.2f (%+.2f%%) | MA50=%.2f | above_ma50=%s",
        vnindex_info["close"],
        vnindex_info["change_pct"],
        vnindex_info["ma50"],
        vnindex_info["above_ma50"],
    )

    # 2. Load cached fundamentals & holding portfolio
    fundamentals = fb.get_all_fundamentals()
    holdings = fb.get_holding_portfolio()

    eligible_fundamentals = {
        t: doc for t, doc in fundamentals.items()
        if float(doc.get("score", 0)) >= MIN_FUNDAMENTAL_SCORE
    }
    logger.info(
        "Loaded %d fundamental candidates (>= %.0f) and %d holding positions.",
        len(eligible_fundamentals),
        MIN_FUNDAMENTAL_SCORE,
        len(holdings),
    )

    # Batch actual close prices for accurate display
    all_symbols = sorted(set(eligible_fundamentals.keys()).union({h["ticker"] for h in holdings}))
    actual_prices_map = vn.get_actual_prices_batch(all_symbols)

    # 3. Tier 2 — Technical Scan on adjusted prices
    raw_candidates: List[Dict[str, Any]] = []
    for ticker, fdoc in eligible_fundamentals.items():
        try:
            df_adj = vn.get_ohlcv(ticker, days=160)
            if df_adj is None or len(df_adj) < 55:
                continue

            signal = detect_buy_signal(df_adj)
            if not signal:
                continue

            actual_close = float(
                actual_prices_map.get(ticker, {}).get("actual_close")
                or df_adj.iloc[-1]["close"]
            )
            recent_20 = df_adj.tail(20)
            avg_value_20d = float((recent_20["close"] * recent_20["volume"]).mean())
            fit_500k = bool(
                actual_close < FIT_500K_MAX_PRICE_VND
                and avg_value_20d >= FIT_500K_MIN_AVG_VALUE_VND
            )
            missing_bctc = bool(fdoc.get("missing_bctc", False))
            fund_score = float(fdoc.get("score", 0.0))
            tech_score = float(signal["technical_score"])
            combined_score = round(0.5 * fund_score + 0.5 * tech_score, 1)

            tags = [f"[{signal['signal_type']}]"]
            if fit_500k:
                tags.append("[Hợp vốn 500k]")
            if missing_bctc:
                tags.append("[Thiếu BCTC mới nhất]")

            # Scale MA20/MA50 to actual display price
            close_adj = float(signal["close_adj"])
            scale = (actual_close / close_adj) if close_adj > 0 else 1.0

            raw_candidates.append({
                "ticker": ticker,
                "exchange": fdoc.get("exchange", ""),
                "sector_group": fdoc.get("sector_group", "non_financial"),
                "signal_type": signal["signal_type"],
                "signal_priority": signal["signal_priority"],
                "fundamental_score": fund_score,
                "technical_score": tech_score,
                "combined_score": combined_score,
                "actual_close": round(actual_close, 0),
                "ma20": round(float(signal["ma20"]) * scale, 0),
                "ma50": round(float(signal["ma50"]) * scale, 0),
                "rsi14": signal["rsi14"],
                "vol_ratio": signal["vol_ratio"],
                "avg_value_20d_bn": round(avg_value_20d / 1e9, 1),
                "fit_500k": fit_500k,
                "missing_bctc": missing_bctc,
                "tags": tags,
                "reason": signal["reason"],
                "metrics": fdoc.get("metrics", {}),
            })
            time.sleep(0.15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Technical scan error for %s: %s", ticker, exc)

    # Sort: Priority A (Pullback) first, then highest combined_score
    raw_candidates.sort(key=lambda x: (x["signal_priority"], -x["combined_score"]))
    top_candidates = raw_candidates[:MAX_BUY_CANDIDATES]
    logger.info("Tier 2 selected %d/%d buy candidates.", len(top_candidates), len(raw_candidates))

    # 4. Tier 4 — Gemini Risk Critique (max 3 tickers/day, NO veto)
    for cand in top_candidates:
        ticker = cand["ticker"]
        logger.info("Running Tier 4 news + Gemini critique for %s...", ticker)
        vn_news_events = vn.get_company_news_and_events(ticker)
        news_bundle = collect_ticker_news(
            ticker=ticker,
            vnstock_news=vn_news_events.get("news", []),
            vnstock_events=vn_news_events.get("events", []),
        )
        critique = gemini.analyze_risk(
            ticker=ticker,
            fundamental_summary={
                "ticker": ticker,
                "exchange": cand["exchange"],
                "sector_group": cand["sector_group"],
                "fundamental_score": cand["fundamental_score"],
                "missing_bctc": cand["missing_bctc"],
                "metrics": cand["metrics"],
            },
            news_bundle=news_bundle,
        )
        cand["gemini_analysis"] = critique
        cand["news_count"] = len(news_bundle.get("articles", []))

    # 5. Tier 3 — Daily Portfolio Risk Management
    portfolio_alerts: List[Dict[str, Any]] = []
    for pos in holdings:
        ticker = pos["ticker"]
        try:
            df_adj = vn.get_ohlcv(ticker, days=160)
            if df_adj is None or len(df_adj) < 25:
                continue

            actual_close = float(
                actual_prices_map.get(ticker, {}).get("actual_close")
                or df_adj.iloc[-1]["close"]
            )
            fdoc = fundamentals.get(ticker)
            eval_res = evaluate_portfolio_position(
                position=pos,
                df_adj=df_adj,
                actual_close=actual_close,
                fundamental_doc=fdoc,
            )

            # Update highest_price and latest tracking metrics in Firestore
            updates: Dict[str, Any] = {
                "latest_price": eval_res["actual_close"],
                "latest_ma20": eval_res["ma20"],
                "latest_ma50": eval_res["ma50"],
                "latest_pnl_pct": eval_res["pnl_pct"],
                "last_checked": today_str,
            }
            old_high = float(pos.get("highest_price") or pos.get("buy_price") or 0.0)
            if eval_res["highest_price"] > old_high:
                updates["highest_price"] = eval_res["highest_price"]
            fb.update_portfolio_fields(ticker, updates)

            if eval_res["alerts"]:
                portfolio_alerts.append(eval_res)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portfolio check error for %s: %s", ticker, exc)

    # 6. Save daily report to Firestore
    report_payload = {
        "date": today_str,
        "vnindex": vnindex_info,
        "portfolio_alerts": portfolio_alerts,
        "buy_candidates": top_candidates,
        "scanned_fundamentals_count": len(eligible_fundamentals),
    }
    fb.save_daily_report(today_str, report_payload)

    # 7. Send Daily FCM Push Notification
    vni_tag = "⚠️ Dưới MA50" if vnindex_info["warning_flag"] else "✅ Trên MA50"
    cand_tickers = ", ".join(c["ticker"] for c in top_candidates) if top_candidates else "Không có"
    title = f"📊 Báo cáo Swing {today_str} | VN-Index {vnindex_info['change_pct']:+.2f}%"
    body_parts = [
        f"VN-Index: {vnindex_info['close']:.1f} ({vni_tag})",
        f"Điểm mua ({len(top_candidates)}): {cand_tickers}",
    ]
    if portfolio_alerts:
        alert_tickers = ", ".join(a["ticker"] for a in portfolio_alerts)
        body_parts.append(f"⚠️ Cảnh báo danh mục: {alert_tickers}")

    fb.send_fcm_notification(
        title=title,
        body=" • ".join(body_parts),
        data={"type": "daily_report", "date": today_str},
    )
    logger.info("=== Daily Scan %s completed successfully ===", today_str)


if __name__ == "__main__":
    run_daily_scan()
