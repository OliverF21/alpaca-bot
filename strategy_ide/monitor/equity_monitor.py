"""
monitor/equity_monitor.py
━━━━━━━━━━━━━━━━━━━━━━━━━
Continuous equity monitor — runs as a background thread alongside the scanner.

What this does (beginner explanation):
  Think of this as a security camera for your brokerage account.  Every
  `poll_interval` seconds (default: 60) it asks Alpaca "what is my account
  worth right now?" and records the answer.  If the answer ever crosses one
  of four danger thresholds it logs a loud warning so you know something is
  wrong.

  It does NOT place or cancel any orders — it is purely observational.

The four alarms:

  1. Daily loss limit   — if today's equity has fallen more than X% below
                          where it started this morning, warn.  Default: 3%.

  2. Max drawdown       — if equity has fallen more than X% below its
                          all-time peak (since the monitor started), warn.
                          Default: 6%.

  3. Equity spike       — if a single poll shows equity changing by more
                          than X% versus the previous poll, warn.  This can
                          indicate bad data from Alpaca.  Default: 5%.

  4. Stale data         — if no successful API call has come back in more
                          than N seconds, log an error.  Default: 300s (5 min).

Usage:
    from strategy_ide.monitor.equity_monitor import EquityMonitor
    from alpaca.trading.client import TradingClient

    client  = TradingClient(api_key, secret_key, paper=True)
    monitor = EquityMonitor(
        alpaca_client        = client,
        poll_interval        = 60,
        daily_loss_limit_pct = 0.03,
        max_drawdown_pct     = 0.06,
        log_dir              = Path("equity_logs"),
    )
    monitor.start()          # starts a background daemon thread
    # ... run your scanner ...
    monitor.stop()           # graceful shutdown
"""

import csv
import logging
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


class EquityMonitor:
    """
    Polls Alpaca every `poll_interval` seconds for account equity and
    triggers alerts when configured thresholds are breached.

    Parameters
    ----------
    alpaca_client : TradingClient
        An already-authenticated Alpaca TradingClient instance.
        The monitor reuses this client so no extra API credentials are needed.
    poll_interval : int
        Seconds between equity polls.  Default: 60.
    daily_loss_limit_pct : float
        Alert when equity drops this fraction below start-of-day equity.
        Example: 0.03 = alert when today's loss exceeds 3% of opening equity.
    max_drawdown_pct : float
        Alert when equity is this fraction below its peak since monitoring began.
        Example: 0.06 = alert at 6% drawdown from the highest equity seen.
    spike_pct : float
        Alert when a single poll shows equity changing by more than this fraction.
        Example: 0.05 = alert if equity jumps or drops more than 5% in one poll.
    stale_seconds : int
        Log an error if no successful Alpaca poll has occurred in this many seconds.
    on_daily_loss_breach : Optional[Callable]
        Optional function called when the daily loss limit is breached.
        Signature: fn(current_equity: float, loss_pct: float) -> None
        Use this to automatically halt the scanner, send a text, etc.
    on_drawdown_breach : Optional[Callable]
        Optional function called when the max drawdown limit is breached.
        Signature: fn(current_equity: float, drawdown_pct: float) -> None
    log_dir : Optional[Path]
        Directory to write daily CSV files (equity_log_YYYYMMDD.csv).
        Each row: timestamp, equity.
        Set to None to skip file logging.
    """

    def __init__(
        self,
        alpaca_client,
        poll_interval: int              = 60,
        daily_loss_limit_pct: float     = 0.03,
        max_drawdown_pct: float         = 0.06,
        spike_pct: float                = 0.05,
        stale_seconds: int              = 300,
        on_daily_loss_breach: Optional[Callable] = None,
        on_drawdown_breach:   Optional[Callable] = None,
        log_dir: Optional[Path]         = None,
    ):
        self._client               = alpaca_client
        self.poll_interval         = poll_interval
        self.daily_loss_limit_pct  = daily_loss_limit_pct
        self.max_drawdown_pct      = max_drawdown_pct
        self.spike_pct             = spike_pct
        self.stale_seconds         = stale_seconds
        self.on_daily_loss_breach  = on_daily_loss_breach
        self.on_drawdown_breach    = on_drawdown_breach
        self.log_dir               = Path(log_dir) if log_dir else None

        # Internal state ──────────────────────────────────────────────────────
        # _equity_log: list of (datetime, equity) tuples — full history
        self._equity_log: list         = []
        self._start_of_day_equity: Optional[float] = None
        self._peak_equity: Optional[float]         = None
        self._current_date: Optional[date]         = None
        self._prev_equity: Optional[float]         = None
        self._last_successful_poll: Optional[datetime] = None

        # Thread control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # CSV file handles (one per day)
        self._csv_file   = None
        self._csv_writer = None

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background monitoring thread.  Safe to call once."""
        if self._thread and self._thread.is_alive():
            log.warning("EquityMonitor is already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,        # daemon=True means it dies automatically when the
            name="equity_monitor"  # main program exits — no manual cleanup needed
        )
        self._thread.start()
        log.info("EquityMonitor started (poll every %ds)", self.poll_interval)

    def stop(self) -> None:
        """Signal the thread to stop and wait up to 10 seconds for it to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._csv_file:
            self._csv_file.close()
        log.info("EquityMonitor stopped")

    def current_equity(self) -> Optional[float]:
        """Return the most recently polled equity value, or None if not yet started."""
        return self._equity_log[-1][1] if self._equity_log else None

    def daily_loss_pct(self) -> Optional[float]:
        """
        Return today's loss as a fraction (0.0 to 1.0, where 1.0 = 100% loss).
        Positive means a loss; negative means a gain vs. this morning's open.
        Returns None if no data yet.
        """
        if self._start_of_day_equity and self._equity_log:
            curr = self._equity_log[-1][1]
            return (self._start_of_day_equity - curr) / self._start_of_day_equity
        return None

    def drawdown_pct(self) -> Optional[float]:
        """
        Return the drawdown from peak as a fraction (0.0 to 1.0).
        0.0 means equity is at its all-time high since the monitor started.
        Returns None if no data yet.
        """
        if self._peak_equity and self._equity_log:
            curr = self._equity_log[-1][1]
            return (self._peak_equity - curr) / self._peak_equity
        return None

    def save_csv(self, path: str) -> None:
        """Write the full in-memory equity log to a CSV file at `path`."""
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity"])
            for ts, eq in self._equity_log:
                writer.writerow([ts.isoformat(), round(eq, 2)])

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _poll_equity(self) -> Optional[float]:
        """
        Ask Alpaca for the current account equity.
        Returns None if the API call fails (network error, auth error, etc.).
        """
        try:
            account = self._client.get_account()
            equity  = float(account.equity)
            self._last_successful_poll = datetime.now()
            return equity
        except Exception as e:
            log.warning("EquityMonitor: Alpaca poll failed — %s", e)
            return None

    def _open_csv_for_today(self, today: date) -> None:
        """Open (or append to) the daily CSV log file."""
        if self._csv_file:
            self._csv_file.close()
        filename = self.log_dir / f"equity_log_{today.strftime('%Y%m%d')}.csv"
        self._csv_file   = open(filename, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        # Write header only if the file is brand new (size == 0)
        if filename.stat().st_size == 0:
            self._csv_writer.writerow(["timestamp", "equity"])

    def _check_alarms(self, equity: float) -> None:
        """
        Run all four alarm checks against the latest equity value.
        Logs CRITICAL for hard limits (daily loss, drawdown) and WARNING
        for soft signals (spike, stale data).
        """
        now   = datetime.now()
        today = date.today()

        # ── Reset daily anchor on a new trading day ────────────────────────
        if self._current_date != today:
            self._current_date        = today
            self._start_of_day_equity = equity
            log.info(
                "EquityMonitor: new day %s — start-of-day equity $%.2f",
                today, equity,
            )
            if self.log_dir:
                self._open_csv_for_today(today)

        # ── Update peak (highest equity seen since monitor started) ────────
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity

        # ── Alarm 1: daily loss limit ──────────────────────────────────────
        if self._start_of_day_equity and self._start_of_day_equity > 0:
            daily_loss = (self._start_of_day_equity - equity) / self._start_of_day_equity
            if daily_loss >= self.daily_loss_limit_pct:
                log.critical(
                    "DAILY LOSS LIMIT BREACHED  equity=$%.2f  loss=%.2f%%  limit=%.2f%%",
                    equity, daily_loss * 100, self.daily_loss_limit_pct * 100,
                )
                if self.on_daily_loss_breach:
                    try:
                        self.on_daily_loss_breach(equity, daily_loss)
                    except Exception as e:
                        log.error("on_daily_loss_breach callback raised: %s", e)

        # ── Alarm 2: max drawdown from peak ───────────────────────────────
        if self._peak_equity and self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
            if dd >= self.max_drawdown_pct:
                log.critical(
                    "MAX DRAWDOWN BREACHED  equity=$%.2f  drawdown=%.2f%%  peak=$%.2f",
                    equity, dd * 100, self._peak_equity,
                )
                if self.on_drawdown_breach:
                    try:
                        self.on_drawdown_breach(equity, dd)
                    except Exception as e:
                        log.error("on_drawdown_breach callback raised: %s", e)

        # ── Alarm 3: equity spike (possible data error) ────────────────────
        if self._prev_equity is not None and self._prev_equity > 0:
            change = abs(equity - self._prev_equity) / self._prev_equity
            if change >= self.spike_pct:
                log.warning(
                    "EQUITY SPIKE  $%.2f → $%.2f  change=%.2f%%",
                    self._prev_equity, equity, change * 100,
                )

        # ── Alarm 4: stale data ────────────────────────────────────────────
        if self._last_successful_poll is not None:
            stale_secs = (now - self._last_successful_poll).total_seconds()
            if stale_secs > self.stale_seconds:
                log.error(
                    "STALE DATA  no successful Alpaca poll in %.0fs (limit=%ds)",
                    stale_secs, self.stale_seconds,
                )

        self._prev_equity = equity

    def _run(self) -> None:
        """
        Main polling loop.  Runs in the background thread until stop() is called.

        Every `poll_interval` seconds:
          1. Ask Alpaca for current equity
          2. Append to the in-memory log
          3. Run alarm checks
          4. Write to today's CSV (if log_dir is set)
        """
        log.info("EquityMonitor: polling loop started")
        while not self._stop_event.is_set():
            equity = self._poll_equity()
            if equity is not None:
                now = datetime.now()
                self._equity_log.append((now, equity))
                self._check_alarms(equity)
                if self._csv_writer:
                    self._csv_writer.writerow([now.isoformat(), round(equity, 2)])
                    self._csv_file.flush()
                log.debug("EquityMonitor: equity=$%.2f", equity)

            # _stop_event.wait() acts like time.sleep() but wakes up immediately
            # when stop() is called, so shutdown is fast.
            self._stop_event.wait(timeout=self.poll_interval)

        log.info("EquityMonitor: polling loop stopped")
