"""
scanner/run_equity_monitor.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Standalone equity monitor — runs independently of any scanner.

Previously the EquityMonitor only ran as a thread inside run_scanner.py,
meaning equity logging stopped whenever the equity scanner stopped (market
close, crash, weekends).  This entry point keeps it alive 24/7 as its own
service managed by run_all.py, so equity_logs/ never has gaps.

See issue #10.
"""

import os
import sys
import time
import logging
from pathlib import Path

_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_STRATEGY_IDE, ".env"))

from alpaca.trading.client import TradingClient
from strategy_ide.monitor.equity_monitor import EquityMonitor

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")


def on_daily_loss(equity: float, loss_pct: float) -> None:
    log.critical(
        "DAILY LOSS LIMIT — equity=$%.2f  loss=%.1f%%",
        equity, loss_pct * 100,
    )


def on_drawdown(equity: float, dd_pct: float) -> None:
    log.critical(
        "MAX DRAWDOWN — equity=$%.2f  drawdown=%.1f%%",
        equity, dd_pct * 100,
    )


def main():
    mode = "PAPER" if PAPER else "LIVE"
    log.info(f"Standalone EquityMonitor [{mode}]  polling every 60s")

    client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

    monitor = EquityMonitor(
        alpaca_client        = client,
        poll_interval        = 60,
        daily_loss_limit_pct = 0.03,
        max_drawdown_pct     = 0.06,
        on_daily_loss_breach = on_daily_loss,
        on_drawdown_breach   = on_drawdown,
        log_dir              = Path(_REPO) / "equity_logs",
    )
    monitor.start()

    # Block forever — run_all.py manages lifecycle via SIGTERM
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
