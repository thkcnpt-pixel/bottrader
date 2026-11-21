"""Gemini Flash API 3-Perspective AI Debate & Risk Critic (Tier 4).

Design rules:
  - NEVER vetoes or removes a stock candidate.
  - Token-efficient single-call Structured Debate (~3,500 tokens/ticker = <1% Free Tier daily quota):
      * AI 1 (Phe Bò — Cơ bản & Động lực): ai_bull_thesis
      * AI 2 (Phe Gấu — Phản biện rủi ro): profit_quality, negative_news, dividend_trap, biggest_risk
      * AI 3 (Trọng tài — Chốt phương án tốt nhất theo số dư tài khoản thực tế): ai_referee_verdict
  - Sleeps 5s between calls to respect Free Tier RPM limits.
  - Uses gemini-3.8-flash with automatic retry on transient 503/429 and fallback to gemini-3.5-flash-lite.
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

_TRANSIENT_ERROR_MARKERS = (
    "503",
    "429",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "OVERLOADED",
    "TRY AGAIN LATER",
)


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


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
        description="[AI 2 - Phe Gấu] Có sắp chia cổ tức bằng cổ phiếu/phát hành thêm gây chôn vốn hoặc kẹt cổ phiếu lẻ không? (2-3 câu bằng tiếng Việt)."
    )
    biggest_risk: str = Field(
        description="[AI 2 - Phe Gấu] Rủi ro lớn nhất nếu mua mã này trong 1-3 tháng tới là gì? (2-3 câu bằng tiếng Việt)."
    )
    ai_referee_verdict: str = Field(
        description="[AI 3 - Trọng tài] Tổng kết phản biện giữa AI 1 và AI 2: chốt phương án hành động tốt nhất, điều kiện giải ngân hoặc đứng ngoài dựa trực tiếp trên số dư tiền mặt khả dụng và danh mục hiện có của tài khoản (2-3 câu thực chiến bằng tiếng Việt)."
    )
    suggested_buy_price: str = Field(
        description="[Nên đặt giá bao nhiêu] Vùng giá / mức giá đặt lệnh mua LO cụ thể (VNĐ) dựa trên giá đóng cửa, MA20, MA50, kèm mức cắt lỗ (SL) và chốt lời (TP) tham khảo (1-2 câu súc tích bằng tiếng Việt)."
    )
    suggested_buy_quantity: str = Field(
        description="[Nên mua bao nhiêu] Khối lượng cổ phiếu (CP) và số tiền giải ngân gợi ý tính trực tiếp trên Số dư tiền mặt khả dụng hiện có của tài khoản (`available_cash_vnd`), nên mua 1 lần hay chia đợt, hoặc báo rõ nếu tiền mặt hiện có không đủ mua 1 CP (1-2 câu súc tích bằng tiếng Việt)."
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
        "suggested_buy_price": msg,
        "suggested_buy_quantity": msg,
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

    acc_ctx = fundamental_summary.get("account_context") or {}
    available_cash_vnd = int(float(acc_ctx.get("available_cash_vnd") or 0))
    holding_stock_vnd = int(float(acc_ctx.get("holding_stock_value_vnd") or 0))
    total_nav_vnd = int(float(acc_ctx.get("total_nav_vnd") or 0))
    max_shares = int(float(acc_ctx.get("max_affordable_shares_with_current_cash") or 0))
    holdings_list = acc_ctx.get("current_holdings") or []
    holdings_str = ", ".join(holdings_list) if holdings_list else "Chưa nắm giữ mã nào"

    if available_cash_vnd > 0 or total_nav_vnd > 0:
        account_prompt_block = (
            f"- **Thông tin tài khoản thực tế của nhà đầu tư:**\n"
            f"  + Tiền mặt khả dụng (Sức mua hiện có): **{available_cash_vnd:,} VNĐ**\n"
            f"  + Giá trị cổ phiếu đang nắm giữ: **{holding_stock_vnd:,} VNĐ** ({holdings_str})\n"
            f"  + Tổng tài sản ròng (NAV): **{total_nav_vnd:,} VNĐ**\n"
            f"  + Số lượng cổ phiếu {ticker} tối đa mua được bằng tiền mặt hiện có: **{max_shares} CP**"
        )
        qty_instruction = (
            f"  + **Nên mua bao nhiêu (`suggested_buy_quantity`):** BẮT BUỘC tính trực tiếp trên **Số dư tiền mặt khả dụng hiện có là {available_cash_vnd:,} VNĐ** "
            f"(tối đa mua được {max_shares} CP theo thị giá hiện tại). Đề xuất rõ số lượng CP nên mua, số tiền tương ứng, nên giải ngân 1 lần hay chia đợt dựa trên tỷ trọng tiền mặt/NAV hiện tại; "
            f"nếu tiền mặt khả dụng ({available_cash_vnd:,} VNĐ) không đủ mua 1 CP thì ghi rõ là sức mua hiện tại chưa đủ và cần nạp thêm hoặc cơ cấu danh mục."
        )
    else:
        account_prompt_block = (
            "- **Thông tin tài khoản:** Nhà đầu tư chưa đồng bộ số dư tiền mặt (`available_cash_vnd = 0`). "
            "Hãy gợi ý khối lượng mua theo tỷ trọng phần trăm sức mua thực tế của tài khoản và nhắc nhà đầu tư cập nhật số dư tiền mặt trong mục Danh mục."
        )
        qty_instruction = (
            "  + **Nên mua bao nhiêu (`suggested_buy_quantity`):** Do tài khoản chưa cập nhật số dư tiền mặt khả dụng, "
            "hãy tư vấn tỷ trọng giải ngân khuyến nghị (% sức mua hiện có) kèm đơn giá 1 CP hoặc 10 CP để nhà đầu tư tự nhân với số dư thực tế."
        )

    bctc_json = json.dumps(fundamental_summary, ensure_ascii=False, indent=2)

    return f"""Hãy mô phỏng **Hội đồng 3 chuyên gia AI phản biện chéo nhau** về mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, phong cách Swing 1-3 tháng, hỗ trợ cả giao dịch lô lẻ và lô chẵn tùy theo số dư tài khoản thực tế của nhà đầu tư):
{account_prompt_block}
- **AI 1 (Phe Bò — Cơ bản & Tăng trưởng):** Nêu luận điểm thuyết phục nhất để mua mã {ticker}.
- **AI 2 (Phe Gấu — Phản biện gay gắt):** Đập lại AI 1 bằng cách soi kỹ 4 rủi ro cốt lõi (chất lượng lợi nhuận, tin xấu/lãnh đạo bán, bẫy cổ tức/phát hành thêm, rủi ro lớn nhất 1-3 tháng).
- **AI 3 (Trọng tài — Chốt phương án tốt nhất theo tài khoản hiện có):** Cân bằng lập luận của cả AI 1 và AI 2, kết hợp với **số dư tiền mặt khả dụng và danh mục đang nắm giữ** của nhà đầu tư để rút ra kết luận thực chiến nhất. Không dùng mốc vốn 500k cố định. Không tự ý loại mã.
- **Kế hoạch đặt lệnh cụ thể (bắt buộc bám sát dữ liệu giá & số dư tiền mặt khả dụng bên dưới):**
  + **Nên đặt giá bao nhiêu (`suggested_buy_price`):** Đề xuất vùng giá mua LO hợp lý (ưu tiên canh nhịp chỉnh gần MA20 / dưới giá đóng cửa nếu phù hợp), kèm mốc cắt lỗ (SL) và chốt lời (TP) tham khảo bằng VNĐ.
{qty_instruction}

=== 1. TÓM TẮT BCTC, GIÁ, KỸ THUẬT & TÀI KHOẢN HIỆN TẠI ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

Trả về JSON đúng 8 trường bằng tiếng Việt súc tích, nêu rõ số liệu cụ thể:
1. ai_bull_thesis
2. profit_quality
3. negative_news
4. dividend_trap
5. biggest_risk
6. ai_referee_verdict
7. suggested_buy_price
8. suggested_buy_quantity
"""


class GeminiAnalyst:
    """Thin wrapper over google-genai for structured 3-AI debate JSON critique."""

    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        primary_model: str = GEMINI_MODEL,
        fallback_model: str = GEMINI_FALLBACK_MODEL,
        sleep_seconds: float = GEMINI_SLEEP_SECONDS,
        max_retries_per_model: int = 2,
        retry_backoff_seconds: float = 4.0,
    ) -> None:
        self.api_key = api_key
        self.models = [primary_model]
        if fallback_model and fallback_model != primary_model:
            self.models.append(fallback_model)
        self.sleep_seconds = sleep_seconds
        self.max_retries_per_model = max(0, max_retries_per_model)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
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
            attempts = self.max_retries_per_model + 1
            for attempt in range(1, attempts + 1):
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
                        "suggested_buy_price": str(parsed.get("suggested_buy_price", "Không có dữ liệu.")),
                        "suggested_buy_quantity": str(parsed.get("suggested_buy_quantity", "Không có dữ liệu.")),
                    }
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    logger.warning(
                        "Gemini model=%s failed for %s (attempt %d/%d): %s",
                        model_name,
                        ticker,
                        attempt,
                        attempts,
                        exc,
                    )
                    if attempt < attempts and _is_transient_error(exc):
                        wait_s = self.retry_backoff_seconds * attempt
                        if wait_s > 0:
                            logger.info(
                                "Retrying Gemini model=%s in %.1fs...",
                                model_name,
                                wait_s,
                            )
                            time.sleep(wait_s)
                        continue
                    break

        return _fallback_critique(last_err[:120] or "lỗi kết nối")
