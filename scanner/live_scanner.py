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
    Polling-based scanner for equities.
    Market data: yfinance / Alpaca REST
    Order execution: Alpaca Trading API

    Parameters
    ----------
    strategy : BaseStrategy
        Strategy instance (tuned for the chosen resolution).
    watchlist : list[str]
        Broad universe for the screener.
    poll_interval : int
        Seconds between signal checks.
    screen_interval : int
        Seconds between full watchlist re-scans.
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
    resolution : str
        Bar resolution for data fetching ("5", "15", etc.).
    confirm_bars : int
        Number of consecutive entry bars required before acting.
        Set to 1 to act immediately on first signal (recommended for
        high-frequency strategies like VWAP reversion).
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
        resolution: str           = "15",
        confirm_bars: int         = 2,
        max_position_pct: float   = 0.25,
    ):
        self.strategy         = strategy
        self.watchlist        = watchlist
        self.poll_interval    = poll_interval
        self.screen_interval  = screen_interval
        self.warmup_bars      = warmup_bars
        self.max_positions    = max_positions
        self.risk_pct         = risk_pct
        self.stop_loss_pct    = stop_loss_pct
        self.take_profit_pct  = take_profit_pct
        self.resolution       = resolution
        self.confirm_bars     = confirm_bars
        self.max_position_pct = max_position_pct

        self._cache: Dict[str, pd.DataFrame]   = {}
        self._active_symbols: List[str]        = []
        self._entry_confirmation: Dict[str, int] = {}   # symbol → consecutive entry bars
        self._position_entry_prices: Dict[str, float] = {}  # symbol → entry price
        self._daily_pnl: Dict[str, float] = {}          # date string → realized PnL
        # Symbols whose trailing stop has already been moved to break-even.
        # Prevents the cancel+resubmit churn described in issue #4 where
        # each poll would re-move an already-moved stop.
        self._trailing_moved: set = set()

        # Alpaca for orders + positions only
        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

        # Build screener — read BB/RSI params from strategy if available,
        # otherwise use sensible defaults (e.g. VWAP strategy has no bb_window).
        _bb_window  = getattr(strategy, "bb_window", 20)
        _bb_std     = getattr(strategy, "bb_std", 2.0)
        _rsi_window = getattr(strategy, "rsi_window", 14)
        _buy_rsi    = getattr(strategy, "buy_rsi", 40)

        self._screener = MeanReversionScreener(
            watchlist       = self.watchlist,
            bb_window       = _bb_window,
            bb_std          = _bb_std,
            rsi_window      = _rsi_window,
            max_rsi         = _buy_rsi + 15,   # wider pre-filter for more candidates (was +10)
            min_volume_ratio = 0.8,            # accept stocks with 80% of avg volume (was 1.1)
            max_candidates   = 20,             # return more candidates per scan (was 10)
            lookback_bars   = self.warmup_bars,
            resolution      = self.resolution,
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
        Require `confirm_bars` consecutive bars showing an entry before acting.

        Set confirm_bars=1 to act on the first signal (no confirmation delay).
        On any non-entry bar the counter resets to zero.
        """
        if raw_signal == "enter":
            self._entry_confirmation[symbol] = (
                self._entry_confirmation.get(symbol, 0) + 1
            )
            count = self._entry_confirmation[symbol]
            if count >= self.confirm_bars:
                return "enter"
            log.info(f"  {symbol}: entry pending confirmation ({count}/{self.confirm_bars})")
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

        Idempotent: once moved, we remember the symbol in
        ``self._trailing_moved`` and skip re-issuing the cancel/submit on
        subsequent polls. See issue #4 — without this guard AMZN on
        2026-04-13 produced 8 stop-modification cycles in 14 minutes
        because each poll re-ran the full dance on an already-moved stop.
        """
        if symbol in self._trailing_moved:
            return
        try:
            entry   = float(position.avg_entry_price)
            current = float(position.current_price)
            gain    = (current - entry) / entry

            if gain >= self.take_profit_pct / 2:
                req         = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                open_orders = self._trader.get_orders(req)
                old_stop    = None
                for o in open_orders:
                    try:
                        if o.stop_price is not None:
                            old_stop = float(o.stop_price)
                        self._trader.cancel_order_by_id(o.id)
                    except Exception:
                        pass

                new_stop = round(entry, 2)
                from alpaca.trading.requests import StopOrderRequest
                self._trader.submit_order(StopOrderRequest(
                    symbol        = symbol,
                    qty           = abs(int(float(position.qty))),
                    side          = OrderSide.SELL,
                    time_in_force = TimeInForce.DAY,
                    stop_price    = new_stop,
                ))
                self._trailing_moved.add(symbol)
                old_str = f"${old_stop:.2f}" if old_stop is not None else "n/a"
                log.info(
                    f"  STOP MOVED {symbol}: old={old_str} new=${new_stop:.2f} "
                    f"gain={gain*100:.1f}% reason=trailing_break_even"
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
        log.info(f"Warming up {len(symbols)} symbols ({self.resolution}-min bars)...")
        bars = fetch_bars_bulk(symbols, resolution=self.resolution, n_bars=self.warmup_bars)
        for symbol, df in bars.items():
            self._cache[symbol] = df
            log.info(f"  {symbol}: {len(df)} bars cached")

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _refresh_and_evaluate(self, symbol: str):
        """Fetch latest bars, update cache, run strategy, return (signal, reason).

        `reason` is the strategy-specific label describing why the bar
        triggered (e.g. ``bb_lower+rsi_oversold`` for an entry,
        ``bb_upper_touch`` for an exit). Empty string on hold or when the
        strategy does not populate a ``reason`` column. See issue #3.
        """
        try:
            df_new = fetch_bars(symbol, resolution=self.resolution, n_bars=self.warmup_bars)
        except Exception as e:
            log.warning(f"  {symbol}: data refresh failed — {e}")
            return "hold", ""

        if df_new.empty or len(df_new) < 25:
            return "hold", ""

        self._cache[symbol] = df_new

        try:
            df = self.strategy.populate_indicators(df_new.copy())
            df = self.strategy.generate_signals(df)
        except Exception as e:
            log.debug(f"  {symbol}: strategy error — {e}")
            return "hold", ""

        sig    = int(df["signal"].iloc[-1])
        reason = str(df["reason"].iloc[-1]) if "reason" in df.columns else ""
        signal = "enter" if sig == 1 else "exit" if sig == -1 else "hold"
        return signal, reason

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _open_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self._trader.get_all_positions()}

    def _equity(self) -> float:
        return float(self._trader.get_account().equity)

    def _has_pending_order(self, symbol: str) -> bool:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        return len(self._trader.get_orders(req)) > 0

    # ── Order execution ─────────────────────────────────────────────────────

    def _qty(self, symbol: str, price: float) -> int:
        """
        Calculate share quantity for entry based on risk management.

        Three independent caps applied (smallest wins):
          1. Risk-based:     equity × risk_pct / (price × stop_loss_pct)
          2. Buying power:   95% of buying_power / price
          3. Position cap:   equity × max_position_pct / price   (issue #5)
        """
        try:
            account = self._trader.get_account()
            equity = float(account.equity)
            buying_power = float(account.buying_power)
        except Exception as e:
            log.warning(f"Could not fetch account info: {e}")
            return 0

        # 1. Risk-based sizing
        risk_amt = equity * self.risk_pct
        stop_dist = price * self.stop_loss_pct
        qty_by_risk = max(int(risk_amt / stop_dist), 1) if stop_dist > 0 else 1

        # 2. Buying power constraint (5% buffer)
        qty_by_bp = int(buying_power * 0.95 / price) if price > 0 else 1

        # 3. Position-size cap: no single position > max_position_pct of equity
        max_notional = equity * self.max_position_pct
        qty_by_cap = int(max_notional / price) if price > 0 else 0

        qty = min(qty_by_risk, qty_by_bp, qty_by_cap)
        qty = max(qty, 1)

        # Floor at $100 minimum order
        if qty * price < 100:
            qty = max(1, int(100 / price))

        log.info(
            f"  SIZING {symbol}: equity=${equity:,.0f} risk={self.risk_pct*100:.2f}% "
            f"cap={self.max_position_pct*100:.0f}% → qty={qty} "
            f"(${qty*price:,.0f}, {qty*price/equity*100:.1f}% of equity)"
        )
        return qty

    def _enter(self, symbol: str, price: float, reason: str = ""):
        qty = self._qty(symbol, price)
        
        # Safety check: don't place order if qty is 0 or negative
        if qty <= 0:
            log.warning(f"  ⚠ SKIP ENTER {symbol}: insufficient buying power or risk calculation failed")
            return
        
        # Final safety check: verify order cost against current buying power
        try:
            account = self._trader.get_account()
            current_buying_power = float(account.buying_power)
            order_cost = qty * price
            if order_cost > current_buying_power * 0.95:  # Allow 5% buffer for margin/fees
                log.warning(f"  ⚠ SKIP ENTER {symbol}: order cost ${order_cost:,.0f} > buying power ${current_buying_power:,.0f}")
                return
        except Exception as e:
            log.warning(f"  ⚠ Could not verify buying power for {symbol}: {e}")
            return
        
        sl  = round(price * (1 - self.stop_loss_pct), 2)
        tp  = round(price * (1 + self.take_profit_pct), 2)
        order_cost = qty * price
        reason_tag = f"  reason={reason}" if reason else ""
        log.info(
            f"  ▶ ENTER {symbol:<6}  price={price:.2f}  qty={qty}  sl={sl}  tp={tp}  "
            f"cost=${order_cost:,.0f}{reason_tag}"
        )
        
        try:
            self._trader.submit_order(MarketOrderRequest(
                symbol        = symbol,
                qty           = qty,
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.GTC,
                order_class   = "bracket",
                stop_loss     = {"stop_price": sl},
                take_profit   = {"limit_price": tp},
            ))
            self._position_entry_prices[symbol] = price
        except Exception as e:
            log.error(f"  ✗ Order failed for {symbol}: {e}")

    def _exit(self, symbol: str, qty: str, current_price: float = float("nan"), reason: str = ""):
        reason_tag = f"  reason={reason}" if reason else ""
        log.info(f"  ◀ EXIT  {symbol:<6}  qty={qty}{reason_tag}")
        sell_qty = abs(int(float(qty)))

        # Cancel any open bracket children (STOP / LIMIT) holding the shares in
        # `held_for_orders`. Without this, the market sell below fails with
        # "insufficient qty available for order" and the strategy EXIT is
        # silently broken for every bracketed position. See issue #2.
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            open_orders = self._trader.get_orders(req)
        except Exception as e:
            log.warning(f"  {symbol}: could not list open orders before exit — {e}")
            open_orders = []

        canceled_any = False
        for o in open_orders:
            try:
                self._trader.cancel_order_by_id(o.id)
                canceled_any = True
                log.info(f"  Canceled bracket child {o.side.value} {o.type.value} ({o.id})")
            except Exception as e:
                log.warning(f"  Cancel failed for {o.id}: {e}")

        # Alpaca processes cancellations asynchronously. Poll `qty_available`
        # for up to ~2s so the market sell doesn't race ahead of the release
        # of `held_for_orders`. 10 x 200ms gives ample headroom in practice.
        if canceled_any:
            for _ in range(10):
                time.sleep(0.2)
                try:
                    pos = self._trader.get_open_position(symbol)
                    if int(float(pos.qty_available)) >= sell_qty:
                        break
                except Exception:
                    # Position gone (already flat) — cancel alone closed us out
                    break

        try:
            self._trader.submit_order(MarketOrderRequest(
                symbol        = symbol,
                qty           = sell_qty,
                side          = OrderSide.SELL,
                time_in_force = TimeInForce.DAY,
            ))
        except Exception as e:
            log.error(f"  ✗ EXIT submit failed for {symbol}: {e}")
            return

        # Clear trailing-stop tracking — the bracket child that held the
        # break-even stop is gone, and a re-entry on this symbol should
        # start the half-TP check fresh. See issue #4.
        self._trailing_moved.discard(symbol)

        # Track realized daily PnL
        entry = self._position_entry_prices.pop(symbol, None)
        if entry and not pd.isna(current_price):
            realized = (current_price - entry) * sell_qty
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
            # Equity scanner only — strip crypto positions (e.g. AVAXUSD) so they
            # aren't evaluated with equity strategies or counted against max_positions.
            positions = {s: p for s, p in positions.items() if not s.endswith("USD")}
            n_open    = len(positions)

            # Reconcile trailing-stop tracking against live positions. If a
            # symbol was flattened externally (bracket TP fill, manual close,
            # scanner restart) clear its entry so a future re-entry starts
            # the half-TP check fresh. See issue #4.
            stale = self._trailing_moved - set(positions)
            if stale:
                self._trailing_moved -= stale

            # Poll strategy: evaluate all held positions (regardless of entry source)
            # plus screener candidates. Held positions ALWAYS get monitored, so if
            # you hold ADBE from a manual trade, the scanner will exit it if the
            # strategy triggers an exit signal. See issue #12 (core_symbols antipattern).
            poll_symbols = list(dict.fromkeys(
                list(positions.keys()) + self._active_symbols
            ))

            for symbol in poll_symbols:
                log.info(f"[{symbol}]")

                price = float("nan")
                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                # Improvement 3: trailing stop — move stop to break-even once
                # the position has gained half the take-profit distance
                if symbol in positions:
                    self._update_trailing_stop(symbol, positions[symbol])

                # Improvement 5: server-side stop-loss — exit if position has
                # breached stop-loss %. Bracket stop orders expire at EOD, so
                # overnight gaps can bypass them. This catches those cases.
                if symbol in positions:
                    pos = positions[symbol]
                    entry_price   = float(pos.avg_entry_price)
                    current_price = float(pos.current_price)
                    loss_pct      = (entry_price - current_price) / entry_price
                    if loss_pct >= self.stop_loss_pct:
                        log.warning(
                            f"  ⛔ STOP-LOSS {symbol}: down {loss_pct*100:.2f}% "
                            f"(entry={entry_price:.2f}, now={current_price:.2f}, "
                            f"threshold={self.stop_loss_pct*100:.1f}%)"
                        )
                        self._exit(
                            symbol, pos.qty,
                            current_price=current_price,
                            reason="hard_stop_loss",
                        )
                        n_open -= 1
                        continue

                raw_signal, reason = self._refresh_and_evaluate(symbol)

                if symbol in self._cache and not self._cache[symbol].empty:
                    price = float(self._cache[symbol]["close"].iloc[-1])

                # Improvement 2: signal confirmation — require 2 consecutive bars
                signal = self._confirm_signal(symbol, raw_signal)

                reason_suffix = f"  reason={reason}" if reason else ""
                log.info(
                    f"  {symbol}: close={price:.2f}  raw={raw_signal.upper():<5}  "
                    f"confirmed={signal.upper():<5}  "
                    f"positions={n_open}/{self.max_positions}{reason_suffix}"
                )

                if signal == "enter" and symbol not in positions:
                    if n_open >= self.max_positions:
                        log.info(f"  {symbol}: skipped — max positions reached")
                    elif self._has_pending_order(symbol):
                        log.info(f"  {symbol}: skipped — pending order exists")
                    else:
                        self._enter(symbol, price, reason=reason)
                        n_open += 1

                elif signal == "exit" and symbol in positions:
                    self._exit(symbol, positions[symbol].qty, current_price=price, reason=reason)

                else:
                    log.info(f"  {symbol}: hold")

            log.info(f"─── Sleeping {self.poll_interval}s ─────────────────────")
            time.sleep(self.poll_interval)

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        mode = "PAPER" if PAPER else "LIVE"
        log.info(f"\n{'━'*55}")
        log.info(f"  Scanner  [{mode}]  {self.resolution}-min bars")
        log.info(f"  Strategy : {self.strategy.name}")
        log.info(f"  Data     : Alpaca REST / yfinance")
        log.info(f"  Exec     : Alpaca Trading API")
        log.info(f"  Polls    : every {self.poll_interval}s")
        log.info(f"  Scans    : every {self.screen_interval}s")
        log.info(f"  Confirm  : {self.confirm_bars} bar(s)")
        log.info(f"{'━'*55}\n")

        log.info("Running initial screen...")
        candidates           = self._screener.scan()
        self._active_symbols = [c["symbol"] for c in candidates]

        # Warm up screener candidates
        to_warm = [s for s in self._active_symbols if s not in self._cache]
        if to_warm:
            self._warmup(to_warm)
        elif not self._active_symbols:
            log.info("No initial screener candidates")

        # Warm up any held positions not already cached — these must be monitored
        # for exits regardless of whether they appear in the screener.
        held = [s for s in self._open_positions().keys() if not s.endswith("USD")]
        held_to_warm = [s for s in held if s not in self._cache]
        if held_to_warm:
            log.info(f"Warming up {len(held_to_warm)} held position(s): {held_to_warm}")
            self._warmup(held_to_warm)

        threading.Thread(
            target=self._screener_loop, daemon=True, name="screener"
        ).start()
        log.info("Screener thread started\n")

        self._poll()
