"""
scanner/run_crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multi-strategy crypto scanner entry point.

Runs 4 strategies in parallel across a dynamically-ranked universe of crypto
pairs. The Signal Arbitrator picks the best trades by conviction score.

Run:
    python scanner/run_crypto_scanner.py
"""

import sys
import os
import logging

_REPO         = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
sys.path.insert(0, _REPO)
sys.path.insert(0, _STRATEGY_IDE)

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_REPO, "strategy_ide", ".env"))

try:
    from strategy_ide.strategies.crypto_trend_following import CryptoTrendFollowingStrategy
    from strategy_ide.strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
    from strategy_ide.strategies.crypto_breakout import CryptoBreakoutStrategy
    from strategy_ide.strategies.crypto_supertrend import CryptoSupertrendStrategy
except ImportError:
    from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
    from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
    from strategies.crypto_breakout import CryptoBreakoutStrategy
    from strategies.crypto_supertrend import CryptoSupertrendStrategy

from scanner.crypto_scanner import CryptoScanner
from strategy_ide.monitor.equity_monitor import EquityMonitor
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Strategies (reworked with loosened thresholds + conviction scoring) ────────

STRATEGIES = [
    CryptoTrendFollowingStrategy(
        fast_ema        = 12,
        slow_ema        = 26,
        adx_threshold   = 12.0,
        atr_stop_mult   = 4.27,
        stop_loss_pct   = 0.069,
        take_profit_pct = 0.181,
    ),
    CryptoMeanReversionStrategy(
        bb_window       = 20,
        bb_std          = 2.0,
        buy_rsi         = 33,
        sell_rsi        = 68,
        atr_stop_mult   = 2.5,
        stop_loss_pct   = 0.04,
        take_profit_pct = 0.08,
    ),
    CryptoBreakoutStrategy(
        channel_window  = 18,
        vol_mult        = 1.25,
        atr_stop_mult   = 2.0,
        stop_loss_pct   = 0.05,
        take_profit_pct = 0.12,
    ),
    CryptoSupertrendStrategy(
        multiplier      = 2.5,
        vol_filter      = False,
        rsi_min         = 40.0,
        stop_loss_pct   = 0.05,
        take_profit_pct = 0.20,
    ),
]

# ── Scanner ───────────────────────────────────────────────────────────────────

scanner = CryptoScanner(
    strategies       = STRATEGIES,
    poll_interval    = 3600,    # 1 hour
    warmup_bars      = 250,
    universe_refresh = 1800,   # 30 minutes
    universe_top_k   = 8,
)


if __name__ == "__main__":
    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
    strat_names = [s.name for s in STRATEGIES]

    print()
    print("━" * 65)
    if paper:
        print("  MODE       : PAPER TRADING (simulated — no real money)")
    else:
        print("  MODE       : ⚠️  LIVE TRADING  (real money, real orders)")
    print(f"  Strategies : {strat_names}")
    print(f"  Universe   : dynamic top-8 by volatility + volume")
    print(f"  Refresh    : every 30 min")
    print(f"  Poll       : every 3600s (1 hour, 24/7)")
    print(f"  Risk       : 1% standard / 2% high-conviction")
    print(f"  Max pos    : dynamic (equity / $5K, max 6)")
    print("━" * 65)
    print()

    # ── Equity monitor ────────────────────────────────────────────────────────
    def on_daily_loss(equity: float, loss_pct: float) -> None:
        log.warning(
            f"DAILY LOSS ALERT  equity=${equity:,.2f}  loss={loss_pct*100:.1f}%"
        )

    def on_drawdown(equity: float, dd_pct: float) -> None:
        log.warning(
            f"DRAWDOWN ALERT  equity=${equity:,.2f}  drawdown={dd_pct*100:.1f}%"
        )

    monitor = EquityMonitor(
        alpaca_client        = scanner._trader,
        poll_interval        = 60,
        daily_loss_limit_pct = 0.05,
        max_drawdown_pct     = 0.15,
        on_daily_loss_breach = on_daily_loss,
        on_drawdown_breach   = on_drawdown,
        log_dir              = Path("equity_logs"),
    )
    monitor.start()
    log.info("Equity monitor started")

    try:
        scanner.run()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    finally:
        monitor.stop()
