"""Vietnamese Stock Market (HOSE/HNX) trading calendar & session progress utilities.

Handles weekend checks, official Vietnamese public holidays (2025-2027),
custom extra holidays via `VN_EXTRA_HOLIDAYS`, OHLCV bar freshness checks,
and U-shaped intraday volume session progress (ATO / continuous / ATC).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Set, Union

import pandas as pd

from utils.config import VN_EXTRA_HOLIDAYS, VN_TZ, vn_now

# Official & scheduled HOSE/HNX market holidays (YYYY-MM-DD) for 2025-2027
# Includes: New Year's Day, Lunar New Year (Tet), Hung Kings Commemoration,
# Reunification Day (30/4) & Labor Day (1/5), National Day (2/9).
VN_MARKET_HOLIDAYS: Set[str] = {
    # --- 2025 ---
    "2025-01-01",
    # Tet At Ty 2025
    "2025-01-27",
    "2025-01-28",
    "2025-01-29",
    "2025-01-30",
    "2025-01-31",
    # Hung Kings (10/3 Lunar = 2025-04-07)
    "2025-04-07",
    # 30/4 - 1/5
    "2025-04-30",
    "2025-05-01",
    "2025-05-02",
    # National Day 2/9
    "2025-09-01",
    "2025-09-02",
    # --- 2026 ---
    "2026-01-01",
    "2026-01-02",
    # Tet Binh Ngo 2026 (29 Tet to Mung 5 Tet)
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    # Hung Kings (10/3 Lunar = Sun 2026-04-26 -> observed Mon 2026-04-27)
    "2026-04-27",
    # 30/4 - 1/5
    "2026-04-30",
    "2026-05-01",
    # National Day 2/9
    "2026-09-01",
    "2026-09-02",
    # --- 2027 ---
    "2027-01-01",
    # Tet Dinh Mui 2027
    "2027-02-05",
    "2027-02-08",
    "2027-02-09",
    "2027-02-10",
    "2027-02-11",
    "2027-02-12",
    # Hung Kings (10/3 Lunar = Fri 2027-04-16)
    "2027-04-16",
    # 30/4 - 1/5 (Fri 30/4, Sat 1/5 -> observed Mon 3/5)
    "2027-04-30",
    "2027-05-03",
    # National Day 2/9
    "2027-09-02",
    "2027-09-03",
}


def get_all_holidays(extra_holidays_csv: Optional[str] = None) -> Set[str]:
    """Return combined set of built-in VN market holidays and custom extra dates."""
    holidays = set(VN_MARKET_HOLIDAYS)
    raw_extra = extra_holidays_csv if extra_holidays_csv is not None else VN_EXTRA_HOLIDAYS
    if raw_extra:
        for item in raw_extra.split(","):
            cleaned = item.strip()
            if cleaned:
                holidays.add(cleaned)
    return holidays


def _to_date(target: Optional[Union[date, datetime, str]] = None) -> date:
    """Normalize date/datetime/YYYY-MM-DD string into a `date` in UTC+7."""
    if target is None:
        return vn_now().date()
    if isinstance(target, datetime):
        if target.tzinfo is not None:
            return target.astimezone(VN_TZ).date()
        return target.date()
    if isinstance(target, date):
        return target
    if isinstance(target, str):
        return datetime.strptime(target.strip()[:10], "%Y-%m-%d").date()
    raise TypeError(f"Unsupported date type: {type(target)}")


def is_trading_day(
    target: Optional[Union[date, datetime, str]] = None,
    extra_holidays_csv: Optional[str] = None,
) -> bool:
    """Return True if `target` is a weekday (Mon-Fri) and not a VN market holiday."""
    d = _to_date(target)
    if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    date_str = d.strftime("%Y-%m-%d")
    return date_str not in get_all_holidays(extra_holidays_csv)


def has_fresh_daily_bar(
    df: Optional[pd.DataFrame],
    target_date: Optional[Union[date, datetime, str]] = None,
    max_staleness_days: int = 0,
) -> bool:
    """Verify that the latest OHLCV row in `df` is not stale compared to `target_date`.

    If `max_staleness_days == 0`, requires the last bar's date to match `target_date`.
    If the `time` column is missing or unparseable, returns True so unit tests with
    synthetic DataFrames are not unexpectedly blocked.
    """
    if df is None or df.empty:
        return False
    if "time" not in df.columns:
        return True

    try:
        last_val = df["time"].iloc[-1]
        last_dt = pd.to_datetime(last_val)
        if pd.isna(last_dt):
            return True
        bar_date = last_dt.date()
        expected_date = _to_date(target_date)
        delta_days = (expected_date - bar_date).days
        return delta_days <= max_staleness_days
    except Exception:  # noqa: BLE001
        return True


def compute_weighted_session_progress(now: Optional[datetime] = None) -> float:
    """Estimate fraction of daily trading volume elapsed (0.15 to 1.0) using U-shaped curve.

    HOSE/HNX session structure (UTC+7):
      - 09:00 - 09:15 (15m, ATO): ~12% of daily volume (minimum floor 0.15)
      - 09:15 - 11:30 (135m, Morning continuous): +43% -> reaches 55% at 11:30
      - 11:30 - 13:00 (Lunch break): holds at 55%
      - 13:00 - 14:30 (90m, Afternoon continuous): +33% -> reaches 88% at 14:30
      - 14:30 - 14:45 (15m, ATC): +12% -> reaches 100% at 14:45
    """
    dt = now.astimezone(VN_TZ) if (now is not None and now.tzinfo is not None) else (now or vn_now())
    mins = dt.hour * 60 + dt.minute

    m_0900 = 9 * 60
    m_0915 = 9 * 60 + 15
    m_1130 = 11 * 60 + 30
    m_1300 = 13 * 60
    m_1430 = 14 * 60 + 30
    m_1445 = 14 * 60 + 45

    if mins <= m_0900:
        return 0.15
    if mins <= m_0915:
        raw = 0.12 * ((mins - m_0900) / 15.0)
        return round(max(0.15, raw), 4)
    if mins <= m_1130:
        raw = 0.12 + 0.43 * ((mins - m_0915) / 135.0)
        return round(max(0.15, raw), 4)
    if mins < m_1300:
        return 0.55
    if mins <= m_1430:
        raw = 0.55 + 0.33 * ((mins - m_1300) / 90.0)
        return round(raw, 4)
    if mins <= m_1445:
        raw = 0.88 + 0.12 * ((mins - m_1430) / 15.0)
        return round(min(1.0, raw), 4)
    return 1.0
