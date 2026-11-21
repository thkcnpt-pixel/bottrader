"""Phase 3 — Market Data Provider Protocol & Multi-Source Health Registry.

Decouples business logic from raw scraping endpoints and tracks per-provider
success rate, failure count, and average latency across VCI, VNDirect, TCBS,
Vietcap IQ, and Simplize.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Protocol, TypeVar
import pandas as pd

T = TypeVar("T")


class MarketDataProvider(Protocol):
    """Standard interface for any Vietnamese market data provider implementation."""

    def get_listed_symbols(self) -> List[Dict[str, str]]:
        ...

    def get_ohlcv(self, symbol: str, days: int = 160, is_index: bool = False) -> Optional[pd.DataFrame]:
        ...

    def get_actual_prices_batch(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        ...

    def get_financial_statements(self, symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
        ...

    def get_company_news_and_events(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        ...


class ProviderHealthRegistry:
    """Tracks call latency, success count, and failure count for each upstream data source."""

    def __init__(self) -> None:
        self._stats: Dict[str, Dict[str, Any]] = {}

    def record_call(self, source: str, success: bool, elapsed_ms: float, error: str = "") -> None:
        key = source.upper().strip()
        entry = self._stats.setdefault(
            key,
            {
                "calls": 0,
                "success": 0,
                "failure": 0,
                "total_ms": 0.0,
                "last_error": "",
            },
        )
        entry["calls"] += 1
        entry["total_ms"] += max(0.0, float(elapsed_ms))
        if success:
            entry["success"] += 1
        else:
            entry["failure"] += 1
            if error:
                entry["last_error"] = str(error)[:200]

    def track(self, source: str, fn: Callable[[], T]) -> T:
        """Execute `fn()` while recording latency and success/failure in the registry."""
        t0 = time.perf_counter()
        try:
            result = fn()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.record_call(source, success=True, elapsed_ms=elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.record_call(source, success=False, elapsed_ms=elapsed_ms, error=str(exc))
            raise

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return summary dictionary suitable for logging or Firestore heartbeat metrics."""
        out: Dict[str, Dict[str, Any]] = {}
        for src, s in sorted(self._stats.items()):
            calls = int(s["calls"])
            succ = int(s["success"])
            avg_ms = (s["total_ms"] / calls) if calls > 0 else 0.0
            out[src] = {
                "calls": calls,
                "success": succ,
                "failure": int(s["failure"]),
                "success_rate_pct": round((succ / calls * 100.0) if calls > 0 else 0.0, 1),
                "avg_latency_ms": round(avg_ms, 1),
                "last_error": s["last_error"],
            }
        return out


GLOBAL_PROVIDER_HEALTH = ProviderHealthRegistry()
