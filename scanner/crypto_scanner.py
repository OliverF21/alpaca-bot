"""
scanner/crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━
Multi-strategy 24/7 crypto scanner.

Runs all four crypto strategies in parallel on each pair in the dynamic
universe, feeds signals into the Signal Arbitrator, and executes the
top-ranked trades.

Thread structure:
  - Main thread: 1h poll loop (fetch → strategies → arbitrator → execute)
  - Background thread 1: Universe ranker (30-min refresh)
  - Background thread 2: Position monitor (stop/TP checks every 5 min)
"""

import datetime
import logging
import os
import sys
import threading
import time
from typing import Dict, List, Set

import pytz
import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest

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

from scanner.crypto_universe import UniverseRanker
from scanner.signal_arbitrator import SignalArbitrator, COOLDOWN_BARS
from scanner.regime_detector import RegimeDetector, Regime, REGIME_STRATEGIES
from scanner.vol_filter import VolatilityFilter

log = logging.getLogger(__name__)

SIGNAL_LOOKBACK = 3  # scan this many closed bars for non-hold signals

_DIAG_COLS = {
    "crypto_trend_following": ["close", "ema_fast", "ema_slow", "adx", "atr"],
    "crypto_mean_reversion":  ["close", "rsi", "bb_pct_b", "bb_lower", "bb_mid", "atr"],
    "crypto_supertrend":      ["close", "supertrend", "supertrend_dir", "rsi"],
    "crypto_breakout":        ["close", "donch_high", "donch_mid", "atr", "atr_sma"],
}

_CRYPTO_BASES = {"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "UNI", "AAVE",
                 "DOT", "MATIC", "SHIB", "LTC", "XRP", "ADA", "ATOM", "ALGO"}

def _is_crypto(symbol: str) -> bool:
    """Alpaca returns 'DOGEUSD' for positions but 'DOGE/USD' for orders."""
    if "/" in symbol:
        return True
    for suffix in ("USD", "USDT", "USDC"):
        if symbol.endswith(suffix) and symbol[:-len(suffix)] in _CRYPTO_BASES:
            return True
    return False

def _normalize_symbol(symbol: str) -> str:
    """'DOGEUSD' → 'DOGE/USD' so it matches the universe and _position_meta keys."""
    if "/" in symbol:
        return symbol
    for suffix in ("USDT", "USDC", "USD"):
        if symbol.endswith(suffix) and symbol[:-len(suffix)] in _CRYPTO_BASES:
            return symbol[:-len(suffix)] + "/" + suffix
    return symbol

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
    Multi-strategy 24/7 crypto polling scanner.

    Parameters
    ----------
    strategies : list[BaseStrategy]
        All crypto strategies to run in parallel on each pair.
    poll_interval : int
        Seconds between signal checks (default 3600 = 1h).
    warmup_bars : int
        Historical 1h bars to pre-load per pair.
    universe_refresh : int
        Seconds between universe re-ranking (default 1800 = 30 min).
    universe_top_k : int
        Number of top pairs to include in active universe.
    """

    def __init__(
        self,
        strategies: List[BaseStrategy],
        poll_interval: int   = 3600,
        warmup_bars: int     = 250,
        universe_refresh: int = 1800,
        universe_top_k: int  = 8,
    ):
        self.strategies      = strategies
        self.poll_interval   = poll_interval
        self.warmup_bars     = warmup_bars

        self._cache: Dict[str, pd.DataFrame] = {}
        self._cooldowns: Dict[str, int]       = {}  # symbol → bars since exit
        self._position_meta: Dict[str, Dict]  = {}  # symbol → {strategy, stop, tp, trailing}
        self._daily_pnl: Dict[str, float]     = {}
        self._last_poll_time: float           = 0.0  # monotonic time of last poll
        self._recent_entries: Dict[str, float] = {}  # symbol → epoch of last entry

        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
        self._ranker = UniverseRanker(
            refresh_interval=universe_refresh,
            top_k=universe_top_k,
        )
        self._regime_detector = RegimeDetector(
            lookback=20, fit_window=200, refit_every=24,
        )
        self._vol_filter = VolatilityFilter(
            lookback=24, long_lookback=168,
            extreme_threshold=2.5, high_threshold=1.5, low_threshold=0.6,
        )

        mode = "PAPER" if PAPER else "LIVE"
        log.info(f"CryptoScanner ready [{mode}]  {len(strategies)} strategies")

    # ── Quiet window ─────────────────────────────────────────────────────────

    def _maybe_sleep_quiet_window(self) -> bool:
        utc_hour = datetime.datetime.now(pytz.utc).hour
        if utc_hour in (2, 3):
            log.info("Quiet window (02:00–03:59 UTC) — sleeping 30 min")
            time.sleep(1800)
            return True
        return False

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _open_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self._trader.get_all_positions()}

    def _safe_open_positions(self) -> Dict[str, object]:
        for attempt in range(3):
            try:
                return self._open_positions()
            except Exception as e:
                log.warning(f"  get_positions failed (attempt {attempt+1}/3): {e}")
                time.sleep(2 ** attempt)
        log.error("  Could not fetch positions after 3 attempts — skipping poll")
        return {}

    def _equity(self) -> float:
        return float(self._trader.get_account().equity)

    def _has_pending_order(self, symbol: str) -> bool:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        return len(self._trader.get_orders(req)) > 0

    # ── Data ─────────────────────────────────────────────────────────────────

    def _warmup(self, symbols: List[str]):
        log.info(f"Warming up {len(symbols)} crypto pairs...")
        bars = fetch_crypto_bars_bulk(symbols, resolution="60", n_bars=self.warmup_bars)
        for symbol, df in bars.items():
            self._cache[symbol] = df
            log.info(f"  {symbol}: {len(df)} bars cached")

    def _refresh_bars(self, symbol: str) -> pd.DataFrame:
        try:
            df = fetch_crypto_bars(symbol, resolution="60", n_bars=self.warmup_bars)
            if not df.empty:
                self._cache[symbol] = df
            return df
        except Exception as e:
            log.warning(f"  {symbol}: data refresh failed — {e}")
            return self._cache.get(symbol, pd.DataFrame())

    # ── Strategy evaluation ──────────────────────────────────────────────────

    @staticmethod
    def _log_diagnostics(symbol: str, strat_name: str, row: pd.Series, signal: str):
        cols = _DIAG_COLS.get(strat_name, ["close"])
        parts = []
        for c in cols:
            v = row.get(c, None)
            if v is not None and not pd.isna(v):
                parts.append(f"{c}={v:.4f}" if isinstance(v, float) else f"{c}={v}")
        log.info(f"  [{symbol}] {strat_name}: {signal.upper()}  {' | '.join(parts)}")

    def _evaluate_all_strategies(
        self, symbol: str, df: pd.DataFrame,
        allowed_strategies: set = None,
        stop_mult: float = 1.0,
    ) -> List[Dict]:
        """Run strategies on a pair's data, return list of signal dicts.

        Parameters
        ----------
        allowed_strategies : set or None
            If provided, only run strategies whose .name is in this set.
            Strategies not in the set are skipped entirely (regime gating).
        stop_mult : float
            GARCH-based multiplier applied to stop prices (>1 = wider stops).
        """
        results = []
        if len(df) < 2:
            return results

        # Drop the in-progress bar — last row from Alpaca is still forming
        df_closed = df.iloc[:-1]

        for strat in self.strategies:
            # Regime gating: skip strategies not allowed in current regime
            if allowed_strategies is not None and strat.name not in allowed_strategies:
                log.info(f"  [{symbol}] {strat.name}: SKIPPED (regime gate)")
                continue
            try:
                df_copy = df_closed.copy()
                df_copy = strat.populate_indicators(df_copy)
                df_copy = strat.generate_signals(df_copy)

                # Scan last K closed bars for the most recent non-hold signal
                lookback = min(SIGNAL_LOOKBACK, len(df_copy))
                tail = df_copy.iloc[-lookback:]
                non_hold = tail[tail["signal"] != 0]

                if not non_hold.empty:
                    sig_row = non_hold.iloc[-1]
                else:
                    sig_row = df_copy.iloc[-1]

                sig_val = int(sig_row["signal"])
                signal_str = "enter" if sig_val == 1 else "exit" if sig_val == -1 else "hold"
                conviction = float(sig_row.get("conviction", 0.0))

                # Diagnostic logging on every evaluation (latest closed bar)
                diag = df_copy.iloc[-1]
                self._log_diagnostics(symbol, strat.name, diag, signal_str)

                # entry_price uses latest closed bar (what we'd actually buy at)
                entry_price = float(diag["close"])

                raw_stop = float(sig_row.get("stop_price", 0)) if not pd.isna(sig_row.get("stop_price", float("nan"))) else 0
                raw_tp = float(sig_row.get("take_profit_price", 0)) if not pd.isna(sig_row.get("take_profit_price", float("nan"))) else 0

                # Apply GARCH vol multiplier: widen stop distance from entry
                if stop_mult != 1.0 and raw_stop > 0:
                    stop_dist = entry_price - raw_stop
                    raw_stop = entry_price - stop_dist * stop_mult

                results.append({
                    "symbol": symbol,
                    "signal": signal_str,
                    "conviction": conviction,
                    "strategy": strat.name,
                    "stop_price": raw_stop,
                    "take_profit_price": raw_tp,
                    "entry_price": entry_price,
                })
            except Exception as e:
                log.debug(f"  {symbol}/{strat.name}: strategy error — {e}")
        return results

    # ── Order execution ──────────────────────────────────────────────────────

    def _qty(self, price: float, risk_pct: float, stop_price: float, equity: float) -> float:
        risk_amt = equity * risk_pct
        stop_dist = abs(price - stop_price) if stop_price > 0 else price * 0.05
        stop_dist = max(stop_dist, price * 0.001)  # floor at 0.1%
        return max(round(risk_amt / stop_dist, 6), 0.000001)

    def _enter(self, symbol: str, price: float, action: Dict):
        risk_pct = action["risk_pct"]
        stop_price = action["stop_price"]
        tp_price = action["take_profit_price"]

        if stop_price <= 0:
            stop_price = round(price * 0.95, 8)
        if tp_price <= 0:
            tp_price = round(price * 1.15, 8)

        # Fetch equity once for all calculations
        equity = self._equity()

        qty = self._qty(price, risk_pct, stop_price, equity)
        sl = round(stop_price, 8)
        tp = round(tp_price, 8)

        # Cap at 20% of equity
        max_notional = equity * 0.20
        if qty * price > max_notional:
            qty = round(max_notional / price, 6)

        # Limit price 0.2% above last close for fast fill (crypto bracket orders require limit entry)
        limit_price = round(price * 1.002, 8)

        log.info(
            f"  ▶ ENTER {symbol:<10}  strategy={action['strategy']}  "
            f"conviction={action['conviction']:.2f}  price={price}  limit={limit_price}  "
            f"qty={qty}  sl={sl}  tp={tp}  risk={risk_pct*100:.0f}%"
        )
        # Alpaca does not support bracket/OTOCO orders for crypto.
        # Submit a simple limit buy; stop/TP are enforced by _position_monitor_loop.
        self._trader.submit_order(LimitOrderRequest(
            symbol        = symbol,
            qty           = qty,
            limit_price   = limit_price,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,
        ))

        # Track position metadata for monitoring
        use_trailing = (
            action["conviction"] >= 0.7
            and action["strategy"] in ("crypto_trend_following", "crypto_breakout")
        )
        self._position_meta[symbol] = {
            "strategy": action["strategy"],
            "entry_price": price,
            "stop_price": sl,
            "take_profit_price": tp,
            "trailing": use_trailing,
        }

    def _exit(self, symbol: str, qty: str, current_price: float = float("nan")):
        qty_float = abs(float(qty))
        log.info(f"  ◀ EXIT  {symbol:<10}  qty={qty_float}")

        # Cancel any open bracket children (STOP / LIMIT) holding the qty in
        # `held_for_orders`. Without this, the market sell below fails with
        # "insufficient qty available for order" and the strategy EXIT is
        # silently broken for every bracketed crypto position. See issue #2.
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

        # Poll qty_available up to ~2s so the market sell doesn't race the
        # release of `held_for_orders` (Alpaca cancel is async).
        if canceled_any:
            for _ in range(10):
                time.sleep(0.2)
                try:
                    pos = self._trader.get_open_position(symbol)
                    if float(pos.qty_available) >= qty_float:
                        break
                except Exception:
                    break

        try:
            self._trader.submit_order(MarketOrderRequest(
                symbol        = symbol,
                qty           = qty_float,
                side          = OrderSide.SELL,
                time_in_force = TimeInForce.GTC,
            ))
        except Exception as e:
            log.error(f"  ✗ EXIT submit failed for {symbol}: {e}")
            return

        entry = self._position_meta.pop(symbol, {}).get("entry_price")
        if entry and not pd.isna(current_price):
            realized = (current_price - entry) * qty_float
            today = datetime.date.today().isoformat()
            self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + realized
            log.info(f"  Daily realized PnL ({today}): ${self._daily_pnl[today]:+.2f}")

        # Start cooldown
        self._cooldowns[symbol] = 0

    # ── Position monitor (background thread) ─────────────────────────────────

    def _position_monitor_loop(self):
        """Enforce stop-loss, take-profit, and trailing stops every 5 minutes.

        Alpaca does not support bracket orders for crypto, so stop/TP
        enforcement happens here instead of on the exchange side.
        """
        while True:
            time.sleep(300)
            try:
                positions = self._safe_open_positions()
                positions = {_normalize_symbol(s): p for s, p in positions.items() if _is_crypto(s)}

                for symbol, pos in positions.items():
                    meta = self._position_meta.get(symbol)
                    if not meta:
                        continue

                    current = float(pos.current_price)
                    entry = meta["entry_price"]
                    sl = meta["stop_price"]
                    tp = meta["take_profit_price"]

                    # ── Stop-loss hit ─────────────────────────────────
                    if sl > 0 and current <= sl:
                        log.info(
                            f"  STOP HIT {symbol}  current={current:.4f}  "
                            f"stop={sl:.4f}  entry={entry:.4f}"
                        )
                        self._exit(symbol, pos.qty, current_price=current)
                        continue

                    # ── Take-profit hit ───────────────────────────────
                    if tp > 0 and current >= tp:
                        log.info(
                            f"  TP HIT {symbol}  current={current:.4f}  "
                            f"tp={tp:.4f}  entry={entry:.4f}"
                        )
                        self._exit(symbol, pos.qty, current_price=current)
                        continue

                    # ── Trailing stop for high-conviction trend/breakout ──
                    if meta["trailing"]:
                        gain = (current - entry) / entry
                        if gain >= 0.05:  # 5% gain → start trailing
                            from strategy_ide.data.crypto_fetcher import fetch_crypto_bars
                            try:
                                df = fetch_crypto_bars(symbol, resolution="60", n_bars=20)
                                if not df.empty:
                                    import pandas_ta as ta
                                    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
                                    if atr is not None and not atr.dropna().empty:
                                        trail_stop = current - 3.0 * float(atr.dropna().iloc[-1])
                                        if trail_stop > meta["stop_price"]:
                                            meta["stop_price"] = trail_stop
                                            log.info(
                                                f"  Trailing stop: {symbol} → ${trail_stop:.2f} "
                                                f"(gain={gain*100:.1f}%)"
                                            )
                            except Exception:
                                pass

            except Exception as e:
                log.debug(f"Position monitor error: {e}")

    @staticmethod
    def _last_close(cache, symbol, fallback=float("nan")):
        df = cache.get(symbol)
        if df is not None and not df.empty and "close" in df.columns:
            return float(df["close"].iloc[-1])
        return fallback

    # ── Main poll loop ───────────────────────────────────────────────────────

    # Minimum seconds between polls — prevents rapid-fire trading on restarts
    _MIN_POLL_GAP = 1800  # 30 minutes

    def _poll(self):
        while True:
            if self._maybe_sleep_quiet_window():
                continue

            # ── Guard: enforce minimum gap between polls ─────────────────────
            now = time.monotonic()
            elapsed = now - self._last_poll_time if self._last_poll_time else float("inf")
            if elapsed < self._MIN_POLL_GAP:
                wait = self._MIN_POLL_GAP - elapsed
                log.info(f"─── Poll too soon ({elapsed:.0f}s since last) — waiting {wait:.0f}s")
                time.sleep(wait)
            self._last_poll_time = time.monotonic()

            log.info("─── Polling crypto signals ─────────────────────────")

            # Tick cooldowns
            for symbol in list(self._cooldowns):
                self._cooldowns[symbol] += 1

            positions = self._safe_open_positions()
            positions = {_normalize_symbol(s): p for s, p in positions.items() if _is_crypto(s)}
            held: Set[str] = set(positions.keys())

            universe = self._ranker.get_universe()
            # Also include any pair we currently hold
            all_symbols = list(set(universe) | held)

            log.info(f"Universe: {universe}")
            log.info(f"Held: {list(held)}")

            # ── Regime detection + volatility analysis ────────────────────────
            # Run on all symbols we have cached data for
            for symbol in all_symbols:
                df = self._refresh_bars(symbol)

            regimes = self._regime_detector.detect_per_symbol(self._cache)
            vol_results = self._vol_filter.analyze_universe(self._cache)

            for sym, regime in regimes.items():
                vr = vol_results.get(sym)
                vol_info = f"vol={vr.vol_regime} stop_mult={vr.stop_mult}" if vr else ""
                log.info(f"  {sym}: regime={regime.value}  {vol_info}")

            # Collect signals from all strategies on all pairs
            all_signals: List[Dict] = []
            for symbol in all_symbols:
                df = self._cache.get(symbol, pd.DataFrame())
                if df.empty or len(df) < 50:
                    log.warning(f"  {symbol}: skipped — insufficient bars ({len(df)})")
                    continue

                # Regime gating: only run strategies appropriate for detected regime
                regime = regimes.get(symbol, Regime.CHOPPY)
                allowed = REGIME_STRATEGIES.get(regime, set())

                # Vol filter: block entries if volatility is extreme
                vr = vol_results.get(symbol)
                if vr and not vr.allow_entry:
                    log.info(f"  {symbol}: BLOCKED by vol filter (vol_ratio={vr.vol_ratio:.2f}, regime={vr.vol_regime})")
                    # Still allow exits for held positions by keeping exit-capable strategies
                    if symbol in held:
                        allowed = {s.name for s in self.strategies}  # allow exit signals through
                    else:
                        continue

                stop_mult = vr.stop_mult if vr else 1.0
                signals = self._evaluate_all_strategies(symbol, df, allowed_strategies=allowed, stop_mult=stop_mult)
                all_signals.extend(signals)

            # Arbitrate
            equity = self._equity()
            arbitrator = SignalArbitrator(account_equity=equity)
            actions = arbitrator.arbitrate(all_signals, held, self._cooldowns)

            # Execute
            for action in actions:
                symbol = action["symbol"]
                if action["action"] == "exit" and symbol in positions:
                    price = self._last_close(self._cache, symbol)
                    self._exit(symbol, positions[symbol].qty, current_price=price)
                elif action["action"] == "enter" and symbol not in positions:
                    if self._has_pending_order(symbol):
                        log.info(f"  {symbol}: skipped — pending order exists")
                        continue
                    # Dedup: don't re-enter a symbol we entered less than 1 hour ago
                    last_entry = self._recent_entries.get(symbol, 0)
                    if time.time() - last_entry < 3600:
                        log.info(f"  {symbol}: skipped — entered {time.time() - last_entry:.0f}s ago (< 1h)")
                        continue
                    price = self._last_close(self._cache, symbol, fallback=action["entry_price"])
                    self._enter(symbol, price, action)
                    self._recent_entries[symbol] = time.time()

            log.info(f"─── Sleeping {self.poll_interval}s ─────────────────────")
            time.sleep(self.poll_interval)

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self):
        mode = "PAPER" if PAPER else "LIVE"
        strat_names = [s.name for s in self.strategies]
        log.info(f"\n{'━'*60}")
        log.info(f"  Multi-Strategy Crypto Scanner  [{mode}]  1h bars")
        log.info(f"  Strategies : {strat_names}")
        log.info(f"  Data       : Alpaca CryptoHistoricalDataClient")
        log.info(f"  Exec       : Alpaca Trading API")
        log.info(f"  Polls      : every {self.poll_interval}s")
        log.info(f"{'━'*60}\n")

        # Initial universe refresh (blocking)
        log.info("Running initial universe ranking...")
        self._ranker.refresh_now()
        universe = self._ranker.get_universe()
        if universe:
            self._warmup(universe)
        else:
            log.info("No initial universe — ranker will retry in background")

        # Start background threads
        self._ranker.start()

        threading.Thread(
            target=self._position_monitor_loop, daemon=True, name="position-monitor"
        ).start()
        log.info("Position monitor thread started\n")

        self._poll()
