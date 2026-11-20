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

from utils.config import LOG_LEVEL, vn_now, vn_today_str  # noqa: E402
from utils.firebase_client import FirebaseClient  # noqa: E402
from utils.indicators import add_technical_indicators, evaluate_intraday_alert  # noqa: E402
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("intraday_alert")


def _compute_session_progress() -> float:
    """Estimate fraction of VN trading day elapsed (09:00-11:30 & 13:00-14:45 = 255 mins)."""
    now = vn_now()
    mins = now.hour * 60 + now.minute
    if mins <= 9 * 60:
        return 0.15
    if mins <= 11 * 60 + 30:
        elapsed = mins - 9 * 60
    elif mins < 13 * 60:
        elapsed = 150
    elif mins <= 14 * 60 + 45:
        elapsed = 150 + (mins - 13 * 60)
    else:
        elapsed = 255
    return max(0.15, min(1.0, elapsed / 255.0))


def run_intraday_alert() -> None:
    """Main entry point for intraday urgent alerts."""
    today_str = vn_today_str()
    fb = FirebaseClient()

    holdings = fb.get_holding_portfolio()
    if not holdings:
        logger.info("No holding positions in portfolio — exiting cleanly.")
        return

    tickers = [h["ticker"] for h in holdings]
    logger.info("Checking intraday alerts for %d holding tickers: %s", len(tickers), tickers)

    vn = VnstockClient()
    live_map = vn.get_actual_prices_batch(tickers)
    sent_keys = fb.get_sent_intraday_keys(today_str)
    session_progress = _compute_session_progress()
    new_sent_keys: List[str] = []

    for pos in holdings:
        ticker = pos["ticker"]
        exchange = pos.get("exchange", "HOSE")
        live = live_map.get(ticker)
        if not live:
            logger.warning("No live price board data for %s — skipping.", ticker)
            continue

        current_price = float(live.get("actual_close", 0.0))
        ref_price = float(live.get("ref_price", 0.0))
        current_vol = float(live.get("volume", 0.0))

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


if __name__ == "__main__":
    run_intraday_alert()
