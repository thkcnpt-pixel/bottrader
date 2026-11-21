"""CLI runner to backtest VN Stock Swing Bot strategies (Pullback & Breakout) on historical OHLCV."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.backtester import backtest_portfolio_universe  # noqa: E402
from utils.config import LOG_LEVEL  # noqa: E402
from utils.vnstock_client import VnstockClient  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest VN Stock Swing setups (Pullback & Breakout).")
    parser.add_argument(
        "--tickers",
        default="FPT,HPG,SSI,MBB,ACB,VCB,MWG,CTR,DGC,PNJ",
        help="Comma-separated list of tickers to backtest.",
    )
    parser.add_argument("--days", type=int, default=750, help="Lookback calendar days (default: 750).")
    parser.add_argument("--output-json", default="", help="Optional path to save JSON backtest report.")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    vn = VnstockClient()
    ohlcv_map = {}

    for sym in tickers:
        logger.info("Fetching %d days OHLCV for %s...", args.days, sym)
        df = vn.get_ohlcv(sym, days=args.days)
        if df is not None and len(df) >= 60:
            ohlcv_map[sym] = df
        else:
            logger.warning("Insufficient OHLCV data for %s — skipping.", sym)

    report = backtest_portfolio_universe(ohlcv_map)
    overall = report["overall"]

    print("\n================ BACKTEST SUMMARY (T+2.5 | Pullback & Breakout) ================")
    print(f"Tickers tested      : {len(ohlcv_map)} ({', '.join(sorted(ohlcv_map.keys()))})")
    print(f"Total trades        : {overall['total_trades']}")
    print(f"Win rate            : {overall['win_rate_pct']:.2f}%")
    print(f"Expectancy / trade  : {overall['expectancy_pct']:+.2f}%")
    print(f"Profit factor       : {overall['profit_factor']:.2f}")
    print(f"Max drawdown        : {overall['max_drawdown_pct']:.2f}%")
    print(f"Avg holding days    : {overall['avg_holding_days']:.1f} sessions")
    print("--------------------------------------------------------------------------------")
    for sig_type, stats in overall.get("by_signal_type", {}).items():
        print(
            f"  [{sig_type:<8}] Trades={stats['count']:<3} | "
            f"WinRate={stats['win_rate_pct']:.1f}% | Expectancy={stats['expectancy_pct']:+.2f}%"
        )
    print("================================================================================\n")

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Saved backtest report to %s", args.output_json)


if __name__ == "__main__":
    main()
