"""
scanner/run_scanner.py
━━━━━━━━━━━━━━━━━━━━━━
Launch the live equity scanner with VWAP reversion (5-min) as the primary
strategy, plus the Hybrid Trend + Mean Reversion (15-min) as a secondary.

Two scanner instances run in parallel threads:
  1. VWAP scanner   — 5-min bars, polls every 300s, no confirmation delay.
     Designed for multiple trades per day on liquid large-caps.
  2. Hybrid scanner — 15-min bars, polls every 900s, 1-bar confirmation.
     Catches deeper pullbacks in uptrending stocks.

Run:
    python scanner/run_scanner.py
"""

import sys
import os
import threading

_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
sys.path.insert(0, _REPO)
sys.path.insert(0, _STRATEGY_IDE)

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_REPO, "strategy_ide", ".env"))

try:
    from strategy_ide.strategies.vwap_reversion import VWAPReversionStrategy
    from strategy_ide.strategies.hybrid_trend_mr import HybridTrendMRStrategy
except ImportError:
    from strategies.vwap_reversion import VWAPReversionStrategy
    from strategies.hybrid_trend_mr import HybridTrendMRStrategy

from scanner.screener import WATCHLIST_SP100, WATCHLIST_LARGE_CAP
from scanner.live_scanner import LiveScanner

# ── VWAP Reversion (primary — high frequency) ────────────────────────────────
# 5-min bars, relaxed thresholds, no confirmation delay.
# Targets: liquid large-caps where VWAP acts as a magnet.

VWAP_STRATEGY = VWAPReversionStrategy(
    rsi_window      = 10,
    buy_rsi         = 40,
    sell_rsi        = 55,
    vwap_dev_mult   = 1.5,
    min_bars_in_day = 12,     # skip first hour
    stop_loss_pct   = 0.005,  # 0.5% stop
    take_profit_pct = 0.01,   # 1.0% TP
)

vwap_scanner = LiveScanner(
    strategy        = VWAP_STRATEGY,
    watchlist       = WATCHLIST_LARGE_CAP,
    poll_interval   = 300,       # every 5 minutes
    screen_interval = 1800,      # re-scan every 30 min
    warmup_bars     = 80,        # ~6.5 hours of 5-min bars
    max_positions   = 3,
    risk_pct        = 0.002,     # 0.2% risk per trade (reduced from 1%)
    stop_loss_pct   = 0.005,
    take_profit_pct = 0.01,
    resolution      = "5",
    confirm_bars    = 1,         # act on first signal — no delay
)


# ── Hybrid Trend + MR (secondary — deeper pullbacks) ─────────────────────────
# 15-min bars, catches bigger dips in uptrending stocks.
# Parameters from cross-ticker hyperopt (see cross_ticker_analysis.md).

HYBRID_STRATEGY = HybridTrendMRStrategy(
    bb_window        = 20,     # tighter than old 40 — more entry opportunities
    bb_std           = 1.8,    # narrower bands — triggers more often
    rsi_window       = 14,
    buy_rsi          = 38,     # raised from 32 — less restrictive
    sell_rsi         = 60,
    exit_target      = "mid",  # exit at midband (faster exits, more turnover)
    min_hold_bars    = 2,
    stop_loss_pct    = 0.02,   # 2% stop
    take_profit_pct  = 0.035,  # 3.5% TP
    trend_sma_window = 50,     # 50-day SMA instead of 200 — computable with available data
    trend_buffer_pct = 0.005,  # tighter buffer
)

hybrid_scanner = LiveScanner(
    strategy        = HYBRID_STRATEGY,
    watchlist       = WATCHLIST_SP100,
    poll_interval   = 900,       # every 15 min
    screen_interval = 1800,
    warmup_bars     = 120,       # ~30 hours of 15-min bars
    max_positions   = 3,
    risk_pct        = 0.002,     # 0.2% risk per trade (reduced from 1%)
    stop_loss_pct   = 0.02,
    take_profit_pct = 0.035,     # 3.5% TP
    resolution      = "15",
    confirm_bars    = 1,         # reduced from 2 — signals are fleeting
)


if __name__ == "__main__":
    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
    print()
    if paper:
        print("  PAPER TRADING — simulated account, no real money")
    else:
        print("  LIVE TRADING — real money, real orders")
    print()

    # ── Launch both scanners ──────────────────────────────────────────────────
    # Equity monitor now runs as its own service (run_equity_monitor.py / issue #10)
    # VWAP runs in the main thread, Hybrid runs in a daemon thread.
    hybrid_thread = threading.Thread(
        target=hybrid_scanner.run,
        daemon=True,
        name="hybrid-scanner",
    )
    hybrid_thread.start()
    print("  Hybrid (15-min) scanner started in background thread")
    print("  VWAP (5-min) scanner starting in main thread...\n")

    try:
        vwap_scanner.run()   # blocks here until Ctrl-C
    except KeyboardInterrupt:
        print("\nInterrupted — shutting down")
