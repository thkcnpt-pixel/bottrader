#!/usr/bin/env python3
"""Tier 2, 3 & 4 — Daily End-of-Day Technical Scan, Portfolio Risk Check & Gemini Critique.

Runs Mon-Fri at 18:00 UTC+7 via GitHub Actions (`daily_scan.yml`).
Saves report to Firestore `daily_reports/{YYYY-MM-DD}` and sends FCM Push Notification.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import (  # noqa: E402
    DATA_ERROR_ALERT_THRESHOLD,
    FIT_500K_MAX_PRICE_VND,
    FIT_500K_MIN_AVG_VALUE_VND,
    FORCE_SCAN,
    LOG_LEVEL,
    MAX_BUY_CANDIDATES,
    MAX_WATCHLIST_CANDIDATES,
    MIN_FUNDAMENTAL_SCORE,
    vn_now,
    vn_today_str,
)
from utils.firebase_client import FirebaseClient  # noqa: E402
from utils.gemini_analyst import GeminiAnalyst  # noqa: E402
from utils.indicators import (  # noqa: E402
    add_technical_indicators,
    detect_buy_signal,
    detect_watchlist_candidate,
    evaluate_portfolio_position,
    is_valid_price_tick,
)
from utils.market_calendar import (  # noqa: E402
    has_fresh_daily_bar,
    is_trading_day,
)
from utils.news_scraper import (  # noqa: E402
    collect_ticker_news,
    detect_holding_negative_news,
)
from utils.portfolio_analytics import compute_trade_journal_analytics  # noqa: E402
from utils.risk_manager import (  # noqa: E402
    compute_atr_stops,
    compute_quant_position_sizing,
    evaluate_market_regime,
)
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daily_scan")


def check_vnindex_regime(vn: VnstockClient, today_str: str | None = None) -> Dict[str, Any]:
    """Check VN-Index close vs MA20/MA50/MA200. Never halts scanning; sets warning_flag if below MA50."""
    df = vn.get_ohlcv("VNINDEX", days=220, is_index=True)
    if df is None or len(df) < 50:
        logger.warning("Could not fetch sufficient VNINDEX history.")
        return {
            "close": 0.0,
            "change_pct": 0.0,
            "ma20": 0.0,
            "ma50": 0.0,
            "ma200": 0.0,
            "above_ma50": True,
            "regime": "UNKNOWN",
            "exposure_scale": 0.7,
            "warning_flag": False,
            "warning_message": "",
            "data_ok": False,
            "has_fresh_bar": True,
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
    fresh_bar = has_fresh_daily_bar(df, target_date=today_str) if today_str else True
    regime_details = evaluate_market_regime(df)

    return {
        "close": round(close_val, 2),
        "change_pct": round(change_pct, 2),
        "ma20": regime_details["ma20"],
        "ma50": round(ma50_val, 2),
        "ma200": regime_details["ma200"],
        "above_ma50": above_ma50,
        "regime": regime_details["regime"],
        "exposure_scale": regime_details["exposure_scale"],
        "warning_flag": warning_flag,
        "warning_message": "Cảnh báo: VN-Index dưới MA50 — Rủi ro cao" if warning_flag else "",
        "data_ok": True,
        "has_fresh_bar": fresh_bar,
    }


def run_daily_scan(force: bool = FORCE_SCAN) -> None:
    """Main entry point for daily scan workflow."""
    today_str = vn_today_str()
    fb = FirebaseClient()

    if not force and not is_trading_day(today_str):
        logger.info("Today (%s) is a weekend or VN market holiday — skipping daily scan.", today_str)
        fb.record_heartbeat("daily_scan", status="skipped_holiday", metrics={"date": today_str})
        return

    vn = VnstockClient()
    gemini = GeminiAnalyst()

    logger.info("=== Starting Daily Scan for %s (force=%s) ===", today_str, force)

    try:
        # 1. Market regime check (VN-Index vs MA20/MA50/MA200)
        vnindex_info = check_vnindex_regime(vn, today_str=today_str)
        logger.info(
            "VN-Index: %.2f (%+.2f%%) | MA50=%.2f | regime=%s | exposure_scale=%.2f",
            vnindex_info["close"],
            vnindex_info["change_pct"],
            vnindex_info["ma50"],
            vnindex_info.get("regime", "UNKNOWN"),
            vnindex_info.get("exposure_scale", 1.0),
        )

        if not vnindex_info.get("data_ok", True):
            fb.send_system_alert(
                workflow="daily_scan",
                title="⚠️ Cảnh báo dữ liệu: Không lấy được VN-Index",
                body="Các nguồn VCI/VNDirect/TCBS không trả về đủ lịch sử VN-Index. Bot vẫn tiếp tục quét danh mục.",
                dedup_key="vnindex_missing",
            )
        elif not force and not vnindex_info.get("has_fresh_bar", True):
            logger.info(
                "VN-Index OHLCV has no bar for %s (unscheduled holiday or stale feed) — skipping duplicate report.",
                today_str,
            )
            fb.record_heartbeat("daily_scan", status="skipped_stale_bar", metrics={"date": today_str})
            return

        # 2. Load cached fundamentals, holding portfolio & live account settings
        fundamentals = fb.get_all_fundamentals()
        holdings = fb.get_holding_portfolio()
        account_settings = fb.get_account_settings()

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

        # Compute live account summary (Available Cash, Stock Market Value, Total NAV)
        available_cash_vnd = round(max(0.0, float(account_settings.get("cash_balance") or 0.0)))
        fee_buy_pct = max(0.0, float(account_settings.get("fee_buy_pct") or 0.0))
        holding_market_value_vnd = 0.0
        holding_summary_list: List[str] = []
        for pos in holdings:
            hticker = str(pos.get("ticker") or "")
            hqty = int(float(pos.get("quantity") or 0))
            hbuy = float(pos.get("buy_price") or 0.0)
            hprice = float(
                actual_prices_map.get(hticker, {}).get("actual_close")
                or pos.get("latest_price")
                or hbuy
            )
            hval = round(hprice * hqty)
            holding_market_value_vnd += hval
            if hticker and hqty > 0:
                holding_summary_list.append(f"{hticker} ({hqty} CP ~{hval:,} đ)")

        total_nav_vnd = round(available_cash_vnd + holding_market_value_vnd)
        stock_ratio_pct = round((holding_market_value_vnd / total_nav_vnd) * 100.0, 1) if total_nav_vnd > 0 else 0.0
        cash_ratio_pct = round(100.0 - stock_ratio_pct, 1) if total_nav_vnd > 0 else 100.0
        logger.info(
            "Live Account Context: Available Cash=%s VND | Stock Value=%s VND | Total NAV=%s VND (Cash %.1f%% / Stock %.1f%%)",
            f"{available_cash_vnd:,}",
            f"{round(holding_market_value_vnd):,}",
            f"{total_nav_vnd:,}",
            cash_ratio_pct,
            stock_ratio_pct,
        )

        # 3. Tier 2 — Technical Scan on adjusted prices
        raw_candidates: List[Dict[str, Any]] = []
        raw_watchlist: List[Dict[str, Any]] = []
        tech_error_count = 0
        # Parallel warm-up of OHLCV cache (candidates + holdings share fetches)
        vn.prefetch_ohlcv(
            set(eligible_fundamentals.keys()) | {h["ticker"] for h in holdings}, days=160
        )
        for ticker, fdoc in eligible_fundamentals.items():
            try:
                df_adj = vn.get_ohlcv(ticker, days=160)
                if df_adj is None or len(df_adj) < 55:
                    tech_error_count += 1
                    continue

                exchange = fdoc.get("exchange", "HOSE")
                live_info = actual_prices_map.get(ticker, {})
                raw_actual = float(live_info.get("actual_close") or 0.0)
                ref_price = float(live_info.get("ref_price") or 0.0)
                if is_valid_price_tick(raw_actual, ref_price, exchange=exchange):
                    actual_close = raw_actual
                else:
                    actual_close = float(df_adj.iloc[-1]["close"])

                recent_20 = df_adj.tail(20)
                avg_value_20d = float((recent_20["close"] * recent_20["volume"]).mean())
                unit_cost_with_fee = actual_close * (1.0 + fee_buy_pct / 100.0)
                if available_cash_vnd > 0:
                    fit_account = bool(
                        unit_cost_with_fee <= available_cash_vnd
                        and avg_value_20d >= FIT_500K_MIN_AVG_VALUE_VND
                    )
                else:
                    fit_account = bool(
                        actual_close < FIT_500K_MAX_PRICE_VND
                        and avg_value_20d >= FIT_500K_MIN_AVG_VALUE_VND
                    )
                missing_bctc = bool(fdoc.get("missing_bctc", False))
                fund_score = float(fdoc.get("score", 0.0))

                signal = detect_buy_signal(df_adj)
                if not signal:
                    watch = detect_watchlist_candidate(df_adj)
                    if watch:
                        w_close_adj = float(watch["close_adj"])
                        w_scale = (actual_close / w_close_adj) if w_close_adj > 0 else 1.0
                        raw_watchlist.append({
                            "ticker": ticker,
                            "exchange": exchange,
                            "sector_group": fdoc.get("sector_group", "non_financial"),
                            "fundamental_score": fund_score,
                            "actual_close": round(actual_close, 0),
                            "ma20": round(float(watch["ma20"]) * w_scale, 0),
                            "ma50": round(float(watch["ma50"]) * w_scale, 0),
                            "rsi14": watch["rsi14"],
                            "vol_ratio": watch["vol_ratio"],
                            "trend_aligned": watch["trend_aligned"],
                            "dist_to_ma20_pct": watch["dist_to_ma20_pct"],
                            "dist_to_ma50_pct": watch["dist_to_ma50_pct"],
                            "avg_value_20d_bn": round(avg_value_20d / 1e9, 1),
                            "fit_account": fit_account,
                            "missing_bctc": missing_bctc,
                            "watch_reason": watch["watch_reason"],
                        })
                    continue

                tech_score = float(signal["technical_score"])
                combined_score = round(0.5 * fund_score + 0.5 * tech_score, 1)

                tags = [f"[{signal['signal_type']}]"]
                if fit_account:
                    tags.append("[Đủ sức mua TK]" if available_cash_vnd > 0 else "[Hợp vốn nhỏ]")
                if missing_bctc:
                    tags.append("[Thiếu BCTC mới nhất]")

                # Scale MA20/MA50/ATR14/levels to actual display price
                close_adj = float(signal["close_adj"])
                scale = (actual_close / close_adj) if close_adj > 0 else 1.0
                ma20_actual = round(float(signal["ma20"]) * scale, 0)
                ma50_actual = round(float(signal["ma50"]) * scale, 0)
                atr14_actual = round(float(signal.get("atr14", 0.0)) * scale, 0)
                high_20d_actual = round(float(signal.get("high_20d", 0.0)) * scale, 0)
                low_20d_actual = round(float(signal.get("low_20d", 0.0)) * scale, 0)
                high_60d_actual = round(float(signal.get("high_60d", 0.0)) * scale, 0)
                low_60d_actual = round(float(signal.get("low_60d", 0.0)) * scale, 0)

                atr_stops = compute_atr_stops(
                    entry_price=actual_close,
                    atr_actual=atr14_actual,
                    ma50_actual=ma50_actual,
                )
                quant_sizing = compute_quant_position_sizing(
                    entry_price=actual_close,
                    stop_loss_price=atr_stops["stop_loss_vnd"],
                    available_cash_vnd=available_cash_vnd,
                    total_nav_vnd=total_nav_vnd,
                    sector_group=fdoc.get("sector_group", "non_financial"),
                    holdings=holdings,
                    actual_prices_map=actual_prices_map,
                    fundamentals_map=fundamentals,
                    fee_buy_pct=fee_buy_pct,
                    market_exposure_scale=float(vnindex_info.get("exposure_scale", 1.0)),
                )

                raw_candidates.append({
                    "ticker": ticker,
                    "exchange": exchange,
                    "sector_group": fdoc.get("sector_group", "non_financial"),
                    "signal_type": signal["signal_type"],
                    "signal_priority": signal["signal_priority"],
                    "fundamental_score": fund_score,
                    "technical_score": tech_score,
                    "combined_score": combined_score,
                    "actual_close": round(actual_close, 0),
                    "ma20": ma20_actual,
                    "ma50": ma50_actual,
                    "atr14": atr14_actual,
                    "high_20d": high_20d_actual,
                    "low_20d": low_20d_actual,
                    "high_60d": high_60d_actual,
                    "low_60d": low_60d_actual,
                    "rsi14": signal["rsi14"],
                    "vol_ratio": signal["vol_ratio"],
                    "avg_value_20d_bn": round(avg_value_20d / 1e9, 1),
                    "fit_500k": fit_account,
                    "fit_account": fit_account,
                    "missing_bctc": missing_bctc,
                    "tags": tags,
                    "reason": signal["reason"],
                    "metrics": fdoc.get("metrics", {}),
                    "quant_risk_plan": {
                        **atr_stops,
                        **quant_sizing,
                    },
                })
            except Exception as exc:  # noqa: BLE001
                tech_error_count += 1
                logger.warning("Technical scan error for %s: %s", ticker, exc)

        # Sort: Priority A (Pullback) first, then highest combined_score
        raw_candidates.sort(key=lambda x: (x["signal_priority"], -x["combined_score"]))
        top_candidates = raw_candidates[:MAX_BUY_CANDIDATES]
        selected_tickers = {c["ticker"] for c in top_candidates}
        raw_watchlist = [w for w in raw_watchlist if w["ticker"] not in selected_tickers]
        raw_watchlist.sort(
            key=lambda x: (
                0 if x["trend_aligned"] else 1,
                -x["fundamental_score"],
                abs(x["dist_to_ma20_pct"]),
            )
        )
        top_watchlist = raw_watchlist[:MAX_WATCHLIST_CANDIDATES]
        logger.info(
            "Tier 2 selected %d/%d buy candidates and %d/%d watchlist candidates.",
            len(top_candidates),
            len(raw_candidates),
            len(top_watchlist),
            len(raw_watchlist),
        )

        # 4. Tier 4 — Multi-AI Debate Risk Critique (max 3 tickers/day, NO veto)
        for cand in top_candidates:
            ticker = cand["ticker"]
            logger.info("Running Tier 4 news + Multi-AI critique for %s...", ticker)
            vn_news_events = vn.get_company_news_and_events(ticker)
            news_bundle = collect_ticker_news(
                ticker=ticker,
                vnstock_news=vn_news_events.get("news", []),
                vnstock_events=vn_news_events.get("events", []),
            )
            actual_close_val = float(cand.get("actual_close") or 0)
            unit_cost_val = actual_close_val * (1.0 + fee_buy_pct / 100.0)
            max_shares_by_cash = int(available_cash_vnd // unit_cost_val) if (unit_cost_val > 0 and available_cash_vnd > 0) else 0
            critique = gemini.analyze_risk(
                ticker=ticker,
                fundamental_summary={
                    "ticker": ticker,
                    "exchange": cand["exchange"],
                    "sector_group": cand["sector_group"],
                    "vnindex_regime": {
                        "close": vnindex_info.get("close", 0.0),
                        "change_pct": vnindex_info.get("change_pct", 0.0),
                        "ma50": vnindex_info.get("ma50", 0.0),
                        "above_ma50": vnindex_info.get("above_ma50", True),
                        "warning_flag": vnindex_info.get("warning_flag", False),
                        "regime": vnindex_info.get("regime", ""),
                    },
                    "fundamental_score": cand["fundamental_score"],
                    "technical_score": cand["technical_score"],
                    "signal_type": cand["signal_type"],
                    "signal_reason": cand["reason"],
                    "actual_close_vnd": cand["actual_close"],
                    "ma20_vnd": cand["ma20"],
                    "ma50_vnd": cand["ma50"],
                    "atr14_vnd": cand["atr14"],
                    "resistance_20d_vnd": cand["high_20d"],
                    "support_20d_vnd": cand["low_20d"],
                    "resistance_60d_vnd": cand["high_60d"],
                    "support_60d_vnd": cand["low_60d"],
                    "rsi14": cand["rsi14"],
                    "vol_ratio": cand["vol_ratio"],
                    "fit_account": cand["fit_account"],
                    "account_context": {
                        "available_cash_vnd": available_cash_vnd,
                        "holding_stock_value_vnd": round(holding_market_value_vnd),
                        "total_nav_vnd": total_nav_vnd,
                        "cash_ratio_pct": cash_ratio_pct,
                        "stock_ratio_pct": stock_ratio_pct,
                        "fee_buy_pct": fee_buy_pct,
                        "current_holdings": holding_summary_list,
                        "max_affordable_shares_with_current_cash": max_shares_by_cash,
                        "quant_risk_plan": cand.get("quant_risk_plan", {}),
                    },
                    "missing_bctc": cand["missing_bctc"],
                    "metrics": cand["metrics"],
                },
                news_bundle=news_bundle,
            )
            cand["gemini_analysis"] = critique
            cand["ai_completed_at"] = vn_now().isoformat()
            cand["news_count"] = len(news_bundle.get("articles", []))
            cand["account_snapshot"] = {
                "available_cash_vnd": available_cash_vnd,
                "total_nav_vnd": total_nav_vnd,
                "max_affordable_shares": max_shares_by_cash,
            }

        # 5. Tier 3 — Daily Portfolio Risk Management (+ Phase 3 negative news monitoring)
        portfolio_alerts: List[Dict[str, Any]] = []
        portfolio_error_count = 0
        for pos in holdings:
            ticker = pos["ticker"]
            try:
                df_adj = vn.get_ohlcv(ticker, days=160)
                if df_adj is None or len(df_adj) < 25:
                    portfolio_error_count += 1
                    continue

                exchange = pos.get("exchange", "HOSE")
                live_info = actual_prices_map.get(ticker, {})
                raw_actual = float(live_info.get("actual_close") or 0.0)
                ref_price = float(live_info.get("ref_price") or 0.0)
                if is_valid_price_tick(raw_actual, ref_price, exchange=exchange):
                    actual_close = raw_actual
                else:
                    actual_close = float(df_adj.iloc[-1]["close"])

                fdoc = fundamentals.get(ticker)
                eval_res = evaluate_portfolio_position(
                    position=pos,
                    df_adj=df_adj,
                    actual_close=actual_close,
                    fundamental_doc=fdoc,
                )

                # Phase 3: Check holding ticker for urgent negative news
                try:
                    vn_news = vn.get_company_news_and_events(ticker)
                    hold_news_bundle = collect_ticker_news(
                        ticker=ticker,
                        vnstock_news=vn_news.get("news", []),
                        vnstock_events=vn_news.get("events", []),
                        max_articles=5,
                    )
                    flagged_news = detect_holding_negative_news(ticker, hold_news_bundle)
                    if flagged_news:
                        top_flag = flagged_news[0]
                        eval_res["alerts"].append({
                            "severity": "high",
                            "type": "NEGATIVE_NEWS_ALERT",
                            "message": (
                                f"{ticker} có tin tức rủi ro ({top_flag['matched_keywords']}): "
                                f"\"{top_flag['title']}\""
                            ),
                        })
                except Exception as news_exc:  # noqa: BLE001
                    logger.debug("Holding news scan skipped for %s: %s", ticker, news_exc)

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
                portfolio_error_count += 1
                logger.warning("Portfolio check error for %s: %s", ticker, exc)

        # 6. Compute Phase 3 Trade Journal Performance Analytics & Save daily report
        perf_analytics = compute_trade_journal_analytics(
            realized_trades=account_settings.get("realized_trades", []),
            initial_capital_vnd=float(account_settings.get("total_deposited") or total_nav_vnd),
        )
        scan_completed_at_iso = vn_now().isoformat()
        report_payload = {
            "date": today_str,
            "scan_completed_at": scan_completed_at_iso,
            "vnindex": vnindex_info,
            "portfolio_alerts": portfolio_alerts,
            "buy_candidates": top_candidates,
            "watchlist_candidates": top_watchlist,
            "scanned_fundamentals_count": len(eligible_fundamentals),
            "account_summary": {
                "available_cash_vnd": available_cash_vnd,
                "holding_stock_value_vnd": round(holding_market_value_vnd),
                "total_nav_vnd": total_nav_vnd,
                "cash_ratio_pct": cash_ratio_pct,
                "stock_ratio_pct": stock_ratio_pct,
                "performance_analytics": perf_analytics,
            },
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

        body_str = " • ".join(body_parts)
        fb.send_fcm_notification(
            title=title,
            body=body_str,
            data={"type": "daily_report", "date": today_str},
        )

        # 8. Record Heartbeat & check data error threshold
        total_scanned = len(eligible_fundamentals) + len(holdings)
        total_errors = tech_error_count + portfolio_error_count
        error_rate = (total_errors / total_scanned) if total_scanned > 0 else 0.0
        health_status = "degraded" if error_rate > DATA_ERROR_ALERT_THRESHOLD else "ok"
        fb.record_heartbeat(
            "daily_scan",
            status=health_status,
            metrics={
                "scanned_fundamentals": len(eligible_fundamentals),
                "holdings": len(holdings),
                "buy_candidates": len(top_candidates),
                "portfolio_alerts": len(portfolio_alerts),
                "tech_errors": tech_error_count,
                "portfolio_errors": portfolio_error_count,
                "error_rate": round(error_rate, 3),
                "market_regime": vnindex_info.get("regime", "UNKNOWN"),
            },
        )
        if total_scanned >= 5 and error_rate > DATA_ERROR_ALERT_THRESHOLD:
            fb.send_system_alert(
                workflow="daily_scan",
                title="⚠️ Cảnh báo hệ thống: Tỉ lệ lỗi dữ liệu cao",
                body=(
                    f"Daily Scan gặp lỗi khi tải dữ liệu cho {total_errors}/{total_scanned} mã "
                    f"({error_rate:.0%}). Kiểm tra kết nối API chứng khoán."
                ),
                dedup_key="high_data_error_rate",
            )

        logger.info("=== Daily Scan %s completed successfully ===", today_str)
    except Exception as exc:
        logger.exception("Unhandled failure in run_daily_scan: %s", exc)
        fb.record_heartbeat("daily_scan", status="failed", error_message=str(exc))
        fb.send_system_alert(
            workflow="daily_scan",
            title="🚨 Lỗi nghiêm trọng: Daily Scan thất bại",
            body=f"Workflow daily_scan bị dừng đột ngột: {str(exc)[:180]}",
            dedup_key="crash",
        )
        raise


if __name__ == "__main__":
    run_daily_scan()
