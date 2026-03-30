"""
scanner/crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━
24/7 crypto polling scanner using CryptoMeanReversionStrategy.

Near-clone of LiveScanner with exactly 3 changes for crypto:

  1. No market-hours guard — crypto trades 24/7.
     Instead: sleep 30 min during the quietest window (02:00–03:59 UTC)
     to avoid noise during thin liquidity.

  2. Fractional _qty() — returns float instead of int.
     Crypto positions are sized to 6 decimal places (e.g. 0.001234 BTC).

  3. No int() cast on qty in _enter()/_exit() — MarketOrderRequest accepts
     float quantities for crypto symbols.

Everything else (confirmation counter, trailing stop, safe reconnect,
screener thread) is identical to LiveScanner.

Usage:
    python scanner/run_crypto_scanner.py
"""

import datetime
import logging
import os
import sys
import threading
import time
from typing import Dict, List

import pytz
import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest

_REPO         = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv()
load_dotenv(os.path.join(_STRATEGY_IDE, ".env"))

try:
    from strategy_ide.strategies.base_strategy import BaseStrategy
    from strategy_ide.data.crypto_fetcher import fetch_crypto_bars, fetch_crypto_bars_bulk
except ImportError:
    from strategies.base_strategy import BaseStrategy
    from data.crypto_fetcher import fetch_crypto_bars, fetch_crypto_bars_bulk

from scanner.crypto_screener import CryptoMeanReversionScreener, CRYPTO_WATCHLIST

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_KEY    = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")


class CryptoScanner:
    """
    24/7 polling scanner for crypto mean reversion.

    Parameters
    ----------
    strategy : BaseStrategy
        CryptoMeanReversionStrategy (or subclass).
    watchlist : list[str]
        Crypto pairs to watch, e.g. ["BTC/USD", "ETH/USD"].
    poll_interval : int
        Seconds between signal checks. Default 3600 (1 hour — matches 1h bars).
    screen_interval : int
        Seconds between full watchlist re-scans. Default 7200 (2 hours).
    warmup_bars : int
        Historical 1h bars to pre-load per pair.
    max_positions : int
        Max concurrent open crypto positions.
    risk_pct : float
        Fraction of equity risked per trade.
    stop_loss_pct : float
        Hard stop percentage below entry (fallback when ATR unavailable).
    take_profit_pct : float
        Take-profit percentage above entry.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        watchlist: List[str]    = CRYPTO_WATCHLIST,
        poll_interval: int      = 3600,
        screen_interval: int    = 7200,
        warmup_bars: int        = 100,
        max_positions: int      = 3,
        risk_pct: float         = 0.01,
        stop_loss_pct: float    = 0.04,
        take_profit_pct: float  = 0.08,
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

        self._cache: Dict[str, pd.DataFrame]     = {}
        self._active_symbols: List[str]          = []
        self._entry_confirmation: Dict[str, int] = {}
        self._position_entry_prices: Dict[str, float] = {}
        self._daily_pnl: Dict[str, float]        = {}

        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

        self._screener = CryptoMeanReversionScreener(
            watchlist        = self.watchlist,
            bb_window        = getattr(strategy, "bb_window",  20),
            bb_std           = getattr(strategy, "bb_std",     2.0),
            rsi_window       = getattr(strategy, "rsi_window", 14),
            max_rsi          = getattr(strategy, "buy_rsi",    28) + 7,
            lookback_bars    = self.warmup_bars,
        )

        mode = "PAPER" if PAPER else "LIVE"
        log.info(f"CryptoScanner ready [{mode}]")
        log.info(f"  Data source : Alpaca CryptoHistoricalDataClient (1h bars)")
        log.info(f"  Execution   : Alpaca Trading API")
        log.info(f"  Watchlist   : {len(self.watchlist)} pairs")

    # ── CHANGE 1: 24/7 low-volatility sleep instead of market hours guard ────

    def _maybe_sleep_quiet_window(self) -> bool:
        """
        During 02:00–03:59 UTC (thinnest crypto liquidity), skip the poll
        and sleep 30 minutes to avoid noise trades.
        Returns True if sleeping (caller should continue the loop).
        """
        utc_hour = datetime.datetime.now(pytz.utc).hour
        if utc_hour in (2, 3):
            log.info("Quiet window (02:00–03:59 UTC) — sleeping 30 min")
            time.sleep(1800)
            return True
        return False

    # ── Signal confirmation (identical to LiveScanner) ────────────────────────

    def _confirm_signal(self, symbol: str, raw_signal: str) -> str:
        if raw_signal == "enter":
            self._entry_confirmation[symbol] = (
                self._entry_confirmation.get(symbol, 0) + 1
            )
            count = self._entry_confirmation[symbol]
            if count >= 2:
                return "enter"
            log.info(f"  {symbol}: entry pending confirmation ({count}/2)")
            return "hold"
        self._entry_confirmation[symbol] = 0
        return raw_signal

    # ── Trailing stop to break-even (identical to LiveScanner) ───────────────

    def _update_trailing_stop(self, symbol: str, position) -> None:
        try:
            entry   = float(position.avg_entry_price)
            current = float(position.current_price)
            gain    = (current - entry) / entry

            if gain >= self.take_profit_pct / 2:
                req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                for o in self._trader.get_orders(req):
                    try:
                        self._trader.cancel_order_by_id(o.id)
                    except Exception:
                        pass

                from alpaca.trading.requests import StopOrderRequest
                # Crypto qty is fractional — do NOT cast to int
                self._trader.submit_order(StopOrderRequest(
                    symbol        = symbol,
                    qty           = abs(float(position.qty)),
                    side          = OrderSide.SELL,
                    time_in_force = TimeInForce.GTC,   # GTC: crypto is 24/7
                    stop_price    = round(entry, 8),
                ))
                log.info(
                    f"  Trailing stop: {symbol} gain={gain*100:.1f}% "
                    f"→ stop moved to break-even {entry}"
                )
        except Exception as e:
            log.debug(f"  Trailing stop update failed for {symbol}: {e}")

    # ── Safe position fetch (identical to LiveScanner) ────────────────────────

    def _safe_open_positions(self) -> Dict[str, object]:
        for attempt in range(3):
            try:
                return self._open_positions()
            except Exception as e:
                log.warning(f"  get_positions failed (attempt {attempt+1}/3): {e}")
                time.sleep(2 ** attempt)
        log.error("  Could not fetch positions after 3 attempts — skipping poll")
        return {}

    # ── Warmup ────────────────────────────────────────────────────────────────

    def _warmup(self, symbols: List[str]):
        log.info(f"Warming up {len(symbols)} crypto pairs...")
        bars = fetch_crypto_bars_bulk(symbols, resolution="60", n_bars=self.warmup_bars)
        for symbol, df in bars.items():
            self._cache[symbol] = df
            log.info(f"  {symbol}: {len(df)} bars cached")

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _refresh_and_evaluate(self, symbol: str) -> str:
        try:
            df_new = fetch_crypto_bars(symbol, resolution="60", n_bars=self.warmup_bars)
        except Exception as e:
            log.warning(f"  {symbol}: data refresh failed — {e}")
            return "hold"

        min_bars = getattr(self.strategy, "bb_window", 20) + getattr(self.strategy, "rsi_window", 14)
        if df_new.empty or len(df_new) < min_bars:
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

    # ── CHANGE 2: Fractional qty for crypto ───────────────────────────────────

    def _qty(self, price: float) -> float:
        """
        Fractional position sizing for crypto.
        Returns float (e.g. 0.001234 BTC) — NOT rounded to int.
        """
        equity    = self._equity()
        risk_amt  = equity * self.risk_pct
        stop_dist = price * self.stop_loss_pct
        return max(round(risk_amt / stop_dist, 6), 0.000001)

    # ── CHANGE 3: No int() cast — float qty for crypto orders ─────────────────

    def _enter(self, symbol: str, price: float):
        qty = self._qty(price)
        sl  = round(price * (1 - self.stop_loss_pct), 8)
        tp  = round(price * (1 + self.take_profit_pct), 8)
        log.info(f"  ▶ ENTER {symbol:<10}  price={price}  qty={qty}  sl={sl}  tp={tp}")
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,              # float, not int
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,  # GTC: crypto trades 24/7
            order_class   = "bracket",
            stop_loss     = {"stop_price": sl},
            take_profit   = {"limit_price": tp},
        ))
        self._position_entry_prices[symbol] = price

    def _exit(self, symbol: str, qty: str, current_price: float = float("nan")):
        qty_float = abs(float(qty))          # float, not int
        log.info(f"  ◀ EXIT  {symbol:<10}  qty={qty_float}")
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty_float,
            side          = OrderSide.SELL,
            time_in_force = TimeInForce.GTC,
        ))
        entry = self._position_entry_prices.pop(symbol, None)
        if entry and not pd.isna(current_price):
            realized = (current_price - entry) * qty_float
            today    = datetime.date.today().isoformat()
            self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + realized
            log.info(f"  Daily realized PnL ({today}): ${self._daily_pnl[today]:+.2f}")

    # ── Screener loop ─────────────────────────────────────────────────────────

    def _screener_loop(self):
        while True:
            time.sleep(self.screen_interval)
            try:
                log.info("─── Re-scanning crypto watchlist ───────────────────")
                candidates   = self._screener.scan()
                new_symbols  = [c["symbol"] for c in candidates]
                if new_symbols:
                    to_warm = [s for s in new_symbols if s not in self._cache]
                    if to_warm:
                        self._warmup(to_warm)
                    self._active_symbols = new_symbols
                    log.info(f"Active pairs: {self._active_symbols}")
            except Exception as e:
                log.error(f"Crypto screener error: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _poll(self):
        while True:
            # CHANGE 1: no market hours guard — sleep briefly during quiet UTC window
            if self._maybe_sleep_quiet_window():
                continue

            log.info("─── Polling crypto signals ─────────────────────────")

            positions = self._safe_open_positions()
            # Filter to only crypto positions (symbol contains "/")
            positions = {s: p for s, p in positions.items() if "/" in s}
            n_open    = len(positions)

            for symbol in list(self._active_symbols):
                log.info(f"[{symbol}]")

                price = float("nan")
                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                if symbol in positions:
                    self._update_trailing_stop(symbol, positions[symbol])

                raw_signal = self._refresh_and_evaluate(symbol)

                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                signal = self._confirm_signal(symbol, raw_signal)

                log.info(
                    f"  price={price:.4g}  raw={raw_signal.upper():<5}  "
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
        log.info(f"  Crypto Mean Reversion Scanner  [{mode}]  1h bars")
        log.info(f"  Data  : Alpaca CryptoHistoricalDataClient")
        log.info(f"  Exec  : Alpaca Trading API")
        log.info(f"  Polls : every {self.poll_interval}s")
        log.info(f"  Scans : every {self.screen_interval}s")
        log.info(f"{'━'*55}\n")

        log.info("Running initial crypto screen...")
        candidates           = self._screener.scan()
        self._active_symbols = [c["symbol"] for c in candidates]

        if self._active_symbols:
            self._warmup(self._active_symbols)
        else:
            log.info("No initial candidates — screener retries in background")

        threading.Thread(
            target=self._screener_loop, daemon=True, name="crypto-screener"
        ).start()
        log.info("Crypto screener thread started\n")

        self._poll()
