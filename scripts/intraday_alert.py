#!/usr/bin/env python3
"""Tier 3 — Intraday Urgent Risk Alert Scanner (every 30 mins during trading hours).

Runs Mon-Fri at 09:15-11:15 and 13:15-14:45 (UTC+7) via GitHub Actions (`intraday_alert.yml`).
ONLY scans tickers in Firestore `portfolio` where `status == "holding"`.
Includes per-session anti-spam tracking in Firestore `intraday_alerts_state/{YYYY-MM-DD}`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import (  # noqa: E402
    DATA_ERROR_ALERT_THRESHOLD,
    FORCE_SCAN,
    LOG_LEVEL,
    vn_now,
    vn_today_str,
)
from utils.firebase_client import FirebaseClient  # noqa: E402
from utils.indicators import (  # noqa: E402
    add_technical_indicators,
    evaluate_intraday_alert,
    is_valid_price_tick,
)
from utils.market_calendar import (  # noqa: E402
    compute_weighted_session_progress,
    is_trading_day,
)
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("intraday_alert")


def _compute_session_progress() -> float:
    """Estimate fraction of VN trading day elapsed with ATO/ATC volume weighting."""
    return compute_weighted_session_progress()


def run_intraday_alert(force: bool = FORCE_SCAN) -> None:
    """Main entry point for intraday urgent alerts."""
    today_str = vn_today_str()
    fb = FirebaseClient()

    if not force and not is_trading_day(today_str):
        logger.info("Today (%s) is a weekend or VN market holiday — skipping intraday scan.", today_str)
        fb.record_heartbeat("intraday_alert", status="skipped_holiday", metrics={"date": today_str})
        return

    try:
        holdings = fb.get_holding_portfolio()
        if not holdings:
            logger.info("No holding positions in portfolio — exiting cleanly.")
            fb.record_heartbeat("intraday_alert", status="ok", metrics={"holdings": 0})
            return

        tickers = [h["ticker"] for h in holdings]
        logger.info("Checking intraday alerts for %d holding tickers: %s", len(tickers), tickers)

        vn = VnstockClient()
        live_map = vn.get_actual_prices_batch(tickers)
        sent_keys = fb.get_sent_intraday_keys(today_str)
        session_progress = _compute_session_progress()
        new_sent_keys: List[str] = []
        valid_checked = 0
        error_count = 0

        for pos in holdings:
            ticker = pos["ticker"]
            exchange = pos.get("exchange", "HOSE")
            live = live_map.get(ticker)
            if not live:
                logger.warning("No live price board data for %s — skipping.", ticker)
                error_count += 1
                continue

            current_price = float(live.get("actual_close", 0.0))
            ref_price = float(live.get("ref_price", 0.0))
            current_vol = float(live.get("volume", 0.0))

            if not is_valid_price_tick(current_price=current_price, ref_price=ref_price, exchange=exchange):
                logger.warning(
                    "Rejected abnormal price tick for %s (%s): price=%.0f, ref=%.0f",
                    ticker,
                    exchange,
                    current_price,
                    ref_price,
                )
                error_count += 1
                continue

            valid_checked += 1

            # Fetch daily OHLCV for MA50 & Vol_MA20 reference
            ma50_actual = float(pos.get("latest_ma50") or 0.0)
            vol_ma20 = 0.0
            try:
                df_adj = vn.get_ohlcv(ticker, days=120)
                if df_adj is not None and len(df_adj) >= 50:
                    data = add_technical_indicators(df_adj)
                    last_row = data.iloc[-1]
                    close_adj = float(last_row["close"])
                    ma50_adj = float(last_row["ma50"]) if not np.isnan(last_row["ma50"]) else 0.0
                    vol_ma20 = float(last_row["vol_ma20"]) if not np.isnan(last_row["vol_ma20"]) else 0.0
                    if close_adj > 0 and ma50_adj > 0 and ref_price > 0:
                        ma50_actual = round(ma50_adj * (ref_price / close_adj), 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not compute MA50/Vol_MA20 for %s: %s", ticker, exc)

            # Update live price & MA50 in Firestore portfolio so PWA/APK always displays fresh prices
            buy_price = float(pos.get("buy_price") or 0.0)
            old_high = float(pos.get("highest_price") or buy_price)
            pnl_pct = ((current_price - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
            pf_updates = {
                "latest_price": round(current_price, 0),
                "latest_pnl_pct": round(pnl_pct, 2),
                "last_checked": vn_now().strftime("%Y-%m-%d %H:%M"),
            }
            if ma50_actual > 0:
                pf_updates["latest_ma50"] = round(ma50_actual, 0)
            if current_price > old_high:
                pf_updates["highest_price"] = round(current_price, 0)
            fb.update_portfolio_fields(ticker, pf_updates)

            alerts = evaluate_intraday_alert(
                ticker=ticker,
                exchange=exchange,
                current_price=current_price,
                ref_price=ref_price,
                current_volume=current_vol,
                vol_ma20=vol_ma20,
                ma50_actual=ma50_actual,
                session_progress=session_progress,
            )

            for alert in alerts:
                dedup_key = f"{ticker}:{alert['alert_type']}"
                if dedup_key in sent_keys:
                    logger.info("Skipping duplicate intraday alert %s (already sent today).", dedup_key)
                    continue

                logger.warning("TRIGGERING INTRADAY ALERT: %s | %s", alert["title"], alert["body"])
                fb.send_fcm_notification(
                    title=alert["title"],
                    body=alert["body"],
                    data={
                        "type": "intraday_alert",
                        "ticker": ticker,
                        "alert_type": alert["alert_type"],
                        "date": today_str,
                    },
                )
                sent_keys.add(dedup_key)
                new_sent_keys.append(dedup_key)

        if new_sent_keys:
            fb.mark_intraday_keys_sent(today_str, new_sent_keys)
            logger.info("Sent %d new intraday alerts: %s", len(new_sent_keys), new_sent_keys)
        else:
            logger.info("No new intraday urgent alerts triggered.")

        err_rate = (error_count / len(holdings)) if holdings else 0.0
        status = "degraded" if err_rate > DATA_ERROR_ALERT_THRESHOLD else "ok"
        fb.record_heartbeat(
            "intraday_alert",
            status=status,
            metrics={
                "holdings": len(holdings),
                "valid_checked": valid_checked,
                "errors": error_count,
                "alerts_sent": len(new_sent_keys),
            },
        )
        if holdings and (valid_checked == 0 or err_rate >= max(0.5, DATA_ERROR_ALERT_THRESHOLD)):
            fb.send_system_alert(
                workflow="intraday_alert",
                title="⚠️ Cảnh báo hệ thống: Lỗi lấy giá Intraday",
                body=(
                    f"Không lấy được giá hợp lệ cho {error_count}/{len(holdings)} mã đang nắm giữ. "
                    "Kiểm tra kết nối API VCI/VNDirect."
                ),
                dedup_key="live_price_degraded",
            )
    except Exception as exc:
        logger.exception("Unhandled failure in run_intraday_alert: %s", exc)
        fb.record_heartbeat("intraday_alert", status="failed", error_message=str(exc))
        fb.send_system_alert(
            workflow="intraday_alert",
            title="🚨 Lỗi nghiêm trọng: Intraday Alert bị dừng",
            body=f"Workflow intraday_alert gặp lỗi: {str(exc)[:180]}",
            dedup_key="crash",
        )
        raise


if __name__ == "__main__":
    run_intraday_alert()
