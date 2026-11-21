"""Multi-AI 3-Perspective Debate & Risk Critic (Tier 4).

Design rules:
  - NEVER vetoes or removes a stock candidate.
  - Uses live account balance (`account_context`) for position sizing advice.
  - True Multi-Provider Debate when >= 2 providers are configured:
      * AI 1 (Phe Bò — Cơ bản & Động lực): Gemini (fallback OpenRouter -> Ckey)
      * AI 2 (Phe Gấu — Phản biện rủi ro): Ckey DeepSeek Free (fallback OpenRouter -> Gemini)
      * AI 3 (Trọng tài — Chốt kèo & Chấm điểm 0-100): OpenRouter Nemotron Ultra (fallback Gemini -> Ckey)
  - Backward-compatible single-call mode when only 1 provider is configured.
  - Automatic retry on transient 503/429/timeout and cross-provider fallback.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

import requests
from pydantic import BaseModel, Field

from utils.config import (
    CKEY_API_KEY,
    CKEY_BASE_URL,
    CKEY_FALLBACK_MODELS,
    CKEY_MODEL,
    CKEY_TIMEOUT_SECONDS,
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    GEMINI_SLEEP_SECONDS,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_FALLBACK_MODELS,
    OPENROUTER_MODEL,
    OPENROUTER_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_TRANSIENT_ERROR_MARKERS = (
    "503",
    "502",
    "504",
    "429",
    "TIMEOUT",
    "TIMED OUT",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "OVERLOADED",
    "TRY AGAIN LATER",
    "RATE LIMIT",
)

_VALID_DECISIONS = {"BUY", "WATCH", "AVOID"}


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(marker in msg for marker in _TRANSIENT_ERROR_MARKERS)


class BullThesisSchema(BaseModel):
    ai_bull_thesis: str = Field(
        description="[AI 1 - Phe Bò] Luận điểm ủng hộ mua mạnh nhất từ BCTC lõi, tăng trưởng & kỹ thuật (2-3 câu ngắn gọn bằng tiếng Việt)."
    )


class BearCritiqueSchema(BaseModel):
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


class RefereeVerdictSchema(BaseModel):
    decision: str = Field(
        description="Quyết định ngắn gọn chỉ chọn 1 trong 3 giá trị: BUY, WATCH, hoặc AVOID."
    )
    confidence_score: int = Field(
        description="Điểm tin cậy tổng hợp từ 0 đến 100 (số nguyên)."
    )
    ai_referee_verdict: str = Field(
        description="[AI 3 - Trọng tài] Tổng kết phản biện giữa AI 1 và AI 2: chốt phương án hành động tốt nhất dựa trực tiếp trên số dư tiền mặt khả dụng và danh mục hiện có của tài khoản (2-3 câu thực chiến bằng tiếng Việt)."
    )
    suggested_buy_price: str = Field(
        description="[Nên đặt giá bao nhiêu] Vùng giá / mức giá đặt lệnh mua LO cụ thể (VNĐ) dựa trên giá đóng cửa, MA20, MA50, ATR và hỗ trợ/kháng cự, kèm mức cắt lỗ (SL) và chốt lời (TP) tham khảo (1-2 câu súc tích bằng tiếng Việt)."
    )
    suggested_buy_quantity: str = Field(
        description="[Nên mua bao nhiêu] Khối lượng cổ phiếu (CP) và số tiền giải ngân gợi ý tính trực tiếp trên Số dư tiền mặt khả dụng hiện có của tài khoản (`available_cash_vnd`), nên mua 1 lần hay chia đợt, hoặc báo rõ nếu tiền mặt hiện có không đủ mua 1 CP (1-2 câu súc tích bằng tiếng Việt)."
    )


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
    decision: str = Field(
        default="WATCH",
        description="Quyết định ngắn gọn chỉ chọn 1 trong 3 giá trị: BUY, WATCH, hoặc AVOID.",
    )
    confidence_score: int = Field(
        default=50,
        description="Điểm tin cậy tổng hợp từ 0 đến 100 (số nguyên).",
    )


def _fallback_critique(reason: str) -> Dict[str, Any]:
    msg = f"Chưa thể gọi AI API ({reason}). Vui lòng kiểm tra tin tức & lịch cổ tức thủ công trước khi đặt lệnh."
    return {
        "ai_bull_thesis": msg,
        "profit_quality": msg,
        "negative_news": msg,
        "dividend_trap": msg,
        "biggest_risk": msg,
        "ai_referee_verdict": msg,
        "suggested_buy_price": msg,
        "suggested_buy_quantity": msg,
        "decision": "WATCH",
        "confidence_score": 0,
        "ai_models_used": {},
    }


def _extract_json_dict(raw_text: str) -> Dict[str, Any]:
    """Parse JSON from LLM text, stripping <think> blocks and markdown code fences."""
    text = (raw_text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"Could not parse JSON object from response: {text[:160]}")


def _build_account_prompt_blocks(
    ticker: str,
    fundamental_summary: Dict[str, Any],
) -> Tuple[str, str]:
    acc_ctx = (fundamental_summary or {}).get("account_context") or {}
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
    return account_prompt_block, qty_instruction


def _format_context_blocks(
    fundamental_summary: Dict[str, Any],
    news_bundle: Dict[str, Any],
) -> Tuple[str, str, str]:
    articles: List[Dict[str, str]] = (news_bundle or {}).get("articles", [])
    events: List[Dict[str, Any]] = (news_bundle or {}).get("events", [])

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

    bctc_json = json.dumps(fundamental_summary or {}, ensure_ascii=False, indent=2)
    return bctc_json, events_text, news_text


def _build_prompt(
    ticker: str,
    fundamental_summary: Dict[str, Any],
    news_bundle: Dict[str, Any],
) -> str:
    bctc_json, events_text, news_text = _format_context_blocks(fundamental_summary, news_bundle)
    account_prompt_block, qty_instruction = _build_account_prompt_blocks(ticker, fundamental_summary)

    return f"""Hãy mô phỏng **Hội đồng 3 chuyên gia AI phản biện chéo nhau** về mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, phong cách Swing 1-3 tháng, hỗ trợ cả giao dịch lô lẻ và lô chẵn tùy theo số dư tài khoản thực tế của nhà đầu tư):
{account_prompt_block}
- **AI 1 (Phe Bò — Cơ bản & Tăng trưởng):** Nêu luận điểm thuyết phục nhất để mua mã {ticker}.
- **AI 2 (Phe Gấu — Phản biện gay gắt):** Đập lại AI 1 bằng cách soi kỹ 4 rủi ro cốt lõi (chất lượng lợi nhuận, tin xấu/lãnh đạo bán, bẫy cổ tức/phát hành thêm, rủi ro lớn nhất 1-3 tháng).
- **AI 3 (Trọng tài — Chốt phương án tốt nhất theo tài khoản hiện có):** Cân bằng lập luận của cả AI 1 và AI 2, kết hợp với **số dư tiền mặt khả dụng, danh mục đang nắm giữ, `vnindex_regime`, `atr14_vnd` và hỗ trợ/kháng cự** để rút ra kết luận thực chiến nhất. Không dùng mốc vốn cố định. Không tự ý loại mã.
- **Kế hoạch đặt lệnh cụ thể (bắt buộc bám sát dữ liệu giá & số dư tiền mặt khả dụng bên dưới):**
  + **Nên đặt giá bao nhiêu (`suggested_buy_price`):** Đề xuất vùng giá mua LO hợp lý (ưu tiên canh nhịp chỉnh gần MA20 / dưới giá đóng cửa nếu phù hợp), kèm mốc cắt lỗ (SL) và chốt lời (TP) tham khảo bằng VNĐ.
{qty_instruction}

=== 1. TÓM TẮT BCTC, GIÁ, KỸ THUẬT, VN-INDEX & TÀI KHOẢN HIỆN TẠI ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

CHỈ trả về 1 JSON object hợp lệ (không kèm giải thích ngoài JSON) đúng 10 trường bằng tiếng Việt súc tích, nêu rõ số liệu cụ thể:
1. ai_bull_thesis (string)
2. profit_quality (string)
3. negative_news (string)
4. dividend_trap (string)
5. biggest_risk (string)
6. ai_referee_verdict (string)
7. suggested_buy_price (string)
8. suggested_buy_quantity (string)
9. decision (string: chỉ chọn "BUY", "WATCH", hoặc "AVOID")
10. confidence_score (integer: 0-100)
"""


def _build_bull_prompt(
    ticker: str,
    fundamental_summary: Dict[str, Any],
    news_bundle: Dict[str, Any],
) -> str:
    bctc_json, events_text, news_text = _format_context_blocks(fundamental_summary, news_bundle)
    return f"""Bạn là **AI 1 — Chuyên gia Phe Bò (Cơ bản & Tăng trưởng)** phân tích mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, Swing 1-3 tháng).
Hãy tìm luận điểm thuyết phục nhất ủng hộ mua mã {ticker} dựa trên BCTC lõi, sức mạnh kỹ thuật, thanh khoản và tin tức tích cực (nêu rõ số liệu cụ thể).

=== 1. TÓM TẮT BCTC, GIÁ & KỸ THUẬT ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

CHỈ trả về 1 JSON object hợp lệ đúng 1 trường:
{{"ai_bull_thesis": "2-3 câu ngắn gọn bằng tiếng Việt nêu rõ số liệu"}}
"""


def _build_bear_prompt(
    ticker: str,
    fundamental_summary: Dict[str, Any],
    news_bundle: Dict[str, Any],
) -> str:
    bctc_json, events_text, news_text = _format_context_blocks(fundamental_summary, news_bundle)
    return f"""Bạn là **AI 2 — Chuyên gia Phe Gấu (Phản biện rủi ro gay gắt)** phân tích độc lập mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, Swing 1-3 tháng).
Nhiệm vụ của bạn là soi thật kỹ các điểm yếu, cạm bẫy và rủi ro khiến kèo mua {ticker} có thể thất bại (nêu rõ số liệu, chú ý `vnindex_regime`, kháng cự 20-60 phiên, BCTC và cổ tức).

=== 1. TÓM TẮT BCTC, GIÁ & KỸ THUẬT ===
{bctc_json}

=== 2. LỊCH SỰ KIỆN & CỔ TỨC ===
{events_text}

=== 3. TIN TỨC MỚI NHẤT ===
{news_text}

CHỈ trả về 1 JSON object hợp lệ đúng 4 trường (mỗi trường 2-3 câu tiếng Việt súc tích):
{{
  "profit_quality": "Lợi nhuận đến từ kinh doanh lõi hay đột biến 1 lần?",
  "negative_news": "Có tin xấu, kiện tụng, thanh tra hay lãnh đạo bán ra không?",
  "dividend_trap": "Có sắp chia cổ tức cổ phiếu/phát hành thêm gây chôn vốn hoặc kẹt lô lẻ không?",
  "biggest_risk": "Rủi ro lớn nhất nếu mua mã này trong 1-3 tháng tới là gì?"
}}
"""


def _build_referee_prompt(
    ticker: str,
    fundamental_summary: Dict[str, Any],
    bull_thesis: str,
    bear_critique: Dict[str, str],
) -> str:
    account_prompt_block, qty_instruction = _build_account_prompt_blocks(ticker, fundamental_summary)
    bctc_json = json.dumps(fundamental_summary or {}, ensure_ascii=False, indent=2)
    bear_json = json.dumps(bear_critique, ensure_ascii=False, indent=2)
    return f"""Bạn là **AI 3 — Trọng tài Độc lập** chốt phương án giao dịch tối ưu cho mã cổ phiếu **{ticker}** (chứng khoán Việt Nam, Swing 1-3 tháng).
{account_prompt_block}
Hãy cân bằng lập luận giữa **AI 1 (Phe Bò)** và **AI 2 (Phe Gấu)** cùng dữ liệu định lượng bên dưới (lưu ý `vnindex_regime`, `atr14_vnd`, hỗ trợ/kháng cự 20-60 phiên, và `quant_risk_plan` trong `account_context`). Không tự ý loại mã, chỉ đánh giá khách quan.
{qty_instruction}

=== 1. DỮ LIỆU ĐỊNH LƯỢNG & TÀI KHOẢN ===
{bctc_json}

=== 2. LUẬN ĐIỂM AI 1 (PHE BÒ) ===
{bull_thesis}

=== 3. PHẢN BIỆN AI 2 (PHE GẤU) ===
{bear_json}

CHỈ trả về 1 JSON object hợp lệ đúng 5 trường:
{{
  "decision": "BUY hoặc WATCH hoặc AVOID",
  "confidence_score": 0-100,
  "ai_referee_verdict": "2-3 câu thực chiến bằng tiếng Việt tổng kết 2 phe và điều kiện giải ngân/đứng ngoài theo tài khoản hiện có",
  "suggested_buy_price": "1-2 câu nêu vùng giá mua LO, SL, TP cụ thể bằng VNĐ",
  "suggested_buy_quantity": "1-2 câu nêu số CP và số tiền giải ngân theo available_cash_vnd hiện có"
}}
"""


def _normalize_decision(raw: Any) -> str:
    val = str(raw or "WATCH").strip().upper()
    if val in _VALID_DECISIONS:
        return val
    if "BUY" in val or "MUA" in val:
        return "BUY"
    if "AVOID" in val or "TRÁNH" in val or "BỎ" in val:
        return "AVOID"
    return "WATCH"


def _normalize_confidence(raw: Any, default: int = 50) -> int:
    try:
        score = int(round(float(raw)))
        return max(0, min(100, score))
    except (TypeError, ValueError):
        return default


def _apply_python_guardrails(
    result: Dict[str, Any],
    fundamental_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach deterministic quantitative guardrails without vetoing candidates."""
    if not isinstance(fundamental_summary, dict):
        return result
    vnindex = fundamental_summary.get("vnindex_regime") or {}
    quant_plan = (fundamental_summary.get("account_context") or {}).get("quant_risk_plan") or {}
    rr = fundamental_summary.get("reference_rr_ratio") or quant_plan.get("rr_ratio")
    notes: List[str] = []

    if vnindex.get("warning_flag") and result.get("decision") == "BUY":
        notes.append("VN-Index đang dưới MA50 (rủi ro thị trường chung cao — ưu tiên chia nhỏ vị thế)")
    if isinstance(rr, (int, float)) and 0 < rr < 1.5:
        notes.append(f"Tỷ lệ R:R tham chiếu thấp ({rr:.2f}x < 1.5x)")

    if notes:
        verdict = str(result.get("ai_referee_verdict", "")).strip()
        guard_text = " [Lưu ý định lượng: " + "; ".join(notes) + ".]"
        if guard_text not in verdict:
            result["ai_referee_verdict"] = (verdict + guard_text).strip()
    return result


class GeminiAnalyst:
    """Multi-AI orchestrator (Gemini + Ckey + OpenRouter) with cross-provider fallback."""

    def __init__(
        self,
        api_key: str = GEMINI_API_KEY,
        primary_model: str = GEMINI_MODEL,
        fallback_model: str = GEMINI_FALLBACK_MODEL,
        sleep_seconds: float = GEMINI_SLEEP_SECONDS,
        max_retries_per_model: int = 2,
        retry_backoff_seconds: float = 4.0,
        ckey_api_key: Optional[str] = None,
        ckey_base_url: str = CKEY_BASE_URL,
        ckey_model: str = CKEY_MODEL,
        ckey_fallback_models: Optional[Sequence[str]] = None,
        ckey_timeout_seconds: float = CKEY_TIMEOUT_SECONDS,
        openrouter_api_key: Optional[str] = None,
        openrouter_base_url: str = OPENROUTER_BASE_URL,
        openrouter_model: str = OPENROUTER_MODEL,
        openrouter_fallback_models: Optional[Sequence[str]] = None,
        openrouter_timeout_seconds: float = OPENROUTER_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.models = [primary_model]
        if fallback_model and fallback_model != primary_model:
            self.models.append(fallback_model)
        self.sleep_seconds = sleep_seconds
        self.max_retries_per_model = max(0, max_retries_per_model)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self._call_count = 0

        self.ckey_api_key = (CKEY_API_KEY if ckey_api_key is None else ckey_api_key).strip()
        self.ckey_base_url = ckey_base_url
        self.ckey_models = self._dedupe_models(
            ckey_model,
            CKEY_FALLBACK_MODELS if ckey_fallback_models is None else ckey_fallback_models,
        )
        self.ckey_timeout_seconds = ckey_timeout_seconds

        self.openrouter_api_key = (
            OPENROUTER_API_KEY if openrouter_api_key is None else openrouter_api_key
        ).strip()
        self.openrouter_base_url = openrouter_base_url
        self.openrouter_models = self._dedupe_models(
            openrouter_model,
            OPENROUTER_FALLBACK_MODELS
            if openrouter_fallback_models is None
            else openrouter_fallback_models,
        )
        self.openrouter_timeout_seconds = openrouter_timeout_seconds

    @staticmethod
    def _dedupe_models(primary: str, fallbacks: Sequence[str]) -> List[str]:
        out: List[str] = []
        for m in [primary, *fallbacks]:
            m_clean = (m or "").strip()
            if m_clean and m_clean not in out:
                out.append(m_clean)
        return out

    def _configured_providers(self) -> List[str]:
        providers: List[str] = []
        if self.api_key:
            providers.append("gemini")
        if self.ckey_api_key:
            providers.append("ckey")
        if self.openrouter_api_key:
            providers.append("openrouter")
        return providers

    def _call_gemini_json(
        self,
        prompt: str,
        schema: Type[BaseModel],
        ticker: str,
        role: str,
    ) -> Tuple[Dict[str, Any], str]:
        if not self.api_key:
            raise RuntimeError("thiếu GEMINI_API_KEY")

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        last_err: Optional[Exception] = None

        for model_name in self.models:
            attempts = self.max_retries_per_model + 1
            for attempt in range(1, attempts + 1):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=schema,
                            temperature=0.25,
                        ),
                    )
                    parsed = _extract_json_dict(response.text or "")
                    return parsed, f"gemini:{model_name}"
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    logger.warning(
                        "Gemini [%s] model=%s failed for %s (attempt %d/%d): %s",
                        role,
                        model_name,
                        ticker,
                        attempt,
                        attempts,
                        exc,
                    )
                    if attempt < attempts and _is_transient_error(exc):
                        wait_s = self.retry_backoff_seconds * attempt
                        if wait_s > 0:
                            logger.info("Retrying Gemini model=%s in %.1fs...", model_name, wait_s)
                            time.sleep(wait_s)
                        continue
                    break

        raise RuntimeError(str(last_err) if last_err else "Gemini connection error")

    def _call_openai_compat_json(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        models: Sequence[str],
        timeout_seconds: float,
        prompt: str,
        ticker: str,
        role: str,
    ) -> Tuple[Dict[str, Any], str]:
        if not api_key:
            raise RuntimeError(f"thiếu {provider.upper()}_API_KEY")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/thkcnpt-pixel/bottrader"
            headers["X-Title"] = "VN Stock Swing Bot"

        last_err: Optional[Exception] = None
        for model_name in models:
            attempts = min(self.max_retries_per_model, 1) + 1
            for attempt in range(1, attempts + 1):
                try:
                    payload: Dict[str, Any] = {
                        "model": model_name,
                        "messages": [
                            {
                                "role": "system",
                                "content": "Bạn là chuyên gia phân tích chứng khoán Việt Nam. Luôn trả lời bằng 1 JSON object hợp lệ, đúng các trường được yêu cầu, tiếng Việt súc tích.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.25,
                    }
                    if provider == "ckey":
                        payload["response_format"] = {"type": "json_object"}

                    resp = requests.post(
                        base_url,
                        headers=headers,
                        json=payload,
                        timeout=timeout_seconds,
                    )
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise RuntimeError("Empty choices from OpenAI-compatible API")
                    content = (choices[0].get("message") or {}).get("content") or ""
                    parsed = _extract_json_dict(content)
                    return parsed, f"{provider}:{model_name}"
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    logger.warning(
                        "%s [%s] model=%s failed for %s (attempt %d/%d): %s",
                        provider.upper(),
                        role,
                        model_name,
                        ticker,
                        attempt,
                        attempts,
                        exc,
                    )
                    if attempt < attempts and _is_transient_error(exc):
                        wait_s = self.retry_backoff_seconds * attempt
                        if wait_s > 0:
                            time.sleep(wait_s)
                        continue
                    break

        raise RuntimeError(str(last_err) if last_err else f"{provider} connection error")

    def _invoke_with_fallback(
        self,
        provider_order: Sequence[str],
        prompt: str,
        schema: Type[BaseModel],
        ticker: str,
        role: str,
    ) -> Tuple[Dict[str, Any], str]:
        errors: List[str] = []
        for provider in provider_order:
            try:
                if provider == "gemini" and self.api_key:
                    return self._call_gemini_json(prompt, schema, ticker, role)
                if provider == "ckey" and self.ckey_api_key:
                    return self._call_openai_compat_json(
                        provider="ckey",
                        base_url=self.ckey_base_url,
                        api_key=self.ckey_api_key,
                        models=self.ckey_models,
                        timeout_seconds=self.ckey_timeout_seconds,
                        prompt=prompt,
                        ticker=ticker,
                        role=role,
                    )
                if provider == "openrouter" and self.openrouter_api_key:
                    return self._call_openai_compat_json(
                        provider="openrouter",
                        base_url=self.openrouter_base_url,
                        api_key=self.openrouter_api_key,
                        models=self.openrouter_models,
                        timeout_seconds=self.openrouter_timeout_seconds,
                        prompt=prompt,
                        ticker=ticker,
                        role=role,
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider}: {exc}")

        raise RuntimeError(" | ".join(errors) or "không có AI provider khả dụng")

    def _run_single_provider(
        self,
        provider: str,
        ticker: str,
        fundamental_summary: Dict[str, Any],
        news_bundle: Dict[str, Any],
    ) -> Dict[str, Any]:
        prompt = _build_prompt(ticker, fundamental_summary, news_bundle)
        try:
            parsed, model_used = self._invoke_with_fallback(
                [provider], prompt, RiskCritiqueSchema, ticker, "single_debate"
            )
            result: Dict[str, Any] = {
                "ai_bull_thesis": str(parsed.get("ai_bull_thesis", "Không có dữ liệu.")),
                "profit_quality": str(parsed.get("profit_quality", "Không có dữ liệu.")),
                "negative_news": str(parsed.get("negative_news", "Không có dữ liệu.")),
                "dividend_trap": str(parsed.get("dividend_trap", "Không có dữ liệu.")),
                "biggest_risk": str(parsed.get("biggest_risk", "Không có dữ liệu.")),
                "ai_referee_verdict": str(parsed.get("ai_referee_verdict", "Không có dữ liệu.")),
                "suggested_buy_price": str(parsed.get("suggested_buy_price", "Không có dữ liệu.")),
                "suggested_buy_quantity": str(parsed.get("suggested_buy_quantity", "Không có dữ liệu.")),
                "decision": _normalize_decision(parsed.get("decision", "WATCH")),
                "confidence_score": _normalize_confidence(parsed.get("confidence_score", 50)),
                "ai_models_used": {"all": model_used},
            }
            return _apply_python_guardrails(result, fundamental_summary)
        except Exception as exc:  # noqa: BLE001
            return _fallback_critique(str(exc)[:140] or "lỗi kết nối")

    def _run_multi_ai_debate(
        self,
        ticker: str,
        fundamental_summary: Dict[str, Any],
        news_bundle: Dict[str, Any],
    ) -> Dict[str, Any]:
        models_used: Dict[str, str] = {}

        # Stage 1A: AI 1 (Phe Bò) — Gemini primary -> OpenRouter -> Ckey
        bull_prompt = _build_bull_prompt(ticker, fundamental_summary, news_bundle)
        try:
            bull_parsed, bull_model = self._invoke_with_fallback(
                ("gemini", "openrouter", "ckey"),
                bull_prompt,
                BullThesisSchema,
                ticker,
                "bull",
            )
            ai_bull_thesis = str(bull_parsed.get("ai_bull_thesis", "Không có dữ liệu."))
            models_used["bull"] = bull_model
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 1 (Bull) failed for %s: %s", ticker, exc)
            ai_bull_thesis = f"Không gọi được AI Phe Bò ({str(exc)[:80]})."

        # Stage 1B: AI 2 (Phe Gấu) — Ckey DeepSeek primary -> OpenRouter -> Gemini
        bear_prompt = _build_bear_prompt(ticker, fundamental_summary, news_bundle)
        try:
            bear_parsed, bear_model = self._invoke_with_fallback(
                ("ckey", "openrouter", "gemini"),
                bear_prompt,
                BearCritiqueSchema,
                ticker,
                "bear",
            )
            bear_critique = {
                "profit_quality": str(bear_parsed.get("profit_quality", "Không có dữ liệu.")),
                "negative_news": str(bear_parsed.get("negative_news", "Không có dữ liệu.")),
                "dividend_trap": str(bear_parsed.get("dividend_trap", "Không có dữ liệu.")),
                "biggest_risk": str(bear_parsed.get("biggest_risk", "Không có dữ liệu.")),
            }
            models_used["bear"] = bear_model
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 2 (Bear) failed for %s: %s", ticker, exc)
            err_msg = f"Không gọi được AI Phe Gấu ({str(exc)[:80]})."
            bear_critique = {
                "profit_quality": err_msg,
                "negative_news": err_msg,
                "dividend_trap": err_msg,
                "biggest_risk": err_msg,
            }

        # Stage 2: AI 3 (Trọng tài) — OpenRouter primary -> Gemini -> Ckey
        referee_prompt = _build_referee_prompt(
            ticker, fundamental_summary, ai_bull_thesis, bear_critique
        )
        try:
            ref_parsed, ref_model = self._invoke_with_fallback(
                ("openrouter", "gemini", "ckey"),
                referee_prompt,
                RefereeVerdictSchema,
                ticker,
                "referee",
            )
            models_used["referee"] = ref_model
            result: Dict[str, Any] = {
                "ai_bull_thesis": ai_bull_thesis,
                **bear_critique,
                "ai_referee_verdict": str(ref_parsed.get("ai_referee_verdict", "Không có dữ liệu.")),
                "suggested_buy_price": str(ref_parsed.get("suggested_buy_price", "Không có dữ liệu.")),
                "suggested_buy_quantity": str(
                    ref_parsed.get("suggested_buy_quantity", "Không có dữ liệu.")
                ),
                "decision": _normalize_decision(ref_parsed.get("decision", "WATCH")),
                "confidence_score": _normalize_confidence(ref_parsed.get("confidence_score", 50)),
                "ai_models_used": models_used,
            }
            return _apply_python_guardrails(result, fundamental_summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 3 (Referee) failed for %s: %s", ticker, exc)
            if not models_used:
                return _fallback_critique(str(exc)[:140] or "lỗi kết nối")
            err_msg = f"Không gọi được AI Trọng tài ({str(exc)[:80]})."
            return _apply_python_guardrails(
                {
                    "ai_bull_thesis": ai_bull_thesis,
                    **bear_critique,
                    "ai_referee_verdict": err_msg,
                    "suggested_buy_price": err_msg,
                    "suggested_buy_quantity": err_msg,
                    "decision": "WATCH",
                    "confidence_score": 50,
                    "ai_models_used": models_used,
                },
                fundamental_summary,
            )

    def analyze_risk(
        self,
        ticker: str,
        fundamental_summary: Dict[str, Any],
        news_bundle: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate structured 3-AI debate JSON critique for a ticker."""
        providers = self._configured_providers()
        if not providers:
            logger.warning("No AI API keys configured — returning fallback critique for %s", ticker)
            return _fallback_critique("thiếu GEMINI_API_KEY / CKEY_API_KEY / OPENROUTER_API_KEY")

        if self._call_count > 0 and self.sleep_seconds > 0:
            logger.info("Sleeping %.1fs before next AI analysis...", self.sleep_seconds)
            time.sleep(self.sleep_seconds)
        self._call_count += 1

        if len(providers) == 1:
            return self._run_single_provider(
                providers[0], ticker, fundamental_summary, news_bundle
            )

        return self._run_multi_ai_debate(ticker, fundamental_summary, news_bundle)
