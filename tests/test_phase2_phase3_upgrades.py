"""Unit tests for Phase 2 (Backtester, ATR Risk Manager, Market Regime) & Analytics/Provider Health."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.backtester import backtest_portfolio_universe, backtest_symbol, summarize_backtest_trades
from utils.data_providers import ProviderHealthRegistry
from utils.indicators import add_technical_indicators, compute_atr
from utils.news_scraper import detect_holding_negative_news
from utils.portfolio_analytics import compute_trade_journal_analytics
from utils.risk_manager import (
    compute_atr_stops,
    compute_quant_position_sizing,
    evaluate_market_regime,
)


def _make_ohlcv(closes, volumes=None, opens=None, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    closes_arr = np.array(closes, dtype=float)
    opens_arr = np.array(opens, dtype=float) if opens is not None else closes_arr * 0.995
    highs_arr = np.array(highs, dtype=float) if highs is not None else np.maximum(opens_arr, closes_arr) * 1.01
    lows_arr = np.array(lows, dtype=float) if lows is not None else np.minimum(opens_arr, closes_arr) * 0.99
    vols_arr = np.array(volumes, dtype=float) if volumes is not None else np.full(n, 1_000_000.0)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": opens_arr,
        "high": highs_arr,
        "low": lows_arr,
        "close": closes_arr,
        "volume": vols_arr,
    })


class TestPhase2RiskAndATR(unittest.TestCase):
    def test_atr_and_ma200_attached(self):
        df = _make_ohlcv(np.linspace(20_000, 30_000, 65))
        enriched = add_technical_indicators(df)
        self.assertIn("atr14", enriched.columns)
        self.assertIn("ma200", enriched.columns)
        atr_series = compute_atr(df, 14)
        self.assertGreater(float(atr_series.iloc[-1]), 0.0)

    def test_market_regime_bull_and_bear(self):
        df_bull = _make_ohlcv(np.linspace(1100, 1300, 70))
        reg_bull = evaluate_market_regime(df_bull)
        self.assertEqual(reg_bull["regime"], "BULL_STRONG")
        self.assertEqual(reg_bull["exposure_scale"], 1.0)

        df_bear = _make_ohlcv(np.linspace(1300, 1100, 70))
        reg_bear = evaluate_market_regime(df_bear)
        self.assertEqual(reg_bear["regime"], "BEAR_DEFENSIVE")
        self.assertLessEqual(reg_bear["exposure_scale"], 0.45)

    def test_atr_stops_and_position_sizing_sector_cap(self):
        stops = compute_atr_stops(entry_price=30_000, atr_actual=800, ma50_actual=28_500, highest_price=32_000)
        self.assertGreater(stops["stop_loss_vnd"], 27_000)
        self.assertLess(stops["stop_loss_vnd"], 30_000)
        self.assertGreater(stops["take_profit_target_vnd"], 30_000)
        self.assertEqual(stops["chandelier_trailing_stop_vnd"], 30_000)

        # Large account (10M NAV) where financial sector already holds 5.5M (55% > 50% cap)
        sizing_capped = compute_quant_position_sizing(
            entry_price=25_000,
            stop_loss_price=23_500,
            available_cash_vnd=4_500_000,
            total_nav_vnd=10_000_000,
            sector_group="financial",
            holdings=[{"ticker": "MBB", "quantity": 220, "latest_price": 25_000, "sector_group": "financial"}],
        )
        self.assertTrue(sizing_capped["sector_cap_reached"])
        self.assertEqual(sizing_capped["recommended_shares"], 0)

        # Non-financial ticker on the same account should NOT be sector-capped
        sizing_ok = compute_quant_position_sizing(
            entry_price=25_000,
            stop_loss_price=23_500,
            available_cash_vnd=4_500_000,
            total_nav_vnd=10_000_000,
            sector_group="non_financial",
            holdings=[{"ticker": "MBB", "quantity": 220, "latest_price": 25_000, "sector_group": "financial"}],
        )
        self.assertFalse(sizing_ok["sector_cap_reached"])
        self.assertGreater(sizing_ok["recommended_shares"], 0)


class TestPhase2Backtester(unittest.TestCase):
    def test_backtest_breakout_and_t_plus_settlement(self):
        # Construct 60-day uptrend + tight base + breakout + rally (+18%) + drop below MA50
        uptrend = [
            19_000 + i * 130 + (300 if i % 2 == 0 else -250)
            for i in range(48)
        ]
        tight_base = [
            25_200, 24_900, 25_150, 24_850, 25_100, 24_950,
            25_200, 24_900, 25_050, 24_850, 25_100, 24_950,
        ]
        breakout_and_rally = [25_950, 26_800, 28_000, 30_200, 29_500, 24_000]
        closes = uptrend + tight_base + breakout_and_rally
        highs = [c * 1.004 for c in uptrend + tight_base] + [c * 1.01 for c in breakout_and_rally]
        lows = [c * 0.996 for c in uptrend + tight_base] + [c * 0.99 for c in breakout_and_rally]
        vols = [1_000_000] * 60 + [2_000_000, 1_500_000, 1_400_000, 1_300_000, 1_200_000, 1_800_000]
        df = _make_ohlcv(closes, volumes=vols, highs=highs, lows=lows)

        res = backtest_symbol(df, ticker="FPT")
        self.assertGreaterEqual(len(res["trades"]), 1)
        first_trade = res["trades"][0]
        self.assertIn(first_trade["signal_type"], ("Pullback", "Breakout"))
        self.assertGreaterEqual(first_trade["holding_days"], 2)  # T+2.5 enforced
        self.assertTrue(first_trade["sold_half"])                # Reached +15% partial TP

        uni = backtest_portfolio_universe({"FPT": df})
        self.assertGreaterEqual(uni["overall"]["total_trades"], 1)

    def test_summarize_backtest_trades_metrics(self):
        sample_trades = [
            {"signal_type": "Pullback", "holding_days": 10, "net_return_pct": 12.5},
            {"signal_type": "Pullback", "holding_days": 6, "net_return_pct": -4.0},
            {"signal_type": "Breakout", "holding_days": 14, "net_return_pct": 18.0},
        ]
        summary = summarize_backtest_trades(sample_trades)
        self.assertEqual(summary["total_trades"], 3)
        self.assertAlmostEqual(summary["win_rate_pct"], 66.67, places=2)
        self.assertGreater(summary["profit_factor"], 5.0)
        self.assertIn("Pullback", summary["by_signal_type"])
        self.assertIn("Breakout", summary["by_signal_type"])


class TestAnalyticsNewsAndProviders(unittest.TestCase):
    def test_trade_journal_analytics(self):
        realized = [
            {"ticker": "FPT", "sell_date": "2026-10-15", "net_pnl": 120_000, "net_pnl_pct": 15.0},
            {"ticker": "HPG", "sell_date": "2026-10-28", "net_pnl": -40_000, "net_pnl_pct": -5.0},
            {"ticker": "FPT", "sell_date": "2026-11-10", "net_pnl": 80_000, "net_pnl_pct": 10.0},
        ]
        stats = compute_trade_journal_analytics(realized, initial_capital_vnd=1_000_000)
        self.assertEqual(stats["total_closed_trades"], 3)
        self.assertEqual(stats["win_count"], 2)
        self.assertEqual(stats["loss_count"], 1)
        self.assertEqual(stats["total_realized_pnl_vnd"], 160_000)
        self.assertEqual(stats["pnl_by_ticker"]["FPT"], 200_000)
        self.assertEqual(stats["pnl_by_month"]["2026-10"], 80_000)
        self.assertEqual(len(stats["equity_curve"]), 3)

    def test_negative_news_detector(self):
        news_bundle = {
            "articles": [
                {"title": "FPT ký hợp đồng AI lớn tại Nhật Bản", "summary": "Tăng trưởng tốt", "source": "CafeF"},
                {"title": "Cổ phiếu XYZ bị đình chỉ giao dịch do vi phạm công bố thông tin", "summary": "", "source": "HOSE"},
            ]
        }
        flagged = detect_holding_negative_news("XYZ", news_bundle)
        self.assertEqual(len(flagged), 1)
        self.assertIn("đình chỉ giao dịch", flagged[0]["matched_keywords"])

    def test_provider_health_registry(self):
        reg = ProviderHealthRegistry()
        reg.record_call("VCI", success=True, elapsed_ms=120.0)
        reg.record_call("VCI", success=False, elapsed_ms=280.0, error="timeout")
        snap = reg.snapshot()
        self.assertEqual(snap["VCI"]["calls"], 2)
        self.assertEqual(snap["VCI"]["success_rate_pct"], 50.0)
        self.assertEqual(snap["VCI"]["avg_latency_ms"], 200.0)
        self.assertEqual(snap["VCI"]["last_error"], "timeout")


if __name__ == "__main__":
    unittest.main()
