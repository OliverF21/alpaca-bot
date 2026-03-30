"""
scanner/run_scanner.py
━━━━━━━━━━━━━━━━━━━━━━
Launch the 15-minute live scanner using the Hybrid Trend + Mean Reversion strategy.

The hybrid strategy combines two filters:
  1. Regime filter   — only go long when the stock is above its 200-day SMA
                       (avoids entering dips in a downtrend / bear market)
  2. Mean reversion  — enter on BB lower-band + RSI oversold, exit at upper band

warmup_bars=5200 supplies 200 trading days of 15-min history so the strategy
can compute a valid 200-day SMA on startup.

Run:
    python scanner/run_scanner.py
"""

import sys
import os
_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
sys.path.insert(0, _REPO)
sys.path.insert(0, _STRATEGY_IDE)  # so strategy_ide.strategies.* can use "from strategies.base_strategy"

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_REPO, "strategy_ide", ".env"))  # strategy_ide .env has Alpaca keys

# Strategy lives in strategy_ide when run from repo root
try:
    from strategy_ide.strategies.hybrid_trend_mr import HybridTrendMRStrategy
except ImportError:
    from strategies.hybrid_trend_mr import HybridTrendMRStrategy
from scanner.screener import WATCHLIST_SP100, WATCHLIST_SECTOR_ETFS, WATCHLIST_LARGE_CAP
from scanner.live_scanner import LiveScanner
from strategy_ide.monitor.equity_monitor import EquityMonitor
from pathlib import Path

# ── Strategy ──────────────────────────────────────────────────────────────────
# Hybrid: 200-day SMA regime filter + 15-min Bollinger Band / RSI mean reversion.
# Only enters long when the stock is above its 200-day SMA (uptrend confirmed).
#
# Parameters updated 2026-03-20 based on 300-trial cross-ticker hyperopt analysis
# (see cross_ticker_analysis.md). Consensus params from tickers with positive
# out-of-sample Sharpe: META (gap 0.019), MSFT (gap 0.038), JPM (gap 0.418),
# TSLA (gap 0.229), NVDA (gap -0.566).
#
# Default backtest results (2022-2025, 15-min bars, updated params):
#   META:  Sharpe 0.914  +38%  MaxDD -10%   B&H +96%
#   JPM:   Sharpe 1.144  +32%  MaxDD  -8%   B&H +122%
#   MSFT:  Sharpe 0.868  +16%  MaxDD  -3%   B&H +49%
#   AMZN:  Sharpe 1.316  +46%  MaxDD  -7%   B&H +38%  ← beats B&H
#   TSLA:  Sharpe -0.226 -12%  MaxDD -24%   (high-risk, lower priority)
#   NVDA:  Sharpe 0.775  +40%  MaxDD -21%   B&H +513% ← trend play, not MR
#
# Excluded (negative OOS Sharpe in hyperopt): SPY, QQQ, AAPL, GOOGL

STRATEGY = HybridTrendMRStrategy(
    bb_window        = 40,    # wider: fewer false signals vs old 20
    bb_std           = 2.1,   # slightly wider bands
    rsi_window       = 18,    # smoother RSI vs old 14
    buy_rsi          = 32,    # unchanged — solid across tickers
    sell_rsi         = 62,    # tighter exit vs old 65
    exit_target      = "upper",
    min_hold_bars    = 2,
    stop_loss_pct    = 0.033, # wider stop 3.3% vs old 1.5%
    take_profit_pct  = 0.055, # wider TP 5.5% vs old 4%
    trend_sma_window = 200,
    trend_buffer_pct = 0.01,
)

# ── Watchlist ─────────────────────────────────────────────────────────────────
# Focused on tickers where mean reversion shows real out-of-sample edge.
# Priority tier: META, JPM, MSFT — lowest IS/OOS Sharpe degradation in hyperopt.
# Secondary tier: AMZN, TSLA, NVDA — positive OOS Sharpe but more degradation.
# Excluded: SPY, QQQ, AAPL, GOOGL — consistently negative/near-zero OOS Sharpe.
# Still includes full WATCHLIST_SP100 as the universe for the screener scan,
# but the priority tickers are front-loaded for faster signal detection.

PRIORITY   = ["META", "JPM", "MSFT"]
SECONDARY  = ["AMZN", "TSLA", "NVDA"]
WATCHLIST  = PRIORITY + SECONDARY + [
    s for s in WATCHLIST_SP100 if s not in PRIORITY + SECONDARY
]

# ── Scanner ───────────────────────────────────────────────────────────────────

scanner = LiveScanner(
    strategy        = STRATEGY,
    watchlist       = WATCHLIST,
    screen_interval = 900,      # re-scan every 15 minutes
    warmup_bars     = 5200,     # 200 trading days × 26 bars/day — needed for 200-SMA
    max_positions   = 5,
    risk_pct        = 0.01,     # 1% risk per trade (down from 2% — safer)
    stop_loss_pct   = 0.033,
    take_profit_pct = 0.055,
)

if __name__ == "__main__":
    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
    print()
    if paper:
        print("  PAPER TRADING — simulated account, no real money")
    else:
        print("  LIVE TRADING — real money, real orders")
    print()

    # ── Equity monitor ────────────────────────────────────────────────────────
    # Runs as a background thread.  Every 60 seconds it polls Alpaca for
    # account equity and fires alarms if risk limits are breached.
    #
    # Alarm thresholds (edit to taste):
    #   daily_loss_limit_pct = 0.03  → alert if today's loss > 3% of opening equity
    #   max_drawdown_pct     = 0.06  → alert if equity is > 6% below its peak
    #
    # CSV logs are written to equity_logs/equity_log_YYYYMMDD.csv.
    # Open them in a spreadsheet or the Streamlit dashboard to see your equity curve.

    def on_daily_loss(equity: float, loss_pct: float) -> None:
        print(f"\n  DAILY LOSS LIMIT HIT — equity=${equity:,.2f}  loss={loss_pct*100:.1f}%")
        print("  Consider stopping the scanner manually.\n")

    def on_drawdown(equity: float, dd_pct: float) -> None:
        print(f"\n  MAX DRAWDOWN HIT — equity=${equity:,.2f}  drawdown={dd_pct*100:.1f}%\n")

    monitor = EquityMonitor(
        alpaca_client        = scanner._trader,   # reuse scanner's Alpaca client
        poll_interval        = 60,
        daily_loss_limit_pct = 0.03,
        max_drawdown_pct     = 0.06,
        on_daily_loss_breach = on_daily_loss,
        on_drawdown_breach   = on_drawdown,
        log_dir              = Path("equity_logs"),
    )
    monitor.start()

    scanner.run()   # blocks here until Ctrl-C

    monitor.stop()
