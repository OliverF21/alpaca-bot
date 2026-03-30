"""
scanner/run_crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAX/USD paper trading — Crypto Trend Following (EMA 20/48 + ADX + ATR stop).

Runs as a completely separate process from the equity scanner (24/7 vs NYSE hours):

    Terminal 1:  python scanner/run_scanner.py         ← equity (NYSE hours)
    Terminal 2:  python scanner/run_crypto_scanner.py  ← AVAX/USD (24/7)

Backtest results (500-trial hyperopt, hourly bars 2021-2024, 70/30 OOS split):
  OOS Sharpe  = 0.597   |  IS Sharpe   = 0.631
  OOS return  = +52.4%  |  vs B&H      = +36.5%  (B&H OOS = +15.9%)
  Overfit gap = 0.034   ← near-zero: IS ≈ OOS — genuine edge, not curve-fit
  OOS trades  = 13      |  Win rate    = 61.5%

Why AVAX?
  AVAX had a moderate B&H OOS baseline (+15.9%) — not a parabolic bull run.
  The EMA crossover + ADX filter captures multi-week directional trends while
  ATR-based stops (4.27× ATR) protect against the -65% bear-market crashes
  that made B&H painful over the full 2021-2024 cycle.

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
except ImportError:
    from strategies.crypto_trend_following import CryptoTrendFollowingStrategy

from scanner.crypto_screener import CRYPTO_WATCHLIST
from scanner.crypto_scanner import CryptoScanner
from strategy_ide.monitor.equity_monitor import EquityMonitor
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Strategy — best 500-trial hyperopt params ──────────────────────────────────
# Optimised against AVAX/USD hourly bars, 2021-2024, 70/30 OOS split.
# Overfit gap = 0.034 (IS Sharpe 0.631 ≈ OOS Sharpe 0.597) — verified robust.

STRATEGY = CryptoTrendFollowingStrategy(
    fast_ema        = 20,     # fast EMA — crossover trigger
    slow_ema        = 48,     # slow EMA — trend baseline
    adx_threshold   = 15.2,  # min ADX to confirm trend strength
    atr_stop_mult   = 4.27,  # ATR multiplier for dynamic stop (wide for crypto)
    stop_loss_pct   = 0.069, # 6.9% fallback stop when ATR unavailable
    take_profit_pct = 0.181, # 18.1% take-profit target
)

# ── Watchlist ─────────────────────────────────────────────────────────────────
# AVAX/USD is the primary (and validated) target.
# Other pairs kept as background screener candidates.
AVAX_PRIMARY = "AVAX/USD"
WATCHLIST    = [AVAX_PRIMARY] + [p for p in CRYPTO_WATCHLIST if p != AVAX_PRIMARY]

# ── Scanner ───────────────────────────────────────────────────────────────────
scanner = CryptoScanner(
    strategy        = STRATEGY,
    watchlist       = WATCHLIST,
    poll_interval   = 3600,   # re-evaluate every hour (matches 1h bar resolution)
    screen_interval = 7200,   # re-scan universe every 2 hours
    warmup_bars     = 250,    # 250 × 1h bars — enough for EMA(48) + ADX(14) warmup
    max_positions   = 1,      # AVAX-focused: single position for clean tracking
    risk_pct        = 0.01,   # 1% equity per trade
    stop_loss_pct   = 0.069,  # must match strategy fallback stop
    take_profit_pct = 0.181,  # must match strategy TP
)

if __name__ == "__main__":
    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

    print()
    print("━" * 60)
    if paper:
        print("  MODE     : PAPER TRADING (simulated — no real money)")
    else:
        print("  MODE     : ⚠️  LIVE TRADING  (real money, real orders)")
    print(f"  Symbol   : {AVAX_PRIMARY}  (primary target)")
    print(f"  Strategy : EMA({STRATEGY.fast_ema}/{STRATEGY.slow_ema})"
          f" + ADX({STRATEGY.adx_threshold:.1f})"
          f" + ATR×{STRATEGY.atr_stop_mult:.2f} stop")
    print(f"  Stop     : ATR×{STRATEGY.atr_stop_mult:.2f} ({STRATEGY.stop_loss_pct*100:.1f}% fallback)")
    print(f"  TP       : {STRATEGY.take_profit_pct*100:.1f}%")
    print(f"  Risk     : 1% of equity per trade")
    print(f"  Backtest : OOS Sharpe=0.597  +52.4%  beats B&H by +36.5%")
    print(f"  Poll     : every 3600s (1 hour, 24/7)")
    print("━" * 60)
    print()

    # ── Ensure AVAX/USD is always active regardless of MR screener output ──────
    # The CryptoScanner screener is built for mean-reversion screening (BB/RSI
    # oversold). For trend following we don't need a screener — we always watch
    # AVAX. Patch the screener so it always includes AVAX/USD.
    _original_scan = scanner._screener.scan
    scanner._screener.scan = lambda: ([{"symbol": AVAX_PRIMARY}]
                                      + [c for c in _original_scan()
                                         if c["symbol"] != AVAX_PRIMARY])

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
        daily_loss_limit_pct = 0.05,   # alert at 5% daily loss
        max_drawdown_pct     = 0.15,   # alert at 15% drawdown from peak
        on_daily_loss_breach = on_daily_loss,
        on_drawdown_breach   = on_drawdown,
        log_dir              = Path("equity_logs"),
    )
    monitor.start()
    log.info("Equity monitor started")

    try:
        scanner.run()   # blocks here until Ctrl-C
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    finally:
        monitor.stop()
