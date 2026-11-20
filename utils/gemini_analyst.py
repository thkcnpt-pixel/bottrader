"""Gemini Flash API Risk Critic (Tier 4).

Design rules:
  - NEVER vetoes or removes a stock candidate.
  - Returns structured JSON with exactly 4 fields:
      1. profit_quality
      2. negative_news
      3. dividend_trap
      4. biggest_risk
  - Sleeps 5s between calls to respect Free Tier RPM limits.
  - Uses gemini-2.5-flash with automatic fallback to gemini-2.0-flash.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from utils.config import (
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    GEMINI_SLEEP_SECONDS,
)

logger = logging.getLogger(__name__)


class RiskCritiqueSchema(BaseModel):
    profit_quality: str = Field(
        description="Lợi nhuận đến từ kinh doanh lõi hay đột biến 1 lần? Trả lời ngắn gọn 2-3 câu bằng tiếng Việt."
    )
    negative_news: str = Field(
        description="Có tin xấu, kiện tụng, thanh tra hay lãnh đạo bán ra không? Trả lời ngắn gọn 2-3 câu bằng tiếng Việt."
    )
    dividend_trap: str = Field(
        description="Có sắp chia cổ tức bằng cổ phiếu/phát hành thêm gây kẹt lô lẻ cho tài khoản vốn 500k không? Trả lời ngắn gọn 2-3 câu bằng tiếng Việt."
    )
    biggest_risk: str = Field(
        description="Rủi ro lớn nhất nếu mua mã này trong 1-3 tháng tới là gì? Trả lời ngắn gọn 2-3 câu bằng tiếng Việt."
    )


def _fallback_critique(reason: str) -> Dict[str, str]:
    msg = f"Chưa thể gọi Gemini API ({reason}). Vui lòng kiểm tra tin tức & lịch cổ tức thủ công trước khi đặt lệnh."
    return {
        "profit_quality": msg,
        "negative_news": msg,
        "dividend_trap": msg,
        "biggest_risk": msg,
    }


def _build_prompt(
    ticker: str,
    fundamental_summary: Dict[str, Any],
    news_bundle: Dict[str, Any],
) -> str:
    articles: List[Dict[str, str]] = news_bundle.get("articles", [])
    events: List[Dict[str, Any]] = news_bundle.get("events", [])

    news_lines = []
    for idx, a in enumerate(articles, 1):
        news_lines.append(
            f"[{idx}] ({a.get('source', '')} | {a.get('date', '')}) {a.get('title', '')}\n"
            f"Nội dung: {a.get('content', '')}"
        )
    news_text = "\n\n".join(news_lines) if news_lines else "Không tìm thấy bài báo nổi bật gần đây."

    event_lines = []
    for ev in events:
        event_lines.append(
            f"- {ev.get('title', '')} | GDKHQ: {ev.get('exright_date', '')} | Tỷ lệ: {ev.get('ratio', '')} | Giá trị: {ev.get('value', '')}"
        )
    events_text = "\n".join(event_lines) if event_lines else "Không có sự kiện/cổ tức gần đây."

    bctc_json = json.dumps(fundamental_summary, ensure_ascii=False, indent=2)

    return f"""Bạn là chuyên gia phản biện rủi ro chứng khoán Việt Nam (phong cách Swing / Trung hạn 1-3 tháng, nhà đầu tư cá nhân vốn nhỏ ~500.000 VNĐ giao dịch lô lẻ).
Nhiệm vụ của bạn KHÔNG phải khuyến nghị mua/bán, mà là SOI RỦI RO khách quan, sắc bén cho mã cổ phiếu **{ticker}** dựa trên dữ liệu dưới đây.

=== 1. TÓM TẮT BCTC 4-8 QUÝ GẦN NHẤT ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

Hãy trả về JSON đúng 4 trường bằng tiếng Việt súc tích, thực tế (mỗi trường 2-3 câu, nêu rõ con số hoặc sự kiện nếu có; nếu không có thông tin xấu hãy ghi rõ "Chưa ghi nhận..."):
1. profit_quality: Lợi nhuận đến từ hoạt động kinh doanh lõi hay đột biến 1 lần (tài chính, bán tài sản)?
2. negative_news: Có tin xấu, kiện tụng, thanh tra, phạt thuế hay lãnh đạo đăng ký bán ra không?
3. dividend_trap: Có sắp chia cổ tức bằng cổ phiếu / phát hành thêm quyền mua gây kẹt cổ phiếu lô lẻ hoặc pha loãng không?
4. biggest_risk: Rủi ro lớn nhất nếu mua nắm giữ mã {ticker} trong 1-3 tháng tới là gì?
"""


class GeminiAnalyst:
    """Thin wrapper over google-genai for structured JSON risk critique."""

    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        primary_model: str = GEMINI_MODEL,
        fallback_model: str = GEMINI_FALLBACK_MODEL,
        sleep_seconds: float = GEMINI_SLEEP_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.models = [primary_model]
        if fallback_model and fallback_model != primary_model:
            self.models.append(fallback_model)
        self.sleep_seconds = sleep_seconds
        self._call_count = 0

    def analyze_risk(
        self,
        ticker: str,
        fundamental_summary: Dict[str, Any],
        news_bundle: Dict[str, Any],
    ) -> Dict[str, str]:
        """Generate structured 4-field risk critique JSON for a ticker."""
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set — returning fallback critique for %s", ticker)
            return _fallback_critique("thiếu GEMINI_API_KEY")

        # Rate-limit protection between consecutive API calls
        if self._call_count > 0 and self.sleep_seconds > 0:
            logger.info("Sleeping %.1fs before next Gemini API call...", self.sleep_seconds)
            time.sleep(self.sleep_seconds)
        self._call_count += 1

        prompt = _build_prompt(ticker, fundamental_summary, news_bundle)

        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            return _fallback_critique(f"chưa cài google-genai: {exc}")

        client = genai.Client(api_key=self.api_key)
        last_err = ""

        for model_name in self.models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RiskCritiqueSchema,
                        temperature=0.2,
                    ),
                )
                raw_text = (response.text or "").strip()
                parsed = json.loads(raw_text)
                return {
                    "profit_quality": str(parsed.get("profit_quality", "Không có dữ liệu.")),
                    "negative_news": str(parsed.get("negative_news", "Không có dữ liệu.")),
                    "dividend_trap": str(parsed.get("dividend_trap", "Không có dữ liệu.")),
                    "biggest_risk": str(parsed.get("biggest_risk", "Không có dữ liệu.")),
                }
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                logger.warning("Gemini model=%s failed for %s: %s", model_name, ticker, exc)

        return _fallback_critique(last_err[:120] or "lỗi kết nối")
