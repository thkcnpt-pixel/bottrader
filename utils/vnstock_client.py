"""Direct HTTP Vietnam Stock Market Client (VCI -> VNDirect -> TCBS fallback).

Uses pure `requests` against official public endpoints (zero dependency on the
quarantined PyPI `vnstock` package) with automatic retries and multi-source fallback.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

from utils.config import (
    ALLOWED_EXCHANGES,
    FINANCIAL_SECTOR_KEYWORDS,
    VNSTOCK_FALLBACK_SOURCE,
    VNSTOCK_PRIMARY_SOURCE,
    vn_now,
)

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://trading.vietcap.com.vn/",
    "Origin": "https://trading.vietcap.com.vn",
}

REQUEST_TIMEOUT = 15


def _normalize_vnd_price(val: float, is_index: bool = False) -> float:
    """Normalize stock prices to VND (multiply by 1000 if returned in thousand-VND units)."""
    if is_index or pd.isna(val):
        return float(val) if not pd.isna(val) else 0.0
    v = float(val)
    if 0 < v < 1000.0:
        return round(v * 1000.0, 2)
    return v


def _classify_sector_group(industry_text: str) -> str:
    """Classify a ticker into 'financial' (banks/securities) or 'non_financial'."""
    text = (industry_text or "").strip().lower()
    for kw in FINANCIAL_SECTOR_KEYWORDS:
        if kw in text:
            return "financial"
    return "non_financial"


class VnstockClient:
    """Resilient VN stock data client with VCI -> VNDirect -> TCBS automatic fallback."""

    def __init__(
        self,
        primary_source: str = VNSTOCK_PRIMARY_SOURCE,
        fallback_source: str = VNSTOCK_FALLBACK_SOURCE,
        max_retries: int = 2,
        retry_delay: float = 1.2,
    ) -> None:
        ordered = [primary_source, "VNDIRECT", fallback_source]
        self.sources = list(dict.fromkeys([s for s in ordered if s]))
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        resp = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _call_with_fallback(
        self,
        action_name: str,
        handlers: Dict[str, Callable[[], Any]],
    ) -> Tuple[Any, Optional[str]]:
        from utils.data_providers import GLOBAL_PROVIDER_HEALTH

        last_err: Optional[Exception] = None
        for source in self.sources:
            fn = handlers.get(source)
            if fn is None:
                continue
            for attempt in range(1, self.max_retries + 1):
                t0 = time.perf_counter()
                try:
                    res = fn()
                    if res is not None:
                        if isinstance(res, pd.DataFrame) and res.empty:
                            raise ValueError(f"Empty DataFrame from {source}")
                        if isinstance(res, (list, dict)) and len(res) == 0:
                            raise ValueError(f"Empty payload from {source}")
                        GLOBAL_PROVIDER_HEALTH.record_call(
                            source,
                            success=True,
                            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                        return res, source
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    GLOBAL_PROVIDER_HEALTH.record_call(
                        source,
                        success=False,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                        error=str(exc),
                    )
                    logger.warning(
                        "[market_api] %s failed on %s (%d/%d): %s",
                        action_name,
                        source,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay * attempt)
        logger.error("[market_api] All sources failed for %s: %s", action_name, last_err)
        return None, None

    # -------------------- 1. Listing (HOSE & HNX) --------------------
    def get_listed_symbols(self) -> List[Dict[str, str]]:
        """Fetch normal-listed HOSE & HNX equities with sector classification."""
        def _vci() -> List[Dict[str, str]]:
            data = self._request_json("GET", "https://trading.vietcap.com.vn/api/price/symbols/getAll")
            items = data if isinstance(data, list) else data.get("data", [])
            out: List[Dict[str, str]] = []
            seen = set()
            for row in items:
                ticker = str(row.get("symbol") or "").strip().upper()
                if len(ticker) != 3 or not ticker.isalpha() or ticker in seen:
                    continue
                board = str(row.get("board") or "").strip().upper()
                if board in ("HSX", "HOSE"):
                    exchange = "HOSE"
                elif board == "HNX":
                    exchange = "HNX"
                else:
                    continue
                if str(row.get("type") or "STOCK").upper() not in ("STOCK", ""):
                    continue
                industry = str(row.get("icbName2") or row.get("icbName3") or row.get("organName") or "")
                seen.add(ticker)
                out.append({
                    "ticker": ticker,
                    "exchange": exchange,
                    "industry": industry,
                    "sector_group": _classify_sector_group(industry),
                    "source": "VCI",
                })
            return out

        def _vndirect() -> List[Dict[str, str]]:
            data = self._request_json(
                "GET",
                "https://finfo-api.vndirect.com.vn/v4/stocks",
                params={"q": "type:STOCK~status:LISTED~floor:HOSE,HNX", "size": 2000},
                headers={"Referer": "https://dstock.vndirect.com.vn/"},
            )
            items = data.get("data", []) if isinstance(data, dict) else []
            out: List[Dict[str, str]] = []
            seen = set()
            for row in items:
                ticker = str(row.get("code") or "").strip().upper()
                if len(ticker) != 3 or not ticker.isalpha() or ticker in seen:
                    continue
                floor = str(row.get("floor") or "").strip().upper()
                if floor not in ALLOWED_EXCHANGES:
                    continue
                industry = str(row.get("industryName") or row.get("companyName") or "")
                seen.add(ticker)
                out.append({
                    "ticker": ticker,
                    "exchange": floor,
                    "industry": industry,
                    "sector_group": _classify_sector_group(industry),
                    "source": "VNDIRECT",
                })
            return out

        res, used = self._call_with_fallback("get_listed_symbols", {"VCI": _vci, "VNDIRECT": _vndirect})
        if not res:
            return []
        logger.info("Loaded %d eligible HOSE/HNX tickers (source=%s)", len(res), used)
        return res

    # -------------------- 2. Daily OHLCV History --------------------
    def get_ohlcv(
        self,
        symbol: str,
        days: int = 160,
        is_index: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV history (adjusted prices for indicators)."""
        end_dt = vn_now()
        start_dt = end_dt - timedelta(days=days)
        ts_start = int(start_dt.timestamp())
        ts_end = int(end_dt.timestamp())
        sym = symbol.upper().strip()

        def _vci() -> pd.DataFrame:
            payload = {
                "timeFrame": "ONE_DAY",
                "symbols": [sym],
                "to": ts_end,
                "countBack": max(60, int(days * 0.75)),
            }
            data = self._request_json(
                "POST",
                "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart",
                json_body=payload,
            )
            item = data[0] if isinstance(data, list) and data else {}
            times = item.get("t", [])
            if not times:
                return pd.DataFrame()
            df = pd.DataFrame({
                "time": pd.to_datetime([int(x) for x in times], unit="s"),
                "open": item.get("o", []),
                "high": item.get("h", []),
                "low": item.get("l", []),
                "close": item.get("c", []),
                "volume": item.get("v", []),
            })
            return df

        def _vndirect() -> pd.DataFrame:
            data = self._request_json(
                "GET",
                "https://dchart-api.vndirect.com.vn/dchart/history",
                params={"resolution": "D", "symbol": sym, "from": ts_start, "to": ts_end},
                headers={"Referer": "https://dchart.vndirect.com.vn/"},
            )
            times = data.get("t", []) if isinstance(data, dict) else []
            if not times:
                return pd.DataFrame()
            return pd.DataFrame({
                "time": pd.to_datetime([int(x) for x in times], unit="s"),
                "open": data.get("o", []),
                "high": data.get("h", []),
                "low": data.get("l", []),
                "close": data.get("c", []),
                "volume": data.get("v", []),
            })

        def _tcbs() -> pd.DataFrame:
            bar_type = "index" if is_index else "stock"
            data = self._request_json(
                "GET",
                "https://apipubaws.tcbs.com.vn/stock-insight/v2/stock/bars-long-term",
                params={"ticker": sym, "type": bar_type, "resolution": "D", "from": ts_start, "to": ts_end},
                headers={"Referer": "https://tcinvest.tcbs.com.vn/"},
            )
            rows = data.get("data", []) if isinstance(data, dict) else []
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df.rename(columns={"tradingDate": "time"}, inplace=True)
            df["time"] = pd.to_datetime(df["time"])
            return df[["time", "open", "high", "low", "close", "volume"]]

        df, _ = self._call_with_fallback(
            f"get_ohlcv({sym})",
            {"VCI": _vci, "VNDIRECT": _vndirect, "TCBS": _tcbs},
        )
        if df is None or df.empty:
            return None

        out = df.copy()
        out["time"] = pd.to_datetime(out["time"]).dt.tz_localize(None)
        out = out.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
        for col in ("open", "high", "low", "close"):
            out[col] = pd.to_numeric(out[col], errors="coerce").apply(
                lambda x: _normalize_vnd_price(x, is_index=is_index)
            )
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
        return out.dropna(subset=["close"]).reset_index(drop=True)

    # -------------------- 3. Actual / Live Price Board --------------------
    def get_actual_prices_batch(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Fetch latest actual (unadjusted) match/close prices & ref prices."""
        if not symbols:
            return {}

        result: Dict[str, Dict[str, float]] = {}
        chunk_size = 50

        for i in range(0, len(symbols), chunk_size):
            chunk = [s.upper().strip() for s in symbols[i : i + chunk_size]]

            def _vci(batch=chunk) -> Dict[str, Dict[str, float]]:
                data = self._request_json(
                    "POST",
                    "https://trading.vietcap.com.vn/api/price/symbols/getList",
                    json_body={"symbols": batch},
                )
                rows = data if isinstance(data, list) else data.get("data", [])
                parsed: Dict[str, Dict[str, float]] = {}
                for row in rows:
                    listing = row.get("listingInfo") or {}
                    match = row.get("matchPrice") or {}
                    t = str(listing.get("symbol") or row.get("symbol") or "").strip().upper()
                    if not t:
                        continue
                    mp = match.get("matchPrice") or match.get("closePrice") or 0
                    rp = listing.get("refPrice") or match.get("referencePrice") or mp
                    vol = match.get("accumulatedVolume") or match.get("matchVol") or 0
                    actual_p = _normalize_vnd_price(float(mp)) if mp else _normalize_vnd_price(float(rp))
                    ref_p = _normalize_vnd_price(float(rp)) if rp else actual_p
                    if actual_p > 0:
                        parsed[t] = {
                            "actual_close": float(actual_p),
                            "ref_price": float(ref_p),
                            "volume": float(vol or 0),
                        }
                return parsed

            def _vndirect(batch=chunk) -> Dict[str, Dict[str, float]]:
                codes = ",".join(batch)
                data = self._request_json(
                    "GET",
                    "https://finfo-api.vndirect.com.vn/v4/stock_prices",
                    params={"sort": "date", "q": f"code:{codes}", "size": len(batch) * 2},
                    headers={"Referer": "https://dstock.vndirect.com.vn/"},
                )
                rows = data.get("data", []) if isinstance(data, dict) else []
                parsed: Dict[str, Dict[str, float]] = {}
                for row in rows:
                    t = str(row.get("code") or "").strip().upper()
                    if not t or t in parsed:
                        continue
                    close_p = _normalize_vnd_price(float(row.get("close") or 0))
                    ref_p = _normalize_vnd_price(float(row.get("basicPrice") or close_p))
                    vol = float(row.get("nmVolume") or row.get("volume") or 0)
                    if close_p > 0:
                        parsed[t] = {
                            "actual_close": close_p,
                            "ref_price": ref_p,
                            "volume": vol,
                        }
                return parsed

            batch_res, _ = self._call_with_fallback(
                f"price_board({len(chunk)})",
                {"VCI": _vci, "VNDIRECT": _vndirect},
            )
            if isinstance(batch_res, dict):
                result.update(batch_res)

        return result

    # -------------------- 4. Financial Statements (Vietcap IQ REST -> VCI GraphQL -> Simplize -> TCBS) --------------------
    def _fetch_bctc_vietcap_iq(self, sym: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Source 1: Vietcap IQ REST API (all 3 BCTC statements + ratios)."""
        iq_headers = {
            "Referer": "https://iq.vietcap.com.vn/",
            "Origin": "https://iq.vietcap.com.vn",
        }
        base = f"https://iq.vietcap.com.vn/api/iq-insight-service/v1/company/{sym}"

        # Ratio summary from Vietcap IQ statistics-financial
        r_ratio = self.session.get(f"{base}/statistics-financial", headers=iq_headers, timeout=8)
        ratio_data = r_ratio.json().get("data") if r_ratio.status_code == 200 else None

        r_inc = self.session.get(f"{base}/financial-statement", params={"section": "INCOME_STATEMENT"}, headers=iq_headers, timeout=8)
        inc_quarters = (r_inc.json().get("data") or {}).get("quarters", []) if r_inc.status_code == 200 else []

        r_bal = self.session.get(f"{base}/financial-statement", params={"section": "BALANCE_SHEET"}, headers=iq_headers, timeout=8)
        bal_quarters = (r_bal.json().get("data") or {}).get("quarters", []) if r_bal.status_code == 200 else []

        r_cf = self.session.get(f"{base}/financial-statement", params={"section": "CASH_FLOW"}, headers=iq_headers, timeout=8)
        cf_quarters = (r_cf.json().get("data") or {}).get("quarters", []) if r_cf.status_code == 200 else []

        if not inc_quarters and not ratio_data:
            return {}

        def _parse_quarters(qlist: List[Dict[str, Any]], mapping: Dict[str, Tuple[str, ...]]) -> Optional[pd.DataFrame]:
            if not qlist:
                return None
            sorted_q = sorted(
                qlist,
                key=lambda q: (int(q.get("yearReport") or 0), int(q.get("lengthReport") or 0)),
                reverse=True,
            )
            out_rows = []
            for q in sorted_q[:8]:
                rec: Dict[str, Any] = {
                    "yearReport": q.get("yearReport"),
                    "lengthReport": q.get("lengthReport"),
                }
                for out_col, keys in mapping.items():
                    for k in keys:
                        if q.get(k) is not None:
                            rec[out_col] = float(q[k])
                            break
                out_rows.append(rec)
            return pd.DataFrame(out_rows) if out_rows else None

        inc_df = _parse_quarters(inc_quarters, {
            "revenue": ("isa3", "isb27", "iss3", "revenue"),
            "post_tax_profit": ("isa22", "isa20", "isb41", "iss22", "netProfit"),
        })
        bal_df = _parse_quarters(bal_quarters, {
            "total assets": ("bsa53", "bsb108", "bss53", "totalAsset"),
            "liabilities": ("bsa54", "bsb109", "bss54", "totalLiability"),
            "owner's equity": ("bsa78", "bsb110", "bss78", "totalEquity"),
        })
        cf_df = _parse_quarters(cf_quarters, {
            "operating": ("cfa18", "cfb20", "cfs18", "cfa1", "cfb1"),
        })

        rat_df = None
        if isinstance(ratio_data, dict) and ratio_data:
            rat_df = pd.DataFrame([{
                "roe": ratio_data.get("roe") or ratio_data.get("roae"),
                "pe": ratio_data.get("pe") or ratio_data.get("priceToEarning") or ratio_data.get("peRatio"),
                "pb": ratio_data.get("pb") or ratio_data.get("priceToBook") or ratio_data.get("pbRatio"),
                "debt_on_equity": ratio_data.get("de") or ratio_data.get("debtToEquity") or ratio_data.get("debtOnEquity"),
                "bad_debt": ratio_data.get("badDebtRatio") or ratio_data.get("npl"),
                "llr": ratio_data.get("llr"),
            }])

        return {
            "income_statement": inc_df,
            "balance_sheet": bal_df,
            "cash_flow": cf_df,
            "ratio": rat_df,
        }

    def _fetch_bctc_vci_graphql(self, sym: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Source 2: Vietcap (VCI) GraphQL quarterly financial ratios."""
        query = """
        query CompanyFinancialRatio($ticker: String!, $period: String!) {
          CompanyFinancialRatio(ticker: $ticker, period: $period) {
            ratio {
              yearReport
              lengthReport
              revenue
              revenueGrowth
              netProfit
              netProfitGrowth
              roe
              roa
              de
              pe
              pb
              eps
              bvps
              ebitda
            }
          }
        }
        """
        rows = []
        for period_val in ("QUARTER", "Q"):
            data = self._request_json(
                "POST",
                "https://trading.vietcap.com.vn/data-mt/graphql",
                json_body={"query": query, "variables": {"ticker": sym, "period": period_val}},
            )
            if isinstance(data, dict) and data.get("errors"):
                continue
            rows = (
                ((data or {}).get("data") or {})
                .get("CompanyFinancialRatio", {})
                .get("ratio", [])
            ) or []
            if rows:
                break

        if not rows:
            return {}

        df = pd.DataFrame(rows)
        if "yearReport" in df.columns and "lengthReport" in df.columns:
            df = df.sort_values(["yearReport", "lengthReport"], ascending=[False, False]).reset_index(drop=True)
        df = df.head(8)

        inc_cols = [c for c in ("yearReport", "lengthReport", "revenue", "netProfit", "revenueGrowth", "netProfitGrowth") if c in df.columns]
        bal_cols = [c for c in ("yearReport", "lengthReport", "de", "bvps") if c in df.columns]
        rat_cols = [c for c in ("yearReport", "lengthReport", "roe", "roa", "de", "pe", "pb", "eps") if c in df.columns]

        return {
            "income_statement": df[inc_cols].rename(columns={"netProfit": "post_tax_profit"}) if inc_cols else None,
            "balance_sheet": df[bal_cols].rename(columns={"de": "debt_on_equity"}) if bal_cols else None,
            "cash_flow": None,
            "ratio": df[rat_cols].rename(columns={"de": "debt_on_equity"}) if rat_cols else None,
        }

    def _fetch_bctc_simplize(self, sym: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Source 3: Simplize public financial summary API."""
        resp = self.session.get(
            f"https://api.simplize.vn/api/company/summary/{sym}",
            headers={"Referer": "https://simplize.vn/", "Origin": "https://simplize.vn"},
            timeout=8,
        )
        if resp.status_code != 200:
            return {}
        d = (resp.json() or {}).get("data") or {}
        if not d:
            return {}

        inc_df = pd.DataFrame([{
            "revenuegrowth": d.get("revenueGrowth"),
            "netprofitgrowth": d.get("netIncomeGrowth") or d.get("profitGrowth"),
            "post_tax_profit": d.get("netIncome"),
        }])
        rat_df = pd.DataFrame([{
            "roe": d.get("roe"),
            "pe": d.get("peRatio"),
            "pb": d.get("pbRatio"),
            "debt_on_equity": d.get("debtEquity") or d.get("debtToEquity"),
        }])
        return {
            "income_statement": inc_df,
            "balance_sheet": rat_df,
            "cash_flow": None,
            "ratio": rat_df,
        }

    def _fetch_bctc_tcbs(self, sym: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Source 4: TCBS quarterly financial statements & ratios."""
        statements: Dict[str, Optional[pd.DataFrame]] = {
            "income_statement": None,
            "balance_sheet": None,
            "cash_flow": None,
            "ratio": None,
        }
        tcbs_endpoints = {
            "income_statement": f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/finance/{sym}/incomestatement",
            "balance_sheet": f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/finance/{sym}/balancesheet",
            "cash_flow": f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/finance/{sym}/cashflow",
            "ratio": f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/finance/{sym}/financialratio",
        }
        for key, url in tcbs_endpoints.items():
            try:
                resp = self.session.get(
                    url,
                    params={"yearly": 0, "isAll": "true"},
                    headers={"Referer": "https://tcinvest.tcbs.com.vn/"},
                    timeout=6,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
                if rows:
                    df = pd.DataFrame(rows)
                    if "year" in df.columns and "quarter" in df.columns:
                        df = df.sort_values(["year", "quarter"], ascending=[False, False]).reset_index(drop=True)
                    statements[key] = df.head(8)
            except Exception as exc:  # noqa: BLE001
                logger.debug("TCBS %s failed for %s: %s", key, sym, exc)
        return statements

    def get_financial_statements(self, symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Fetch quarterly BCTC with 4-source fallback: Vietcap IQ -> VCI GraphQL -> Simplize -> TCBS."""
        sym = symbol.upper().strip()
        empty = {"income_statement": None, "balance_sheet": None, "cash_flow": None, "ratio": None}

        for source_name, fetcher in (
            ("VIETCAP_IQ", self._fetch_bctc_vietcap_iq),
            ("VCI_GRAPHQL", self._fetch_bctc_vci_graphql),
            ("SIMPLIZE", self._fetch_bctc_simplize),
            ("TCBS", self._fetch_bctc_tcbs),
        ):
            try:
                res = fetcher(sym)
                if res and any(
                    isinstance(res.get(k), pd.DataFrame) and not res[k].empty
                    for k in ("income_statement", "balance_sheet", "cash_flow", "ratio")
                ):
                    logger.info("[BCTC] Loaded %s from %s", sym, source_name)
                    return {**empty, **res}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[BCTC] %s failed for %s: %s", source_name, sym, exc)

        return empty

    # -------------------- 5. Company News & Dividend Events --------------------
    def get_company_news_and_events(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch recent company news and corporate events/dividends from TCBS."""
        sym = symbol.upper().strip()
        news_items: List[Dict[str, Any]] = []
        event_items: List[Dict[str, Any]] = []

        try:
            data = self._request_json(
                "GET",
                f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/ticker/{sym}/activity-news",
                params={"page": 0, "size": 8},
                headers={"Referer": "https://tcinvest.tcbs.com.vn/"},
            )
            rows = data.get("listActivityNews", []) if isinstance(data, dict) else []
            for row in rows[:8]:
                title = str(row.get("title") or "").strip()
                if title:
                    news_items.append({
                        "title": title,
                        "date": str(row.get("publishDate") or ""),
                        "url": str(row.get("source") or ""),
                        "source": "TCBS",
                        "summary": str(row.get("title") or ""),
                    })
        except Exception as exc:  # noqa: BLE001
            logger.debug("TCBS news failed for %s: %s", sym, exc)

        try:
            data = self._request_json(
                "GET",
                f"https://apipubaws.tcbs.com.vn/tcanalysis/v1/ticker/{sym}/events-news",
                params={"page": 0, "size": 6},
                headers={"Referer": "https://tcinvest.tcbs.com.vn/"},
            )
            rows = data.get("listEventNews", []) if isinstance(data, dict) else []
            for row in rows[:6]:
                event_items.append({
                    "title": str(row.get("eventName") or row.get("eventDesc") or ""),
                    "exright_date": str(row.get("exerRightDate") or ""),
                    "issue_date": str(row.get("pubDate") or ""),
                    "ratio": str(row.get("ratio") or ""),
                    "value": str(row.get("value") or ""),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("TCBS events failed for %s: %s", sym, exc)

        return {"news": news_items, "events": event_items}
