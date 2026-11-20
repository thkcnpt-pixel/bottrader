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

# Gemini AI config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_SLEEP_SECONDS = float(os.getenv("GEMINI_SLEEP_SECONDS", "5.0"))

# Execution flags
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

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

# Tier 3 — Portfolio & Intraday Risk Constants
TAKE_PROFIT_HALF_PCT = 15.0         # +15%
TRAILING_STOP_DROP_PCT = 8.0        # -8% from highest_price after sold_half
INTRADAY_DROP_THRESHOLD_PCT = -3.5  # <= -3.5%
INTRADAY_VOL_SPIKE_RATIO = 1.3      # Abnormal selling volume ratio
INTRADAY_MA50_BREACH_PCT = -2.0     # > 2% below MA50
INTRADAY_CEILING_SURGE_PCT = 6.5    # >= +6.5%
INTRADAY_FLOOR_HOSE_PCT = -6.8      # Near/at HOSE floor (-7%)
INTRADAY_FLOOR_HNX_PCT = -9.8       # Near/at HNX floor (-10%)

# Firestore Collections
COL_FUNDAMENTALS = "fundamentals"
COL_PORTFOLIO = "portfolio"
COL_DAILY_REPORTS = "daily_reports"
COL_SETTINGS = "settings"
COL_INTRADAY_STATE = "intraday_alerts_state"
DOC_FCM_TOKENS = "fcm_tokens"
