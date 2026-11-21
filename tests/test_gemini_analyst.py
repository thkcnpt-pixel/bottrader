"""Unit tests for Multi-AI GeminiAnalyst retry, live account sizing, debate, and fallback behavior."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import (
    CKEY_MODEL,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    OPENROUTER_MODEL,
)
from utils.gemini_analyst import GeminiAnalyst, _extract_json_dict

VALID_JSON = json.dumps(
    {
        "ai_bull_thesis": "Tăng trưởng lõi tốt.",
        "profit_quality": "Lợi nhuận từ mảng cốt lõi.",
        "negative_news": "Không có tin xấu.",
        "dividend_trap": "Không có lịch chia cổ tức cổ phiếu gần đây.",
        "biggest_risk": "Áp lực chốt lời ngắn hạn.",
        "ai_referee_verdict": "Có thể giải ngân thăm dò.",
        "suggested_buy_price": "Đặt mua LO quanh 41.000 - 41.500 đ, SL 38.500 đ, TP 48.000 đ.",
        "suggested_buy_quantity": "Mua 12 CP lô lẻ (~495.000 đ), chia 2 đợt mỗi đợt 6 CP.",
        "decision": "BUY",
        "confidence_score": 82,
    },
    ensure_ascii=False,
)

BULL_JSON = json.dumps({"ai_bull_thesis": "ROE 22%, lợi nhuận lõi tăng 30% YoY."}, ensure_ascii=False)
BEAR_JSON = json.dumps(
    {
        "profit_quality": "Biên lãi gộp ổn định, không có đột biến 1 lần.",
        "negative_news": "Chưa ghi nhận tin xấu.",
        "dividend_trap": "Chưa có lịch chốt quyền cổ tức cổ phiếu.",
        "biggest_risk": "Cận kháng cự 60 phiên quanh 43.000 đ.",
    },
    ensure_ascii=False,
)
REFEREE_JSON = json.dumps(
    {
        "decision": "BUY",
        "confidence_score": 78,
        "ai_referee_verdict": "Cân bằng tốt giữa tăng trưởng và rủi ro kháng cự; giải ngân từng phần.",
        "suggested_buy_price": "Đặt LO 40.500 - 41.000 đ, SL 38.500 đ, TP 47.000 đ.",
        "suggested_buy_quantity": "Mua 12 CP (~492.000 đ), chia 2 đợt mỗi đợt 6 CP.",
    },
    ensure_ascii=False,
)


def _mock_http_response(content: str, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = content
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


class TestGeminiAnalyst(unittest.TestCase):
    def test_default_models_updated(self):
        self.assertEqual(GEMINI_MODEL, "gemini-3.8-flash")
        self.assertEqual(GEMINI_FALLBACK_MODEL, "gemini-3.5-flash-lite")
        self.assertEqual(CKEY_MODEL, "deepseek-v4.1-flash")
        self.assertEqual(OPENROUTER_MODEL, "nvidia/nemotron-3-ultra-550b-a55b:free")

    def test_extract_json_strips_think_and_fences(self):
        raw = "<think>Phân tích kỹ...</think>\n```json\n{\"decision\": \"BUY\", \"confidence_score\": 80}\n```"
        parsed = _extract_json_dict(raw)
        self.assertEqual(parsed["decision"], "BUY")
        self.assertEqual(parsed["confidence_score"], 80)

    def test_retries_transient_503_before_succeeding(self):
        fake_models = MagicMock()
        fake_models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand."),
            SimpleNamespace(text=VALID_JSON),
        ]
        fake_client = SimpleNamespace(models=fake_models)

        with patch("google.genai.Client", return_value=fake_client):
            analyst = GeminiAnalyst(
                api_key="test-key",
                ckey_api_key="",
                openrouter_api_key="",
                primary_model="gemini-3.8-flash",
                fallback_model="gemini-3.5-flash-lite",
                sleep_seconds=0,
                max_retries_per_model=2,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk("VIC", {"roe_pct": 15.0}, {"articles": [], "events": []})

        self.assertEqual(result["ai_bull_thesis"], "Tăng trưởng lõi tốt.")
        self.assertEqual(result["decision"], "BUY")
        self.assertEqual(result["confidence_score"], 82)
        self.assertIn("41.000", result["suggested_buy_price"])
        self.assertIn("12 CP", result["suggested_buy_quantity"])
        self.assertEqual(fake_models.generate_content.call_count, 2)
        called_models = [call.kwargs["model"] for call in fake_models.generate_content.call_args_list]
        self.assertEqual(called_models, ["gemini-3.8-flash", "gemini-3.8-flash"])

    def test_falls_back_after_exhausting_503_retries(self):
        fake_models = MagicMock()
        fake_models.generate_content.side_effect = [
            RuntimeError("503 UNAVAILABLE"),
            RuntimeError("503 UNAVAILABLE"),
            SimpleNamespace(text=VALID_JSON),
        ]
        fake_client = SimpleNamespace(models=fake_models)

        with patch("google.genai.Client", return_value=fake_client):
            analyst = GeminiAnalyst(
                api_key="test-key",
                ckey_api_key="",
                openrouter_api_key="",
                primary_model="gemini-3.8-flash",
                fallback_model="gemini-3.5-flash-lite",
                sleep_seconds=0,
                max_retries_per_model=1,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk("VIC", {}, {})

        self.assertEqual(result["ai_referee_verdict"], "Có thể giải ngân thăm dò.")
        called_models = [call.kwargs["model"] for call in fake_models.generate_content.call_args_list]
        self.assertEqual(
            called_models,
            ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )

    def test_non_transient_404_fails_fast_to_fallback(self):
        fake_models = MagicMock()
        fake_models.generate_content.side_effect = [
            RuntimeError("404 NOT_FOUND. Model not found."),
            SimpleNamespace(text=VALID_JSON),
        ]
        fake_client = SimpleNamespace(models=fake_models)

        with patch("google.genai.Client", return_value=fake_client):
            analyst = GeminiAnalyst(
                api_key="test-key",
                ckey_api_key="",
                openrouter_api_key="",
                primary_model="gemini-old-model",
                fallback_model="gemini-3.5-flash-lite",
                sleep_seconds=0,
                max_retries_per_model=2,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk("VIC", {}, {})

        self.assertEqual(result["profit_quality"], "Lợi nhuận từ mảng cốt lõi.")
        called_models = [call.kwargs["model"] for call in fake_models.generate_content.call_args_list]
        self.assertEqual(called_models, ["gemini-old-model", "gemini-3.5-flash-lite"])

    def test_build_prompt_uses_live_account_balance_instead_of_500k(self):
        from utils.gemini_analyst import _build_prompt

        prompt = _build_prompt(
            "HPG",
            {
                "actual_close_vnd": 25_000,
                "account_context": {
                    "available_cash_vnd": 1_250_000,
                    "holding_stock_value_vnd": 750_000,
                    "total_nav_vnd": 2_000_000,
                    "max_affordable_shares_with_current_cash": 50,
                    "current_holdings": ["FPT (6 CP ~750,000 đ)"],
                },
            },
            {"articles": [], "events": []},
        )
        self.assertIn("1,250,000 VNĐ", prompt)
        self.assertIn("2,000,000 VNĐ", prompt)
        self.assertIn("50 CP", prompt)
        self.assertNotIn("~500.000 VNĐ", prompt)

    def test_multi_ai_3_provider_debate_and_guardrail(self):
        fake_models = MagicMock()
        fake_models.generate_content.return_value = SimpleNamespace(text=BULL_JSON)
        fake_client = SimpleNamespace(models=fake_models)

        def fake_post(url, headers=None, json=None, timeout=None):
            if "xah.io" in url:
                return _mock_http_response(f"<think>checking</think>{BEAR_JSON}")
            if "openrouter.ai" in url:
                return _mock_http_response(f"```json\n{REFEREE_JSON}\n```")
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("google.genai.Client", return_value=fake_client), patch(
            "utils.gemini_analyst.requests.post", side_effect=fake_post
        ):
            analyst = GeminiAnalyst(
                api_key="gem-key",
                ckey_api_key="ckey-key",
                openrouter_api_key="or-key",
                sleep_seconds=0,
                max_retries_per_model=0,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk(
                "FPT",
                {
                    "vnindex_regime": {"warning_flag": True},
                    "reference_rr_ratio": 1.2,
                },
                {"articles": [], "events": []},
            )

        self.assertEqual(result["ai_bull_thesis"], "ROE 22%, lợi nhuận lõi tăng 30% YoY.")
        self.assertIn("kháng cự 60 phiên", result["biggest_risk"])
        self.assertEqual(result["decision"], "BUY")
        self.assertEqual(result["confidence_score"], 78)
        self.assertIn("VN-Index đang dưới MA50", result["ai_referee_verdict"])
        self.assertIn("R:R tham chiếu thấp", result["ai_referee_verdict"])
        self.assertEqual(result["ai_models_used"]["bull"], "gemini:gemini-3.8-flash")
        self.assertEqual(result["ai_models_used"]["bear"], "ckey:deepseek-v4.1-flash")
        self.assertEqual(
            result["ai_models_used"]["referee"],
            "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        )

    def test_cross_provider_fallback_when_ckey_times_out(self):
        fake_models = MagicMock()
        fake_models.generate_content.return_value = SimpleNamespace(text=BULL_JSON)
        fake_client = SimpleNamespace(models=fake_models)

        or_calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            if "xah.io" in url:
                raise TimeoutError("Read timed out")
            if "openrouter.ai" in url:
                or_calls.append(json["model"])
                if len(or_calls) == 1:
                    return _mock_http_response(BEAR_JSON)
                return _mock_http_response(REFEREE_JSON)
            raise AssertionError(f"Unexpected URL: {url}")

        with patch("google.genai.Client", return_value=fake_client), patch(
            "utils.gemini_analyst.requests.post", side_effect=fake_post
        ):
            analyst = GeminiAnalyst(
                api_key="gem-key",
                ckey_api_key="ckey-key",
                openrouter_api_key="or-key",
                sleep_seconds=0,
                max_retries_per_model=0,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk("MBB", {}, {})

        self.assertEqual(result["ai_models_used"]["bull"], "gemini:gemini-3.8-flash")
        self.assertTrue(result["ai_models_used"]["bear"].startswith("openrouter:"))
        self.assertTrue(result["ai_models_used"]["referee"].startswith("openrouter:"))
        self.assertEqual(result["decision"], "BUY")


if __name__ == "__main__":
    unittest.main()
