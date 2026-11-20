"""Unit tests for GeminiAnalyst retry and fallback behavior."""

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

from utils.config import GEMINI_FALLBACK_MODEL, GEMINI_MODEL
from utils.gemini_analyst import GeminiAnalyst

VALID_JSON = json.dumps(
    {
        "ai_bull_thesis": "Tăng trưởng lõi tốt.",
        "profit_quality": "Lợi nhuận từ mảng cốt lõi.",
        "negative_news": "Không có tin xấu.",
        "dividend_trap": "Không có lịch chia cổ tức cổ phiếu gần đây.",
        "biggest_risk": "Áp lực chốt lời ngắn hạn.",
        "ai_referee_verdict": "Có thể giải ngân thăm dò.",
    },
    ensure_ascii=False,
)


class TestGeminiAnalyst(unittest.TestCase):
    def test_default_models_updated(self):
        self.assertEqual(GEMINI_MODEL, "gemini-3.8-flash")
        self.assertEqual(GEMINI_FALLBACK_MODEL, "gemini-3.5-flash-lite")

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
                primary_model="gemini-3.8-flash",
                fallback_model="gemini-3.5-flash-lite",
                sleep_seconds=0,
                max_retries_per_model=2,
                retry_backoff_seconds=0,
            )
            result = analyst.analyze_risk("VIC", {"roe_pct": 15.0}, {"articles": [], "events": []})

        self.assertEqual(result["ai_bull_thesis"], "Tăng trưởng lõi tốt.")
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


if __name__ == "__main__":
    unittest.main()
