"""Unit tests for core Tier 1-4 algorithms (pure Python, no external network calls)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.update_fundamentals import score_financial, score_non_financial
from utils.gemini_analyst import GeminiAnalyst
from utils.indicators import (
    compute_rsi,
    compute_sma,
    detect_buy_signal,
    evaluate_intraday_alert,
    evaluate_portfolio_position,
)
from utils.news_scraper import clean_html_to_text, truncate_words


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


class TestFundamentalScoring(unittest.TestCase):
    def test_strong_non_financial_scores_high(self):
        metrics = {
            "rev_yoy_pct": 18.0,
            "profit_yoy_pct": 22.0,
            "roe_pct": 21.0,
            "ocf_ttm": 500_000_000_000.0,
            "ocf_to_net_profit": 1.1,
            "de_ratio": 0.45,
            "pe": 12.5,
            "pe_hist_median": 14.0,
        }
        score = score_non_financial(metrics)
        self.assertGreaterEqual(score, 90.0)

    def test_weak_non_financial_scores_below_65(self):
        metrics = {
            "rev_yoy_pct": -5.0,
            "profit_yoy_pct": -12.0,
            "roe_pct": 6.0,
            "ocf_ttm": -100_000_000.0,
            "ocf_to_net_profit": -0.5,
            "de_ratio": 2.4,
            "pe": 35.0,
            "pe_hist_median": 15.0,
        }
        self.assertLess(score_non_financial(metrics), 65.0)

    def test_strong_financial_scores_high(self):
        metrics = {
            "profit_yoy_pct": 24.0,
            "roe_pct": 21.5,
            "npl_pct": 1.2,
            "llr_pct": 125.0,
            "pb": 1.6,
        }
        self.assertGreaterEqual(score_financial(metrics), 85.0)


class TestTechnicalIndicatorsAndSignals(unittest.TestCase):
    def test_sma_and_rsi_bounds(self):
        s = pd.Series(np.linspace(10_000, 20_000, 60))
        ma20 = compute_sma(s, 20)
        rsi = compute_rsi(s, 14)
        self.assertAlmostEqual(ma20.iloc[-1], s.tail(20).mean(), places=4)
        self.assertGreater(rsi.iloc[-1], 70.0)
        self.assertLessEqual(rsi.iloc[-1], 100.0)

    def test_detect_pullback_signal(self):
        # Construct steady uptrend then gentle pullback to MA20 with contracting volume and green bounce
        base = list(np.linspace(20_000, 31_000, 52))
        pullback = [30_700, 30_300, 29_900, 29_600, 29_300, 29_100, 29_450]
        closes = base + pullback
        opens = [c * 0.995 for c in base] + [30_800, 30_500, 30_100, 29_800, 29_500, 29_250, 29_100]
        vols = [1_200_000] * 52 + [1_000_000, 900_000, 800_000, 700_000, 650_000, 600_000, 1_100_000]
        df = _make_ohlcv(closes, volumes=vols, opens=opens)

        sig = detect_buy_signal(df)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["signal_type"], "Pullback")
        self.assertEqual(sig["signal_priority"], 1)

    def test_detect_breakout_signal(self):
        # Realistic oscillating uptrend followed by 12-day tight base (< 8% range) then high-volume breakout
        uptrend = [
            19_000 + i * 130 + (300 if i % 2 == 0 else -250)
            for i in range(48)
        ]
        tight_base = [
            25_200, 24_900, 25_150, 24_850, 25_100, 24_950,
            25_200, 24_900, 25_050, 24_850, 25_100, 24_950,
        ]
        breakout_day = [25_950]
        closes = uptrend + tight_base + breakout_day
        highs = [c * 1.004 for c in uptrend + tight_base] + [26_050]
        lows = [c * 0.996 for c in uptrend + tight_base] + [24_950]
        vols = [1_000_000] * 60 + [2_000_000]  # 2.0x Vol_MA20
        df = _make_ohlcv(closes, volumes=vols, highs=highs, lows=lows)

        sig = detect_buy_signal(df)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["signal_type"], "Breakout")
        self.assertEqual(sig["signal_priority"], 2)

    def test_downtrend_rejected(self):
        closes = list(np.linspace(35_000, 20_000, 65))
        df = _make_ohlcv(closes)
        self.assertIsNone(detect_buy_signal(df))

    def test_detect_watchlist_candidate(self):
        from utils.indicators import detect_watchlist_candidate

        df_up = _make_ohlcv(list(np.linspace(20_000, 35_000, 65)))
        w = detect_watchlist_candidate(df_up)
        self.assertIsNotNone(w)
        self.assertTrue(w["trend_aligned"])
        self.assertGreater(w["dist_to_ma50_pct"], 0)
        self.assertIn("MA20", w["watch_reason"])

        df_down = _make_ohlcv(list(np.linspace(35_000, 20_000, 65)))
        self.assertIsNone(detect_watchlist_candidate(df_down))


class TestPortfolioAndIntradayAlerts(unittest.TestCase):
    def test_portfolio_take_profit_half(self):
        closes = list(np.linspace(20_000, 25_000, 60))
        df = _make_ohlcv(closes)
        pos = {"ticker": "FPT", "buy_price": 20_000, "highest_price": 24_000, "sold_half": False}
        res = evaluate_portfolio_position(pos, df, actual_close=25_000)
        self.assertEqual(res["action"], "TAKE_PROFIT_HALF")
        self.assertEqual(res["highest_price"], 25_000)

    def test_portfolio_trailing_stop_after_half(self):
        closes = list(np.linspace(20_000, 30_000, 58)) + [27_200, 27_000]
        df = _make_ohlcv(closes)
        pos = {"ticker": "HPG", "buy_price": 20_000, "highest_price": 30_000, "sold_half": True}
        res = evaluate_portfolio_position(pos, df, actual_close=27_000)
        self.assertEqual(res["action"], "SELL_ALL")
        self.assertTrue(any(a["type"] == "TRAILING_STOP_8PCT" for a in res["alerts"]))

    def test_intraday_urgent_conditions(self):
        # Sharp drop -4.2% with high volume & deep MA50 breach
        alerts = evaluate_intraday_alert(
            ticker="SSI",
            exchange="HOSE",
            current_price=28_740,
            ref_price=30_000,
            current_volume=1_500_000,
            vol_ma20=1_000_000,
            ma50_actual=30_000,
            session_progress=0.5,
        )
        types = {a["alert_type"] for a in alerts}
        self.assertIn("SHARP_DROP_HIGH_VOL", types)
        self.assertIn("DEEP_MA50_OR_FLOOR", types)

        # Ceiling surge >= +6.5%
        surge_alerts = evaluate_intraday_alert(
            ticker="MBB",
            exchange="HOSE",
            current_price=26_700,
            ref_price=25_000,
            current_volume=2_000_000,
            vol_ma20=1_500_000,
            ma50_actual=23_000,
            session_progress=0.5,
        )
        self.assertTrue(any(a["alert_type"] == "CEILING_SURGE" for a in surge_alerts))


class TestNewsAndGeminiAndFrontendRules(unittest.TestCase):
    def test_html_clean_and_truncate(self):
        raw = "<div><script>bad()</script><p>Cổ phiếu FPT tăng mạnh.</p></div>"
        self.assertEqual(clean_html_to_text(raw), "Cổ phiếu FPT tăng mạnh.")
        long_text = " ".join(["word"] * 1200)
        truncated = truncate_words(long_text, max_words=900)
        self.assertEqual(len(truncated.split()), 900)

    def test_gemini_fallback_never_vetoes(self):
        analyst = GeminiAnalyst(api_key="", ckey_api_key="", openrouter_api_key="")
        res = analyst.analyze_risk("FPT", {}, {"articles": [], "events": []})
        self.assertEqual(
            set(res.keys()),
            {
                "ai_bull_thesis",
                "profit_quality",
                "negative_news",
                "dividend_trap",
                "biggest_risk",
                "ai_referee_verdict",
                "suggested_buy_price",
                "suggested_buy_quantity",
                "decision",
                "confidence_score",
                "ai_models_used",
            },
        )

    def test_frontend_ui_constraints(self):
        import re
        html = (ROOT_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
        js = (ROOT_DIR / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Dev by Thkc", html)
        self.assertNotIn("qrcode", html.lower())
        self.assertNotIn("qr-code", html.lower())
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        js_ids = set(re.findall(r'\$\("([^"]+)"\)', js))
        missing_ids = sorted(js_ids - html_ids)
        self.assertEqual(missing_ids, [], f"Missing DOM IDs in index.html: {missing_ids}")


if __name__ == "__main__":
    unittest.main()
