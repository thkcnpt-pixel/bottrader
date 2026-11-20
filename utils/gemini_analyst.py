"""Gemini Flash API 3-Perspective AI Debate & Risk Critic (Tier 4).

Design rules:
  - NEVER vetoes or removes a stock candidate.
  - Token-efficient single-call Structured Debate (~3,500 tokens/ticker = <1% Free Tier daily quota):
      * AI 1 (Phe Bò — Cơ bản & Động lực): ai_bull_thesis
      * AI 2 (Phe Gấu — Phản biện rủi ro): profit_quality, negative_news, dividend_trap, biggest_risk
      * AI 3 (Trọng tài — Chốt phương án tốt nhất cho vốn 500k): ai_referee_verdict
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
    ai_bull_thesis: str = Field(
        description="[AI 1 - Phe Bò] Luận điểm ủng hộ mua mạnh nhất từ BCTC lõi, tăng trưởng & kỹ thuật (2-3 câu ngắn gọn bằng tiếng Việt)."
    )
    profit_quality: str = Field(
        description="[AI 2 - Phe Gấu] Lợi nhuận đến từ kinh doanh lõi hay đột biến 1 lần? (2-3 câu bằng tiếng Việt)."
    )
    negative_news: str = Field(
        description="[AI 2 - Phe Gấu] Có tin xấu, kiện tụng, thanh tra hay lãnh đạo bán ra không? (2-3 câu bằng tiếng Việt)."
    )
    dividend_trap: str = Field(
        description="[AI 2 - Phe Gấu] Có sắp chia cổ tức bằng cổ phiếu/phát hành thêm gây kẹt lô lẻ vốn 500k không? (2-3 câu bằng tiếng Việt)."
    )
    biggest_risk: str = Field(
        description="[AI 2 - Phe Gấu] Rủi ro lớn nhất nếu mua mã này trong 1-3 tháng tới là gì? (2-3 câu bằng tiếng Việt)."
    )
    ai_referee_verdict: str = Field(
        description="[AI 3 - Trọng tài] Tổng kết phản biện giữa AI 1 và AI 2: chốt phương án hành động tốt nhất, điều kiện giải ngân hoặc hủy kèo cho nhà đầu tư vốn nhỏ 500k (2-3 câu thực chiến bằng tiếng Việt)."
    )


def _fallback_critique(reason: str) -> Dict[str, str]:
    msg = f"Chưa thể gọi Gemini API ({reason}). Vui lòng kiểm tra tin tức & lịch cổ tức thủ công trước khi đặt lệnh."
    return {
        "ai_bull_thesis": msg,
        "profit_quality": msg,
        "negative_news": msg,
        "dividend_trap": msg,
        "biggest_risk": msg,
        "ai_referee_verdict": msg,
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

    return f"""Hãy mô phỏng **Hội đồng 3 chuyên gia AI phản biện chéo nhau** về mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, phong cách Swing 1-3 tháng, nhà đầu tư vốn nhỏ ~500.000 VNĐ giao dịch lô lẻ):
- **AI 1 (Phe Bò — Cơ bản & Tăng trưởng):** Nêu luận điểm thuyết phục nhất để mua mã {ticker}.
- **AI 2 (Phe Gấu — Phản biện gay gắt):** Đập lại AI 1 bằng cách soi kỹ 4 rủi ro cốt lõi (chất lượng lợi nhuận, tin xấu/lãnh đạo bán, bẫy cổ tức/kẹt lô lẻ, rủi ro lớn nhất 1-3 tháng).
- **AI 3 (Trọng tài — Chốt phương án tốt nhất):** Cân bằng lập luận của cả AI 1 và AI 2 để rút ra kết luận thực chiến nhất (điểm cần theo dõi, kịch bản quản trị rủi ro). Không tự ý loại mã.

=== 1. TÓM TẮT BCTC 4-8 QUÝ GẦN NHẤT ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

Trả về JSON đúng 6 trường bằng tiếng Việt súc tích, nêu rõ số liệu nếu có:
1. ai_bull_thesis
2. profit_quality
3. negative_news
4. dividend_trap
5. biggest_risk
6. ai_referee_verdict
"""


class GeminiAnalyst:
    """Thin wrapper over google-genai for structured 3-AI debate JSON critique."""

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
        """Generate structured 3-AI debate JSON critique for a ticker."""
        if not self.api_key:
            logger.warning("GEMINI_API_KEY not set — returning fallback critique for %s", ticker)
            return _fallback_critique("thiếu GEMINI_API_KEY")

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
                        temperature=0.25,
                    ),
                )
                raw_text = (response.text or "").strip()
                parsed = json.loads(raw_text)
                return {
                    "ai_bull_thesis": str(parsed.get("ai_bull_thesis", "Không có dữ liệu.")),
                    "profit_quality": str(parsed.get("profit_quality", "Không có dữ liệu.")),
                    "negative_news": str(parsed.get("negative_news", "Không có dữ liệu.")),
                    "dividend_trap": str(parsed.get("dividend_trap", "Không có dữ liệu.")),
                    "biggest_risk": str(parsed.get("biggest_risk", "Không có dữ liệu.")),
                    "ai_referee_verdict": str(parsed.get("ai_referee_verdict", "Không có dữ liệu.")),
                }
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                logger.warning("Gemini model=%s failed for %s: %s", model_name, ticker, exc)

        return _fallback_critique(last_err[:120] or "lỗi kết nối")
