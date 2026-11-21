"""Centralized configuration and trading constants for VN Stock Swing Bot."""

from __future__ import annotations

import os
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")


def vn_now() -> datetime:
    """Return current datetime in Asia/Ho_Chi_Minh (UTC+7)."""
    return datetime.now(VN_TZ)


def vn_today_str() -> str:
    """Return today's date string (YYYY-MM-DD) in UTC+7."""
    return vn_now().strftime("%Y-%m-%d")


# Data sources
VNSTOCK_PRIMARY_SOURCE = os.getenv("VNSTOCK_PRIMARY_SOURCE", "VCI").upper()
VNSTOCK_FALLBACK_SOURCE = os.getenv("VNSTOCK_FALLBACK_SOURCE", "TCBS").upper()

# Gemini AI config (AI 1 — Phe Bò & Fallback)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_SLEEP_SECONDS = float(os.getenv("GEMINI_SLEEP_SECONDS", "5.0"))

# Ckey AI config (AI 2 — Phe Gấu / DeepSeek Free, OpenAI-compatible)
CKEY_API_KEY = os.getenv("CKEY_API_KEY", "").strip()
CKEY_BASE_URL = os.getenv("CKEY_BASE_URL", "https://api.xah.io/v1/chat/completions").strip()
CKEY_MODEL = os.getenv("CKEY_MODEL", "deepseek-v4.1-flash-1017-free").strip()
CKEY_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("CKEY_FALLBACK_MODELS", "deepseek-v4.1-flash,qwen3.8-omni-flash").split(",")
    if m.strip()
]
CKEY_TIMEOUT_SECONDS = float(os.getenv("CKEY_TIMEOUT_SECONDS", "35.0"))

# OpenRouter AI config (AI 3 — Trọng tài, OpenAI-compatible)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
).strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free"
).strip()
OPENROUTER_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("OPENROUTER_FALLBACK_MODELS", "openrouter/free").split(",")
    if m.strip()
]
OPENROUTER_TIMEOUT_SECONDS = float(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "45.0"))

# Execution flags & Reliability (Phase 1)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() in ("1", "true", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
VN_EXTRA_HOLIDAYS = os.getenv("VN_EXTRA_HOLIDAYS", "").strip()
DATA_ERROR_ALERT_THRESHOLD = float(os.getenv("DATA_ERROR_ALERT_THRESHOLD", "0.25"))

# Tier 1 — Fundamental Screening Constants
ALLOWED_EXCHANGES = {"HOSE", "HNX"}
MIN_AVG_VALUE_20D_VND = 15_000_000_000      # 15 tỷ VNĐ
FIT_500K_MAX_PRICE_VND = 50_000             # < 50,000 VNĐ
FIT_500K_MIN_AVG_VALUE_VND = 50_000_000_000 # >= 50 tỷ VNĐ
MIN_FUNDAMENTAL_SCORE = 65.0

# Financial sector detection keywords (Vietnamese & English ICB names)
FINANCIAL_SECTOR_KEYWORDS = (
    "ngân hàng",
    "bank",
    "chứng khoán",
    "securities",
    "dịch vụ tài chính",
    "financial services",
    "tài chính",
)

# Tier 2 — Technical Screening Constants
MAX_BUY_CANDIDATES = int(os.getenv("MAX_BUY_CANDIDATES", "3"))
PULLBACK_MA20_LOW_RATIO = 0.98
PULLBACK_MA20_HIGH_RATIO = 1.03
PULLBACK_RSI_MIN = 45.0
PULLBACK_RSI_MAX = 62.0

BREAKOUT_BASE_MIN_DAYS = 10
BREAKOUT_BASE_MAX_DAYS = 15
BREAKOUT_BASE_MAX_RANGE = 0.08  # 8%
BREAKOUT_VOL_MULTIPLIER = 1.5
BREAKOUT_RSI_MIN = 55.0
BREAKOUT_RSI_MAX = 68.0

# Tier 3 — Portfolio, ATR Risk & Intraday Constants (Phase 1 & 2)
TAKE_PROFIT_HALF_PCT = 15.0         # +15%
TRAILING_STOP_DROP_PCT = 8.0        # -8% from highest_price after sold_half
ATR_PERIOD = 14
ATR_INITIAL_STOP_MULT = 2.0         # Initial stop = entry - 2.0 * ATR14
ATR_TRAILING_STOP_MULT = 2.5        # Chandelier trailing stop = high - 2.5 * ATR14
DEFAULT_RISK_PER_TRADE_PCT = 1.5    # Risk 1.5% of NAV per trade
MAX_SINGLE_POSITION_NAV_PCT = 35.0  # Max 35% of NAV in a single ticker
MAX_SECTOR_EXPOSURE_NAV_PCT = 50.0  # Max 50% of NAV in a single sector_group

INTRADAY_DROP_THRESHOLD_PCT = -3.5  # <= -3.5%
INTRADAY_VOL_SPIKE_RATIO = 1.3      # Abnormal selling volume ratio
INTRADAY_MA50_BREACH_PCT = -2.0     # > 2% below MA50
INTRADAY_CEILING_SURGE_PCT = 6.5    # >= +6.5%
INTRADAY_FLOOR_HOSE_PCT = -6.8      # Near/at HOSE floor (-7%)
INTRADAY_FLOOR_HNX_PCT = -9.8       # Near/at HNX floor (-10%)

# Price-tick sanity bands vs reference price (includes tick-rounding tolerance)
MAX_PRICE_BAND_HOSE_PCT = 7.5
MAX_PRICE_BAND_HNX_PCT = 10.5
MAX_PRICE_BAND_UPCOM_PCT = 15.5

# Firestore Collections & System Docs
COL_FUNDAMENTALS = "fundamentals"
COL_PORTFOLIO = "portfolio"
COL_DAILY_REPORTS = "daily_reports"
COL_SETTINGS = "settings"
COL_INTRADAY_STATE = "intraday_alerts_state"
DOC_FCM_TOKENS = "fcm_tokens"
DOC_ACCOUNT = "account"
DOC_SYSTEM_HEALTH = "system_health"
