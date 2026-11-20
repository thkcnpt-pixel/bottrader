"""Resilient vnstock v3 client with automatic VCI -> TCBS fallback and retries."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from utils.config import (
    ALLOWED_EXCHANGES,
    FINANCIAL_SECTOR_KEYWORDS,
    VNSTOCK_FALLBACK_SOURCE,
    VNSTOCK_PRIMARY_SOURCE,
    vn_now,
)

logger = logging.getLogger(__name__)


def _normalize_vnd_price(val: float, is_index: bool = False) -> float:
    """Normalize vnstock price units.

    VCI/TCBS often return stock prices in thousands of VND (e.g., 25.5 == 25,500 VND).
    Index values (e.g., VNINDEX ~ 1250) should NOT be multiplied.
    """
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
    """Wrapper around vnstock with automatic VCI -> TCBS fallback & retry."""

    def __init__(
        self,
        primary_source: str = VNSTOCK_PRIMARY_SOURCE,
        fallback_source: str = VNSTOCK_FALLBACK_SOURCE,
        max_retries: int = 2,
        retry_delay: float = 1.5,
    ) -> None:
        self.sources = [primary_source]
        if fallback_source and fallback_source != primary_source:
            self.sources.append(fallback_source)
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _call_with_fallback(
        self,
        action_name: str,
        fn_factory: Callable[[str], Any],
    ) -> Tuple[Any, Optional[str]]:
        """Execute fn_factory(source) trying primary source first, then fallback."""
        last_err: Optional[Exception] = None
        for source in self.sources:
            for attempt in range(1, self.max_retries + 1):
                try:
                    result = fn_factory(source)
                    if result is not None:
                        if isinstance(result, pd.DataFrame) and result.empty:
                            raise ValueError(f"Empty DataFrame returned from {source}")
                        return result, source
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    logger.warning(
                        "[vnstock] %s failed on source=%s (attempt %d/%d): %s",
                        action_name,
                        source,
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay * attempt)
            logger.info("[vnstock] Switching fallback source after %s failed for %s", source, action_name)

        logger.error("[vnstock] All sources failed for %s: %s", action_name, last_err)
        return None, None

    @staticmethod
    def _get_stock(symbol: str, source: str):
        from vnstock import Vnstock  # lazy import for fast startup & testability

        return Vnstock().stock(symbol=symbol, source=source)

    def get_listed_symbols(self) -> List[Dict[str, str]]:
        """Fetch all normal-listed HOSE & HNX stocks with exchange and sector_group."""
        def _fetch(source: str) -> pd.DataFrame:
            stock = self._get_stock("VNM", source)
            df_ex = stock.listing.symbols_by_exchange()
            if df_ex is None or df_ex.empty:
                df_ex = stock.listing.all_symbols()
            try:
                df_ind = stock.listing.symbols_by_industries()
            except Exception:  # noqa: BLE001
                df_ind = pd.DataFrame()
            return (df_ex, df_ind)

        res, used_source = self._call_with_fallback("get_listed_symbols", _fetch)
        if res is None:
            return []

        df_ex, df_ind = res
        df_ex = df_ex.copy()
        df_ex.columns = [str(c).strip().lower() for c in df_ex.columns]

        sym_col = next((c for c in ("symbol", "ticker", "code") if c in df_ex.columns), None)
        ex_col = next((c for c in ("exchange", "comgroupcode", "board", "market") if c in df_ex.columns), None)
        type_col = next((c for c in ("type", "organ_type", "stock_type") if c in df_ex.columns), None)
        status_col = next((c for c in ("status", "trading_status", "warning", "delisted") if c in df_ex.columns), None)

        if not sym_col:
            logger.error("Could not identify symbol column in listing DataFrame: %s", list(df_ex.columns))
            return []

        # Build industry lookup map
        industry_map: Dict[str, str] = {}
        if isinstance(df_ind, pd.DataFrame) and not df_ind.empty:
            df_ind = df_ind.copy()
            df_ind.columns = [str(c).strip().lower() for c in df_ind.columns]
            ind_sym = next((c for c in ("symbol", "ticker") if c in df_ind.columns), None)
            ind_cols = [c for c in ("icb_name2", "icb_name3", "icb_name4", "industry", "sector") if c in df_ind.columns]
            if ind_sym and ind_cols:
                for _, row in df_ind.iterrows():
                    s = str(row[ind_sym]).strip().upper()
                    joined = " ".join(str(row[c]) for c in ind_cols if pd.notna(row[c]))
                    industry_map[s] = joined

        results: List[Dict[str, str]] = []
        seen = set()

        for _, row in df_ex.iterrows():
            ticker = str(row[sym_col]).strip().upper()
            # Standard VN equities are 3-character alphabetic tickers (excludes ETFs/CW/bonds)
            if not ticker or len(ticker) != 3 or not ticker.isalpha() or ticker in seen:
                continue

            exchange_raw = str(row[ex_col]).strip().upper() if ex_col and pd.notna(row[ex_col]) else ""
            if exchange_raw in ("HSX", "HOSE"):
                exchange = "HOSE"
            elif exchange_raw == "HNX":
                exchange = "HNX"
            else:
                continue

            if exchange not in ALLOWED_EXCHANGES:
                continue

            # Exclude non-stock securities if type column is present
            if type_col and pd.notna(row[type_col]):
                t_val = str(row[type_col]).strip().upper()
                if t_val not in ("STOCK", "CP", "EQUITY", ""):
                    continue

            # Exclude warned / controlled / restricted / halted stocks
            if status_col and pd.notna(row[status_col]):
                st = str(row[status_col]).strip().lower()
                bad_flags = ("warn", "control", "restrict", "halt", "suspend", "delist", "cảnh báo", "kiểm soát", "hạn chế")
                if any(flag in st for flag in bad_flags):
                    continue

            industry_str = industry_map.get(ticker, "")
            seen.add(ticker)
            results.append({
                "ticker": ticker,
                "exchange": exchange,
                "industry": industry_str,
                "sector_group": _classify_sector_group(industry_str),
                "source": used_source or "",
            })

        logger.info("Loaded %d eligible HOSE/HNX tickers (source=%s)", len(results), used_source)
        return results

    def get_ohlcv(
        self,
        symbol: str,
        days: int = 160,
        is_index: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV history (adjusted prices for indicators).

        Returns a DataFrame sorted ascending by `time` with columns:
        `time`, `open`, `high`, `low`, `close`, `volume`.
        """
        end_dt = vn_now()
        start_dt = end_dt - timedelta(days=days)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        def _fetch(source: str) -> pd.DataFrame:
            stock = self._get_stock(symbol, source)
            return stock.quote.history(start=start_str, end=end_str, interval="1D")

        df, _ = self._call_with_fallback(f"get_ohlcv({symbol})", _fetch)
        if df is None or df.empty:
            return None

        out = df.copy()
        out.columns = [str(c).strip().lower() for c in out.columns]
        if "time" not in out.columns and "date" in out.columns:
            out.rename(columns={"date": "time"}, inplace=True)

        required = {"time", "open", "high", "low", "close", "volume"}
        if not required.issubset(set(out.columns)):
            return None

        out["time"] = pd.to_datetime(out["time"])
        out = out.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)

        for col in ("open", "high", "low", "close"):
            out[col] = pd.to_numeric(out[col], errors="coerce").apply(
                lambda x: _normalize_vnd_price(x, is_index=is_index)
            )
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
        out = out.dropna(subset=["close"]).reset_index(drop=True)
        return out

    def get_actual_prices_batch(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Fetch actual (unadjusted) latest prices & reference prices via trading.price_board.

        Returns map: ticker -> {actual_close, ref_price, volume, exchange}
        """
        if not symbols:
            return {}

        result: Dict[str, Dict[str, float]] = {}
        chunk_size = 50

        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]

            def _fetch(source: str, batch=chunk) -> pd.DataFrame:
                stock = self._get_stock(batch[0], source)
                return stock.trading.price_board(symbols_list=batch)

            df, _ = self._call_with_fallback(f"price_board({len(chunk)})", _fetch)
            if df is None or df.empty:
                continue

            flat = df.copy()
            if isinstance(flat.columns, pd.MultiIndex):
                flat.columns = [
                    "_".join(str(p).strip().lower() for p in col if str(p).strip())
                    for col in flat.columns.values
                ]
            else:
                flat.columns = [str(c).strip().lower() for c in flat.columns]

            sym_col = next((c for c in flat.columns if "symbol" in c or "ticker" in c), None)
            price_col = next(
                (c for c in flat.columns if any(k in c for k in ("match_price", "close_price", "match_match_price", "last_price"))),
                None,
            )
            ref_col = next(
                (c for c in flat.columns if any(k in c for k in ("ref_price", "reference", "prior_close"))),
                None,
            )
            vol_col = next(
                (c for c in flat.columns if any(k in c for k in ("total_volume", "accumulated_volume", "match_vol", "volume"))),
                None,
            )

            if not sym_col:
                continue

            for _, row in flat.iterrows():
                t = str(row[sym_col]).strip().upper()
                match_p = pd.to_numeric(row[price_col], errors="coerce") if price_col else np.nan
                ref_p = pd.to_numeric(row[ref_col], errors="coerce") if ref_col else np.nan
                vol = pd.to_numeric(row[vol_col], errors="coerce") if vol_col else 0.0

                actual_p = _normalize_vnd_price(match_p) if not pd.isna(match_p) and match_p > 0 else _normalize_vnd_price(ref_p)
                ref_norm = _normalize_vnd_price(ref_p) if not pd.isna(ref_p) and ref_p > 0 else actual_p

                if actual_p > 0:
                    result[t] = {
                        "actual_close": float(actual_p),
                        "ref_price": float(ref_norm),
                        "volume": float(vol) if not pd.isna(vol) else 0.0,
                    }

        return result

    def get_financial_statements(self, symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
        """Fetch all 3 quarterly financial statements + ratio table (4-8 quarters)."""
        statements: Dict[str, Optional[pd.DataFrame]] = {
            "income_statement": None,
            "balance_sheet": None,
            "cash_flow": None,
            "ratio": None,
        }

        for key, method_name in (
            ("income_statement", "income_statement"),
            ("balance_sheet", "balance_sheet"),
            ("cash_flow", "cash_flow"),
            ("ratio", "ratio"),
        ):
            def _fetch(source: str, m=method_name) -> pd.DataFrame:
                stock = self._get_stock(symbol, source)
                fn = getattr(stock.finance, m)
                return fn(period="quarter", lang="en")

            df, _ = self._call_with_fallback(f"{symbol}.{key}", _fetch)
            if isinstance(df, pd.DataFrame) and not df.empty:
                clean = df.copy()
                if isinstance(clean.columns, pd.MultiIndex):
                    clean.columns = [
                        "_".join(str(p).strip() for p in col if str(p).strip())
                        for col in clean.columns.values
                    ]
                statements[key] = clean.head(8)

        return statements

    def get_company_news_and_events(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch recent company news and corporate events/dividends from vnstock."""
        news_items: List[Dict[str, Any]] = []
        event_items: List[Dict[str, Any]] = []

        def _fetch_news(source: str):
            stock = self._get_stock(symbol, source)
            return stock.company.news()

        df_news, _ = self._call_with_fallback(f"{symbol}.company.news", _fetch_news)
        if isinstance(df_news, pd.DataFrame) and not df_news.empty:
            for _, row in df_news.head(8).iterrows():
                row_d = {str(k).lower(): v for k, v in row.to_dict().items()}
                title = str(row_d.get("news_title") or row_d.get("title") or "").strip()
                if title:
                    news_items.append({
                        "title": title,
                        "date": str(row_d.get("public_date") or row_d.get("publish_date") or ""),
                        "url": str(row_d.get("news_source_link") or row_d.get("url") or ""),
                        "source": "vnstock",
                        "summary": str(row_d.get("news_short_content") or row_d.get("summary") or ""),
                    })

        def _fetch_events(source: str):
            stock = self._get_stock(symbol, source)
            return stock.company.events()

        df_events, _ = self._call_with_fallback(f"{symbol}.company.events", _fetch_events)
        if isinstance(df_events, pd.DataFrame) and not df_events.empty:
            for _, row in df_events.head(6).iterrows():
                row_d = {str(k).lower(): v for k, v in row.to_dict().items()}
                event_items.append({
                    "title": str(row_d.get("event_title") or row_d.get("event_list_name") or row_d.get("event_desc") or ""),
                    "exright_date": str(row_d.get("exright_date") or ""),
                    "issue_date": str(row_d.get("issue_date") or ""),
                    "ratio": str(row_d.get("ratio") or ""),
                    "value": str(row_d.get("value") or ""),
                })

        return {"news": news_items, "events": event_items}
