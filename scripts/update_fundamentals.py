#!/usr/bin/env python3
"""Tier 1 — Weekly Full-Market Fundamental Screener & Scorer (HOSE & HNX).

Runs every Sunday 20:00 UTC+7 via GitHub Actions (`update_fundamentals.yml`).
Saves all stocks with fundamental score >= 65/100 to Firestore `fundamentals/{ticker}`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Ensure project root is on sys.path when invoked as `python scripts/update_fundamentals.py`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.config import (  # noqa: E402
    FIT_500K_MAX_PRICE_VND,
    FIT_500K_MIN_AVG_VALUE_VND,
    LOG_LEVEL,
    MIN_AVG_VALUE_20D_VND,
    MIN_FUNDAMENTAL_SCORE,
    vn_now,
)
from utils.firebase_client import FirebaseClient  # noqa: E402
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("update_fundamentals")


def _find_col(df: Optional[pd.DataFrame], keywords: Tuple[str, ...]) -> Optional[str]:
    """Find first column whose lowercase name matches any keyword."""
    if df is None or df.empty:
        return None
    cols_lower = {str(c).lower(): c for c in df.columns}
    for kw in keywords:
        for low, orig in cols_lower.items():
            if kw in low:
                return orig
    return None


def _safe_series(df: Optional[pd.DataFrame], keywords: Tuple[str, ...]) -> List[float]:
    col = _find_col(df, keywords)
    if not col or df is None:
        return []
    vals = pd.to_numeric(df[col], errors="coerce").dropna().tolist()
    return [float(v) for v in vals]


def _extract_latest_quarter_label(df: Optional[pd.DataFrame]) -> Optional[Tuple[int, int]]:
    """Extract (year, quarter) of the most recent row in a financial statement."""
    if df is None or df.empty:
        return None
    year_col = _find_col(df, ("yearreport", "year", "nam"))
    q_col = _find_col(df, ("lengthreport", "quarter", "quy"))
    if year_col and q_col:
        try:
            y = int(float(df.iloc[0][year_col]))
            q = int(float(df.iloc[0][q_col]))
            if 2000 <= y <= 2100 and 1 <= q <= 4:
                return (y, q)
        except (ValueError, TypeError):
            pass
    return None


def _expected_latest_quarter() -> Tuple[int, int]:
    """Return the latest quarter whose BCTC should normally be published by now.

    Vietnam listed companies publish quarterly BCTC ~30-45 days after quarter end.
    """
    now = vn_now()
    y, m = now.year, now.month
    if m <= 2:
        return (y - 1, 3)
    if m <= 5:
        return (y - 1, 4)
    if m <= 8:
        return (y, 1)
    if m <= 11:
        return (y, 2)
    return (y, 3)


def _is_missing_latest_bctc(latest_q: Optional[Tuple[int, int]]) -> bool:
    if latest_q is None:
        return True
    exp_y, exp_q = _expected_latest_quarter()
    return (latest_q[0], latest_q[1]) < (exp_y, exp_q)


def _normalize_pct(val: float) -> float:
    """Convert decimal ratios (e.g., 0.18) to percentage (18.0) when appropriate."""
    if -2.0 <= val <= 2.0:
        return val * 100.0
    return val


def score_non_financial(metrics: Dict[str, Optional[float]]) -> float:
    """Score Non-Financial stock on 0-100 scale.

    - Revenue & Net Profit YoY growth > 10% (25 pts)
    - ROE > 15% (20 pts)
    - Cash flow quality: OCF > 0 and OCF/LNST >= 0.8 (25 pts)
    - D/E < 1.0 (15 pts)
    - P/E positive & reasonable vs history/industry (15 pts)
    """
    score = 0.0

    # 1. Revenue & Net Profit YoY growth (25 pts)
    rev_yoy = metrics.get("rev_yoy_pct")
    profit_yoy = metrics.get("profit_yoy_pct")
    if rev_yoy is not None:
        if rev_yoy >= 15.0:
            score += 12.5
        elif rev_yoy > 10.0:
            score += 10.5
        elif rev_yoy > 5.0:
            score += 6.5
        elif rev_yoy > 0.0:
            score += 3.0
    if profit_yoy is not None:
        if profit_yoy >= 15.0:
            score += 12.5
        elif profit_yoy > 10.0:
            score += 10.5
        elif profit_yoy > 5.0:
            score += 6.5
        elif profit_yoy > 0.0:
            score += 3.0

    # 2. ROE > 15% (20 pts)
    roe = metrics.get("roe_pct")
    if roe is not None:
        if roe >= 20.0:
            score += 20.0
        elif roe > 15.0:
            score += 17.0
        elif roe >= 12.0:
            score += 11.0
        elif roe >= 8.0:
            score += 5.0

    # 3. Cash flow quality: OCF > 0 and OCF/LNST >= 0.8 (25 pts)
    ocf = metrics.get("ocf_ttm")
    ocf_to_np = metrics.get("ocf_to_net_profit")
    if ocf is not None and ocf > 0:
        score += 10.0
        if ocf_to_np is not None:
            if ocf_to_np >= 1.0:
                score += 15.0
            elif ocf_to_np >= 0.8:
                score += 13.0
            elif ocf_to_np >= 0.5:
                score += 7.0
            elif ocf_to_np > 0:
                score += 3.0

    # 4. D/E < 1.0 (15 pts)
    de = metrics.get("de_ratio")
    if de is not None and de >= 0:
        if de < 0.6:
            score += 15.0
        elif de < 1.0:
            score += 13.0
        elif de < 1.5:
            score += 7.0
        elif de < 2.0:
            score += 3.0

    # 5. P/E positive & reasonable vs history/industry (15 pts)
    pe = metrics.get("pe")
    pe_hist_median = metrics.get("pe_hist_median")
    if pe is not None and pe > 0:
        ref_pe = pe_hist_median if (pe_hist_median and pe_hist_median > 0) else 15.0
        if pe <= min(15.0, ref_pe * 1.05):
            score += 15.0
        elif pe <= min(20.0, ref_pe * 1.2):
            score += 11.0
        elif pe <= 25.0:
            score += 6.0
        else:
            score += 2.0

    return round(min(100.0, max(0.0, score)), 1)


def score_financial(metrics: Dict[str, Optional[float]]) -> float:
    """Score Banking & Securities stock on 0-100 scale.

    - Net Profit YoY growth > 10% (30 pts)
    - ROE > 15% (25 pts)
    - Asset quality / NPL / provisioning stability (25 pts)
    - P/B reasonable vs ROE (20 pts)
    """
    score = 0.0

    # 1. Net Profit YoY > 10% (30 pts)
    profit_yoy = metrics.get("profit_yoy_pct")
    if profit_yoy is not None:
        if profit_yoy >= 20.0:
            score += 30.0
        elif profit_yoy > 10.0:
            score += 25.0
        elif profit_yoy > 5.0:
            score += 15.0
        elif profit_yoy > 0.0:
            score += 8.0

    # 2. ROE > 15% (25 pts)
    roe = metrics.get("roe_pct")
    if roe is not None:
        if roe >= 20.0:
            score += 25.0
        elif roe > 15.0:
            score += 21.0
        elif roe >= 12.0:
            score += 14.0
        elif roe >= 9.0:
            score += 7.0

    # 3. Asset quality / NPL / provisioning stability (25 pts)
    npl = metrics.get("npl_pct")
    llr = metrics.get("llr_pct")
    equity_to_assets = metrics.get("equity_to_assets_pct")

    if npl is not None:
        if npl <= 1.5:
            score += 15.0
        elif npl <= 2.5:
            score += 11.0
        elif npl <= 3.0:
            score += 6.0
    elif equity_to_assets is not None:
        # Securities firms or banks without explicit NPL field: evaluate capital buffer stability
        if equity_to_assets >= 10.0:
            score += 14.0
        elif equity_to_assets >= 7.0:
            score += 11.0
        elif equity_to_assets >= 5.0:
            score += 7.0
    else:
        score += 10.0  # neutral fallback if field unavailable

    if llr is not None:
        if llr >= 100.0:
            score += 10.0
        elif llr >= 70.0:
            score += 7.5
        elif llr >= 50.0:
            score += 4.0
    else:
        # If LLR unavailable, check profit stability across 4 quarters
        rev_yoy = metrics.get("rev_yoy_pct")
        if rev_yoy is not None and rev_yoy > 5.0:
            score += 8.0
        else:
            score += 5.0

    # 4. P/B reasonable vs ROE (20 pts)
    pb = metrics.get("pb")
    if pb is not None and pb > 0:
        # Justified P/B heuristic: ROE / 10 (e.g., ROE 18% -> fair P/B ~ 1.8)
        fair_pb = max(1.0, (roe or 14.0) / 10.0)
        ratio = pb / fair_pb
        if ratio <= 0.9:
            score += 20.0
        elif ratio <= 1.15:
            score += 16.0
        elif ratio <= 1.4:
            score += 10.0
        elif ratio <= 1.8:
            score += 5.0

    return round(min(100.0, max(0.0, score)), 1)


def extract_fundamental_metrics(
    statements: Dict[str, Optional[pd.DataFrame]],
    sector_group: str,
) -> Tuple[Dict[str, Optional[float]], bool]:
    """Extract normalized 4-8 quarter metrics from the 3 BCTC statements + ratio table."""
    inc = statements.get("income_statement")
    bal = statements.get("balance_sheet")
    cf = statements.get("cash_flow")
    rat = statements.get("ratio")

    latest_q = _extract_latest_quarter_label(inc) or _extract_latest_quarter_label(bal)
    missing_bctc = _is_missing_latest_bctc(latest_q)

    # Revenue (4 quarters TTM vs prior 4 quarters, or latest Q vs Q-4)
    revs = _safe_series(inc, ("revenue", "net_sales", "total_operating_income", "doanh thu", "net revenue"))
    profits = _safe_series(
        inc,
        (
            "attribute to parent",
            "post_tax_profit",
            "net_profit",
            "profit after tax",
            "share_holder_income",
            "lợi nhuận sau thuế",
        ),
    )

    rev_yoy_pct: Optional[float] = None
    if len(revs) >= 8 and abs(sum(revs[4:8])) > 1e-6:
        rev_yoy_pct = (sum(revs[:4]) - sum(revs[4:8])) / abs(sum(revs[4:8])) * 100.0
    elif len(revs) >= 5 and abs(revs[4]) > 1e-6:
        rev_yoy_pct = (revs[0] - revs[4]) / abs(revs[4]) * 100.0
    else:
        rg = _safe_series(inc, ("revenuegrowth", "revenue_yoy"))
        if rg:
            rev_yoy_pct = _normalize_pct(rg[0])

    profit_yoy_pct: Optional[float] = None
    if len(profits) >= 8 and abs(sum(profits[4:8])) > 1e-6:
        profit_yoy_pct = (sum(profits[:4]) - sum(profits[4:8])) / abs(sum(profits[4:8])) * 100.0
    elif len(profits) >= 5 and abs(profits[4]) > 1e-6:
        profit_yoy_pct = (profits[0] - profits[4]) / abs(profits[4]) * 100.0
    else:
        pg = _safe_series(inc, ("netprofitgrowth", "net_profit_yoy"))
        if pg:
            profit_yoy_pct = _normalize_pct(pg[0])

    # Operating Cash Flow (TTM 4 quarters) & OCF / Net Profit
    ocfs = _safe_series(
        cf,
        (
            "operating",
            "from_sale",
            "cash flows from operating",
            "lưu chuyển tiền tệ ròng từ các hoạt động sản xuất kinh doanh",
        ),
    )
    ocf_ttm = float(sum(ocfs[:4])) if len(ocfs) >= 4 else (float(ocfs[0]) if ocfs else None)
    np_ttm = float(sum(profits[:4])) if len(profits) >= 4 else (float(profits[0]) if profits else None)
    ocf_to_np: Optional[float] = None
    if ocf_ttm is not None and np_ttm is not None and np_ttm > 0:
        ocf_to_np = round(ocf_ttm / np_ttm, 2)

    # Ratios (ROE, D/E, P/E, P/B)
    roe_vals = _safe_series(rat, ("roe",))
    roe_pct = round(_normalize_pct(roe_vals[0]), 2) if roe_vals else None

    de_vals = _safe_series(rat, ("debt_on_equity", "debt/equity", "d/e", "debt_to_equity"))
    if de_vals:
        de_ratio = round(de_vals[0], 2)
    else:
        liab = _safe_series(bal, ("liabilities", "nợ phải trả"))
        eq = _safe_series(bal, ("owner's equity", "owners_equity", "equity", "vốn chủ sở hữu"))
        de_ratio = round(liab[0] / eq[0], 2) if (liab and eq and eq[0] > 0) else None

    pe_vals = _safe_series(rat, ("p/e", "price_to_earning", "pe"))
    pe = round(pe_vals[0], 2) if pe_vals else None
    pos_pes = [v for v in pe_vals[:8] if v > 0]
    pe_hist_median = round(float(np.median(pos_pes)), 2) if pos_pes else None

    pb_vals = _safe_series(rat, ("p/b", "price_to_book", "pb"))
    pb = round(pb_vals[0], 2) if pb_vals else None

    # Financial-specific metrics (NPL, LLR, Equity/Assets)
    npl_vals = _safe_series(rat, ("bad_debt", "npl", "nợ xấu"))
    npl_pct = round(_normalize_pct(npl_vals[0]), 2) if npl_vals else None

    llr_vals = _safe_series(rat, ("llr", "loan_loss", "bao phủ nợ"))
    llr_pct = round(_normalize_pct(llr_vals[0]), 2) if llr_vals else None

    assets = _safe_series(bal, ("total assets", "asset", "tổng cộng tài sản"))
    eq_bal = _safe_series(bal, ("owner's equity", "owners_equity", "equity", "vốn chủ sở hữu"))
    equity_to_assets_pct = (
        round(eq_bal[0] / assets[0] * 100.0, 2) if (assets and eq_bal and assets[0] > 0) else None
    )

    metrics: Dict[str, Optional[float]] = {
        "rev_yoy_pct": round(rev_yoy_pct, 2) if rev_yoy_pct is not None else None,
        "profit_yoy_pct": round(profit_yoy_pct, 2) if profit_yoy_pct is not None else None,
        "roe_pct": roe_pct,
        "ocf_ttm": round(ocf_ttm, 2) if ocf_ttm is not None else None,
        "net_profit_ttm": round(np_ttm, 2) if np_ttm is not None else None,
        "ocf_to_net_profit": ocf_to_np,
        "de_ratio": de_ratio,
        "pe": pe,
        "pe_hist_median": pe_hist_median,
        "pb": pb,
        "npl_pct": npl_pct,
        "llr_pct": llr_pct,
        "equity_to_assets_pct": equity_to_assets_pct,
        "latest_quarter": f"Q{latest_q[1]}/{latest_q[0]}" if latest_q else "N/A",
    }
    return metrics, missing_bctc


def run_update_fundamentals(limit: int = 0) -> None:
    """Main entry point for Tier 1 fundamental screening."""
    vn = VnstockClient()
    fb = FirebaseClient()

    listed = vn.get_listed_symbols()
    if not listed:
        logger.error("No listed HOSE/HNX symbols retrieved. Aborting.")
        return

    if limit > 0:
        listed = listed[:limit]
        logger.info("Limiting scan to first %d tickers (--limit)", limit)

    # Batch actual prices for accurate VND pricing
    all_tickers = [item["ticker"] for item in listed]
    actual_prices_map = vn.get_actual_prices_batch(all_tickers)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    existing_cache = fb.get_all_fundamentals()
    passed_count = 0
    liquid_count = 0
    total_listed = len(listed)

    def _process_one(idx_item: Tuple[int, Dict[str, str]]) -> Tuple[bool, bool]:
        idx, item = idx_item
        ticker = item["ticker"]
        exchange = item["exchange"]
        sector_group = item["sector_group"]

        try:
            df_ohlcv = vn.get_ohlcv(ticker, days=45)
            if df_ohlcv is None or len(df_ohlcv) < 20:
                return (False, False)

            recent_20 = df_ohlcv.tail(20)
            avg_value_20d = float((recent_20["close"] * recent_20["volume"]).mean())
            if avg_value_20d < MIN_AVG_VALUE_20D_VND:
                return (False, False)

            actual_close = float(
                actual_prices_map.get(ticker, {}).get("actual_close") or recent_20.iloc[-1]["close"]
            )
            fit_500k = bool(
                actual_close < FIT_500K_MAX_PRICE_VND
                and avg_value_20d >= FIT_500K_MIN_AVG_VALUE_VND
            )

            statements = vn.get_financial_statements(ticker)
            has_any_bctc = any(
                isinstance(statements.get(k), pd.DataFrame) and not statements[k].empty
                for k in ("income_statement", "balance_sheet", "cash_flow")
            )

            if not has_any_bctc:
                prev_doc = existing_cache.get(ticker)
                if prev_doc and float(prev_doc.get("score", 0)) >= MIN_FUNDAMENTAL_SCORE:
                    fb.save_fundamental(
                        ticker,
                        {
                            **prev_doc,
                            "missing_bctc": True,
                            "fit_500k": fit_500k,
                            "actual_close": round(actual_close, 0),
                            "avg_value_20d": round(avg_value_20d, 0),
                        },
                    )
                    return (True, True)
                return (True, False)

            metrics, missing_bctc = extract_fundamental_metrics(statements, sector_group)
            score = score_financial(metrics) if sector_group == "financial" else score_non_financial(metrics)

            if missing_bctc and ticker in existing_cache:
                prev_score = float(existing_cache[ticker].get("score", 0))
                if prev_score > score:
                    score = prev_score

            if score >= MIN_FUNDAMENTAL_SCORE:
                doc_payload = {
                    "ticker": ticker,
                    "exchange": exchange,
                    "industry": item.get("industry", ""),
                    "sector_group": sector_group,
                    "score": score,
                    "missing_bctc": missing_bctc,
                    "fit_500k": fit_500k,
                    "actual_close": round(actual_close, 0),
                    "avg_value_20d": round(avg_value_20d, 0),
                    "metrics": metrics,
                }
                fb.save_fundamental(ticker, doc_payload)
                logger.info(
                    "[%d/%d] SAVED %s (%s | %s) | Score=%.1f | fit_500k=%s | missing_bctc=%s",
                    idx,
                    total_listed,
                    ticker,
                    exchange,
                    sector_group,
                    score,
                    fit_500k,
                    missing_bctc,
                )
                return (True, True)

            return (True, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error processing %s: %s", ticker, exc)
            return (False, False)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_process_one, pair) for pair in enumerate(listed, 1)]
        for fut in as_completed(futures):
            is_liq, is_saved = fut.result()
            if is_liq:
                liquid_count += 1
            if is_saved:
                passed_count += 1

    logger.info(
        "Tier 1 complete: scanned=%d, passed_liquidity=%d, saved_fundamentals(>=%.0f)=%d",
        total_listed,
        liquid_count,
        MIN_FUNDAMENTAL_SCORE,
        passed_count,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update weekly fundamental scores in Firestore.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tickers (for testing).")
    args = parser.parse_args()
    run_update_fundamentals(limit=args.limit)
