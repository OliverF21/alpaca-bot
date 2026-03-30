"""
scanner/live_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━
15-minute polling scanner.

Data source:  Alpaca StockHistoricalDataClient (IEX free tier) with yfinance fallback
Order execution: Alpaca Trading API

Flow:
  - Every poll_interval seconds, fetch the latest 15m bars for all
    active candidates via Alpaca REST
  - Evaluate signals on the fresh data
  - Submit orders via Alpaca Trading API
  - A background thread re-runs the screener every screen_interval seconds
"""

import datetime
import logging
import os
import threading
import time
from typing import Dict, List

import pytz

import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest

# Strategy and data when run from repo root (run_scanner adds strategy_ide to path)
try:
    from strategy_ide.strategies.base_strategy import BaseStrategy
    from strategy_ide.data.fetcher import fetch_bars, fetch_bars_bulk
except ImportError:
    from strategies.base_strategy import BaseStrategy
    from data.fetcher import fetch_bars, fetch_bars_bulk
from scanner.screener import MeanReversionScreener, WATCHLIST_SP100

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


class LiveScanner:
    """
    Polling-based 15-minute scanner.
    Market data: Finnhub REST API
    Order execution: Alpaca Trading API

    Parameters
    ----------
    strategy : BaseStrategy
        Strategy instance tuned for 15m bars.
    watchlist : list[str]
        Broad universe for the screener.
    poll_interval : int
        Seconds between signal checks. Default: 900 (15 min).
    screen_interval : int
        Seconds between full watchlist re-scans. Default: 1800 (30 min).
        Runs less often than polling since it makes ~90 API calls.
    warmup_bars : int
        Historical bars to pre-load per symbol.
    max_positions : int
        Max concurrent open positions.
    risk_pct : float
        Fraction of equity risked per trade.
    stop_loss_pct : float
        Hard stop below entry.
    take_profit_pct : float
        Bracket take-profit above entry.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        watchlist: List[str]      = WATCHLIST_SP100,
        poll_interval: int        = 900,
        screen_interval: int      = 1800,
        warmup_bars: int          = 60,
        max_positions: int        = 5,
        risk_pct: float           = 0.02,
        stop_loss_pct: float      = 0.015,
        take_profit_pct: float    = 0.03,
    ):
        self.strategy        = strategy
        self.watchlist       = watchlist
        self.poll_interval   = poll_interval
        self.screen_interval = screen_interval
        self.warmup_bars     = warmup_bars
        self.max_positions   = max_positions
        self.risk_pct        = risk_pct
        self.stop_loss_pct   = stop_loss_pct
        self.take_profit_pct = take_profit_pct

        self._cache: Dict[str, pd.DataFrame]   = {}
        self._active_symbols: List[str]        = []
        self._entry_confirmation: Dict[str, int] = {}   # symbol → consecutive entry bars
        self._position_entry_prices: Dict[str, float] = {}  # symbol → entry price
        self._daily_pnl: Dict[str, float] = {}          # date string → realized PnL

        # Alpaca for orders + positions only
        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

        self._screener = MeanReversionScreener(
            watchlist     = self.watchlist,
            bb_window     = strategy.bb_window,
            bb_std        = strategy.bb_std,
            rsi_window    = strategy.rsi_window,
            max_rsi       = strategy.buy_rsi + 6,
            lookback_bars = self.warmup_bars,
        )

        mode = "PAPER" if PAPER else "⚠️  LIVE"
        log.info(f"LiveScanner ready [{mode}]")
        log.info(f"  Data source : Alpaca REST / yfinance (15m polling)")
        log.info(f"  Execution   : Alpaca Trading API")
        log.info(f"  Watchlist   : {len(self.watchlist)} symbols")

    # ── Market hours guard ────────────────────────────────────────────────────

    def _is_market_hours(self) -> bool:
        """
        Return True only during NYSE regular hours: Mon–Fri 9:30am–4:00pm ET.

        Why this matters: Alpaca will reject orders outside market hours.
        Without this check the scanner wastes API calls overnight and on
        weekends and fills the log with confusing rejection errors.
        """
        eastern    = pytz.timezone("America/New_York")
        now_et     = datetime.datetime.now(eastern)
        if now_et.weekday() >= 5:           # 5=Saturday, 6=Sunday
            return False
        market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
        return market_open <= now_et <= market_close

    # ── Signal confirmation ───────────────────────────────────────────────────

    def _confirm_signal(self, symbol: str, raw_signal: str) -> str:
        """
        Require 2 consecutive 15-min bars showing an entry before acting.

        Why: a single bar touching the lower Bollinger Band can be a data
        spike or a one-bar wick that immediately reverses.  Two consecutive
        bars below the band with low RSI is a much stronger confirmation.

        On any non-entry bar the counter resets to zero.
        """
        if raw_signal == "enter":
            self._entry_confirmation[symbol] = (
                self._entry_confirmation.get(symbol, 0) + 1
            )
            count = self._entry_confirmation[symbol]
            if count >= 2:
                return "enter"
            log.info(f"  {symbol}: entry pending confirmation ({count}/2)")
            return "hold"
        # Reset counter on anything other than enter
        self._entry_confirmation[symbol] = 0
        return raw_signal

    # ── Trailing stop to break-even ───────────────────────────────────────────

    def _update_trailing_stop(self, symbol: str, position) -> None:
        """
        Once a position has gained half the take-profit distance, move the
        hard stop up to break-even (the entry price).

        Why: this locks in a no-loss scenario.  If the stock reverses after
        gaining 1.5% (halfway to the 3% take-profit), we exit at breakeven
        instead of at the original -1.5% stop.

        Implementation: cancel any open child orders (the bracket stop) and
        replace with a fresh stop order at the entry price.
        """
        try:
            entry   = float(position.avg_entry_price)
            current = float(position.current_price)
            gain    = (current - entry) / entry

            if gain >= self.take_profit_pct / 2:
                req         = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                open_orders = self._trader.get_orders(req)
                for o in open_orders:
                    try:
                        self._trader.cancel_order_by_id(o.id)
                    except Exception:
                        pass

                from alpaca.trading.requests import StopOrderRequest
                self._trader.submit_order(StopOrderRequest(
                    symbol        = symbol,
                    qty           = abs(int(float(position.qty))),
                    side          = OrderSide.SELL,
                    time_in_force = TimeInForce.DAY,
                    stop_price    = round(entry, 2),
                ))
                log.info(
                    f"  Trailing stop: {symbol} gain={gain*100:.1f}% "
                    f"→ stop moved to break-even {entry:.2f}"
                )
        except Exception as e:
            log.debug(f"  Trailing stop update failed for {symbol}: {e}")

    # ── Safe position fetch with retry ───────────────────────────────────────

    def _safe_open_positions(self) -> Dict[str, object]:
        """
        Fetch open positions from Alpaca with up to 3 retries.

        Why: transient network errors or Alpaca rate-limit responses should
        not crash the entire polling loop.  We use exponential back-off:
        wait 1s after the first failure, 2s after the second, 4s after the third.
        If all 3 attempts fail we skip this poll iteration gracefully.
        """
        for attempt in range(3):
            try:
                return self._open_positions()
            except Exception as e:
                log.warning(f"  get_positions failed (attempt {attempt+1}/3): {e}")
                time.sleep(2 ** attempt)    # 1s, 2s, 4s
        log.error("  Could not fetch positions after 3 attempts — skipping poll")
        return {}

    # ── Warmup ────────────────────────────────────────────────────────────────

    def _warmup(self, symbols: List[str]):
        log.info(f"Warming up {len(symbols)} symbols...")
        bars = fetch_bars_bulk(symbols, resolution="15", n_bars=self.warmup_bars)
        for symbol, df in bars.items():
            self._cache[symbol] = df
            log.info(f"  {symbol}: {len(df)} bars cached")

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _refresh_and_evaluate(self, symbol: str) -> str:
        """Fetch latest bars, update cache, run strategy, return signal."""
        try:
            df_new = fetch_bars(symbol, resolution="15", n_bars=self.warmup_bars)
        except Exception as e:
            log.warning(f"  {symbol}: data refresh failed — {e}")
            return "hold"

        if df_new.empty or len(df_new) < 25:
            return "hold"

        self._cache[symbol] = df_new

        try:
            df = self.strategy.populate_indicators(df_new.copy())
            df = self.strategy.generate_signals(df)
        except Exception as e:
            log.debug(f"  {symbol}: strategy error — {e}")
            return "hold"

        sig = int(df["signal"].iloc[-1])
        return "enter" if sig == 1 else "exit" if sig == -1 else "hold"

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _open_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self._trader.get_all_positions()}

    def _equity(self) -> float:
        return float(self._trader.get_account().equity)

    def _has_pending_order(self, symbol: str) -> bool:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        return len(self._trader.get_orders(req)) > 0

    # ── Order execution ─────────────────────────────────────────────────────

    def _qty(self, price: float) -> int:
        equity    = self._equity()
        risk_amt  = equity * self.risk_pct
        stop_dist = price * self.stop_loss_pct
        return max(int(risk_amt / stop_dist), 1)

    def _enter(self, symbol: str, price: float):
        qty = self._qty(price)
        sl  = round(price * (1 - self.stop_loss_pct), 2)
        tp  = round(price * (1 + self.take_profit_pct), 2)
        log.info(f"  ▶ ENTER {symbol:<6}  price={price:.2f}  qty={qty}  sl={sl}  tp={tp}")
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.DAY,
            order_class   = "bracket",
            stop_loss     = {"stop_price": sl},
            take_profit   = {"limit_price": tp},
        ))
        self._position_entry_prices[symbol] = price

    def _exit(self, symbol: str, qty: str, current_price: float = float("nan")):
        log.info(f"  ◀ EXIT  {symbol:<6}  qty={qty}")
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = abs(int(float(qty))),
            side          = OrderSide.SELL,
            time_in_force = TimeInForce.DAY,
        ))
        # Track realized daily PnL
        entry = self._position_entry_prices.pop(symbol, None)
        if entry and not pd.isna(current_price):
            realized = (current_price - entry) * abs(int(float(qty)))
            today    = datetime.date.today().isoformat()
            self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + realized
            log.info(
                f"  Daily realized PnL ({today}): ${self._daily_pnl[today]:+.2f}"
            )

    # ── Screener loop ─────────────────────────────────────────────────────────

    def _screener_loop(self):
        while True:
            time.sleep(self.screen_interval)
            try:
                log.info("─── Re-scanning watchlist ──────────────────────────")
                candidates = self._screener.scan()
                new_symbols = [c["symbol"] for c in candidates]
                if new_symbols:
                    to_warm = [s for s in new_symbols if s not in self._cache]
                    if to_warm:
                        self._warmup(to_warm)
                    self._active_symbols = new_symbols
                    log.info(f"Active symbols: {self._active_symbols}")
            except Exception as e:
                log.error(f"Screener error: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _poll(self):
        while True:
            # Improvement 1: market hours guard — don't poll outside NYSE hours
            if not self._is_market_hours():
                log.info("Market closed — sleeping 5 min")
                time.sleep(300)
                continue

            log.info("─── Polling signals ────────────────────────────────")

            # Improvement 4: safe reconnect — retry up to 3× on API errors
            positions = self._safe_open_positions()
            n_open    = len(positions)

            for symbol in list(self._active_symbols):
                log.info(f"[{symbol}]")

                price = float("nan")
                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                # Improvement 3: trailing stop — move stop to break-even once
                # the position has gained half the take-profit distance
                if symbol in positions:
                    self._update_trailing_stop(symbol, positions[symbol])

                raw_signal = self._refresh_and_evaluate(symbol)

                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                # Improvement 2: signal confirmation — require 2 consecutive bars
                signal = self._confirm_signal(symbol, raw_signal)

                log.info(
                    f"  close={price:.2f}  raw={raw_signal.upper():<5}  "
                    f"confirmed={signal.upper():<5}  positions={n_open}/{self.max_positions}"
                )

                if signal == "enter" and symbol not in positions:
                    if n_open >= self.max_positions:
                        log.info(f"  Skipped — max positions reached")
                    elif self._has_pending_order(symbol):
                        log.info(f"  Skipped — pending order exists")
                    else:
                        self._enter(symbol, price)
                        n_open += 1

                elif signal == "exit" and symbol in positions:
                    self._exit(symbol, positions[symbol].qty, current_price=price)

                else:
                    log.info(f"  Hold")

            log.info(f"─── Sleeping {self.poll_interval}s ─────────────────────")
            time.sleep(self.poll_interval)

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        mode = "PAPER" if PAPER else "LIVE"
        log.info(f"\n{'━'*55}")
        log.info(f"  Mean Reversion Scanner  [{mode}]  15m bars")
        log.info(f"  Data  : Alpaca REST / yfinance")
        log.info(f"  Exec  : Alpaca Trading API")
        log.info(f"  Polls : every {self.poll_interval}s")
        log.info(f"  Scans : every {self.screen_interval}s")
        log.info(f"{'━'*55}\n")

        log.info("Running initial screen...")
        candidates           = self._screener.scan()
        self._active_symbols = [c["symbol"] for c in candidates]

        if self._active_symbols:
            self._warmup(self._active_symbols)
        else:
            log.info("No initial candidates — screener retries in background")

        threading.Thread(
            target=self._screener_loop, daemon=True, name="screener"
        ).start()
        log.info("Screener thread started\n")

        self._poll()
