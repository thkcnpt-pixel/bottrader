"""Unit tests for Phase 1 Stability & Hardening upgrades."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import VN_TZ
from utils.firebase_client import FirebaseClient
from utils.indicators import evaluate_intraday_alert, is_valid_price_tick
from utils.market_calendar import (
    compute_weighted_session_progress,
    has_fresh_daily_bar,
    is_trading_day,
)


class TestMarketCalendar(unittest.TestCase):
    def test_weekdays_weekends_and_holidays(self):
        # Regular Friday trading day
        self.assertTrue(is_trading_day("2026-11-20"))
        # Weekend (Saturday & Sunday)
        self.assertFalse(is_trading_day("2026-11-21"))
        self.assertFalse(is_trading_day("2026-11-22"))
        # Official VN market holidays (Tet 2026, 30/4, 2/9)
        self.assertFalse(is_trading_day("2026-02-17"))
        self.assertFalse(is_trading_day("2026-04-30"))
        self.assertFalse(is_trading_day("2026-09-02"))
        # Custom extra holiday via CSV
        self.assertFalse(is_trading_day("2026-11-20", extra_holidays_csv="2026-11-20, 2026-12-25"))

    def test_has_fresh_daily_bar(self):
        df = pd.DataFrame({
            "time": ["2026-11-19", "2026-11-20"],
            "close": [1250.0, 1260.0],
        })
        self.assertTrue(has_fresh_daily_bar(df, target_date="2026-11-20"))
        self.assertFalse(has_fresh_daily_bar(df, target_date="2026-11-23"))
        self.assertFalse(has_fresh_daily_bar(pd.DataFrame(), target_date="2026-11-20"))

    def test_weighted_session_progress_curve(self):
        t_pre = VN_TZ.localize(datetime(2026, 11, 20, 8, 45))
        t_ato_end = VN_TZ.localize(datetime(2026, 11, 20, 9, 15))
        t_lunch = VN_TZ.localize(datetime(2026, 11, 20, 12, 0))
        t_pre_atc = VN_TZ.localize(datetime(2026, 11, 20, 14, 30))
        t_close = VN_TZ.localize(datetime(2026, 11, 20, 14, 45))

        self.assertEqual(compute_weighted_session_progress(t_pre), 0.15)
        self.assertEqual(compute_weighted_session_progress(t_ato_end), 0.15)
        self.assertEqual(compute_weighted_session_progress(t_lunch), 0.55)
        self.assertEqual(compute_weighted_session_progress(t_pre_atc), 0.88)
        self.assertEqual(compute_weighted_session_progress(t_close), 1.0)


class TestPriceTickSanityFilter(unittest.TestCase):
    def test_hose_and_hnx_price_bands(self):
        # HOSE limit is ±7% (7.5% with rounding tolerance)
        self.assertTrue(is_valid_price_tick(32_000, 30_000, exchange="HOSE"))   # +6.67%
        self.assertTrue(is_valid_price_tick(27_950, 30_000, exchange="HOSE"))   # -6.83%
        self.assertFalse(is_valid_price_tick(35_000, 30_000, exchange="HOSE"))  # +16.6% (glitch)
        self.assertFalse(is_valid_price_tick(0, 30_000, exchange="HOSE"))
        self.assertFalse(is_valid_price_tick(-10_000, 30_000, exchange="HOSE"))

        # HNX limit is ±10% (10.5% with tolerance)
        self.assertTrue(is_valid_price_tick(32_900, 30_000, exchange="HNX"))    # +9.67%
        self.assertFalse(is_valid_price_tick(34_000, 30_000, exchange="HNX"))   # +13.33%

    def test_evaluate_intraday_alert_ignores_corrupted_ticks(self):
        # A +40% or -40% bad API tick on HOSE must NOT trigger false ceiling/floor alerts
        alerts_high = evaluate_intraday_alert(
            ticker="FPT",
            exchange="HOSE",
            current_price=140_000,
            ref_price=100_000,
            current_volume=2_000_000,
            vol_ma20=1_000_000,
            ma50_actual=98_000,
            session_progress=0.5,
        )
        self.assertEqual(alerts_high, [])

        alerts_low = evaluate_intraday_alert(
            ticker="FPT",
            exchange="HOSE",
            current_price=60_000,
            ref_price=100_000,
            current_volume=2_000_000,
            vol_ma20=1_000_000,
            ma50_actual=98_000,
            session_progress=0.5,
        )
        self.assertEqual(alerts_low, [])


class TestFirebaseConcurrencyAndHealth(unittest.TestCase):
    def _make_mock_client(self, existing_doc_data=None):
        client = object.__new__(FirebaseClient)
        client.dry_run = False
        doc_ref = MagicMock()
        if existing_doc_data is not None:
            doc_ref.get.return_value = SimpleNamespace(exists=True, to_dict=lambda: existing_doc_data)
        else:
            doc_ref.get.return_value = SimpleNamespace(exists=False, to_dict=lambda: {})
        col_ref = MagicMock()
        col_ref.document.return_value = doc_ref
        db = MagicMock()
        db.collection.return_value = col_ref
        client._db = db
        return client, doc_ref

    def test_monotonic_highest_price_protection(self):
        # Remote already recorded highest_price = 35,000; slower worker tries to write 33,000
        client, doc_ref = self._make_mock_client(
            {"ticker": "HPG", "status": "holding", "buy_price": 30_000, "highest_price": 35_000}
        )
        client.update_portfolio_fields("HPG", {"latest_price": 33_000, "highest_price": 33_000})
        doc_ref.set.assert_called_once()
        written_payload = doc_ref.set.call_args.args[0]
        self.assertEqual(written_payload["highest_price"], 35_000)

    def test_skips_tracking_update_when_position_already_closed(self):
        client, doc_ref = self._make_mock_client(
            {"ticker": "SSI", "status": "closed", "buy_price": 25_000, "highest_price": 28_000}
        )
        client.update_portfolio_fields("SSI", {"latest_price": 27_500})
        doc_ref.set.assert_not_called()

    def test_record_heartbeat_and_system_alert_dedup(self):
        client, doc_ref = self._make_mock_client()
        client.record_heartbeat("daily_scan", status="ok", metrics={"buy_candidates": 2})
        doc_ref.set.assert_called_once()
        hb_payload = doc_ref.set.call_args.args[0]
        self.assertEqual(hb_payload["workflows"]["daily_scan"]["status"], "ok")

        # Test deduplicated system alert
        client.get_sent_intraday_keys = MagicMock(return_value={"SYS:daily_scan:vnindex_missing"})
        client.send_fcm_notification = MagicMock(return_value=1)
        sent = client.send_system_alert(
            workflow="daily_scan",
            title="Alert",
            body="Body",
            dedup_key="vnindex_missing",
        )
        self.assertEqual(sent, 0)
        client.send_fcm_notification.assert_not_called()

    def test_firestore_rules_hardened(self):
        rules = (ROOT_DIR / "firestore.rules").read_text(encoding="utf-8")
        self.assertIn("email_verified == true", rules)
        self.assertIn("isValidPortfolioWrite", rules)
        self.assertIn("match /settings/fcm_tokens", rules)
        self.assertIn("match /settings/account", rules)


if __name__ == "__main__":
    unittest.main()
