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

from scanner.crypto_universe import UniverseRanker
from scanner.signal_arbitrator import SignalArbitrator, COOLDOWN_BARS

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

        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
        self._ranker = UniverseRanker(
            refresh_interval=universe_refresh,
            top_k=universe_top_k,
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

    def _evaluate_all_strategies(self, symbol: str, df: pd.DataFrame) -> List[Dict]:
        """Run all strategies on a pair's data, return list of signal dicts."""
        results = []
        for strat in self.strategies:
            try:
                df_copy = df.copy()
                df_copy = strat.populate_indicators(df_copy)
                df_copy = strat.generate_signals(df_copy)

                last = df_copy.iloc[-1]
                sig_val = int(last["signal"])
                signal_str = "enter" if sig_val == 1 else "exit" if sig_val == -1 else "hold"
                conviction = float(last.get("conviction", 0.0))

                results.append({
                    "symbol": symbol,
                    "signal": signal_str,
                    "conviction": conviction,
                    "strategy": strat.name,
                    "stop_price": float(last.get("stop_price", 0)) if not pd.isna(last.get("stop_price", float("nan"))) else 0,
                    "take_profit_price": float(last.get("take_profit_price", 0)) if not pd.isna(last.get("take_profit_price", float("nan"))) else 0,
                    "entry_price": float(last["close"]),
                })
            except Exception as e:
                log.debug(f"  {symbol}/{strat.name}: strategy error — {e}")
        return results

    # ── Order execution ──────────────────────────────────────────────────────

    def _qty(self, price: float, risk_pct: float, stop_price: float) -> float:
        equity = self._equity()
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

        qty = self._qty(price, risk_pct, stop_price)
        sl = round(stop_price, 8)
        tp = round(tp_price, 8)

        # Cap at 20% of equity
        max_notional = self._equity() * 0.20
        if qty * price > max_notional:
            qty = round(max_notional / price, 6)

        log.info(
            f"  ▶ ENTER {symbol:<10}  strategy={action['strategy']}  "
            f"conviction={action['conviction']:.2f}  price={price}  "
            f"qty={qty}  sl={sl}  tp={tp}  risk={risk_pct*100:.0f}%"
        )
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,
            order_class   = "bracket",
            stop_loss     = {"stop_price": sl},
            take_profit   = {"limit_price": tp},
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
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty_float,
            side          = OrderSide.SELL,
            time_in_force = TimeInForce.GTC,
        ))
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
        """Check stops/TPs every 5 minutes between polls."""
        while True:
            time.sleep(300)
            try:
                positions = self._safe_open_positions()
                positions = {s: p for s, p in positions.items() if "/" in s}

                for symbol, pos in positions.items():
                    meta = self._position_meta.get(symbol)
                    if not meta:
                        continue

                    current = float(pos.current_price)
                    entry = meta["entry_price"]

                    # Trailing stop for high-conviction trend/breakout entries
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

    # ── Main poll loop ───────────────────────────────────────────────────────

    def _poll(self):
        while True:
            if self._maybe_sleep_quiet_window():
                continue

            log.info("─── Polling crypto signals ─────────────────────────")

            # Tick cooldowns
            for symbol in list(self._cooldowns):
                self._cooldowns[symbol] += 1

            positions = self._safe_open_positions()
            positions = {s: p for s, p in positions.items() if "/" in s}
            held: Set[str] = set(positions.keys())

            universe = self._ranker.get_universe()
            # Also include any pair we currently hold
            all_symbols = list(set(universe) | held)

            log.info(f"Universe: {universe}")
            log.info(f"Held: {list(held)}")

            # Collect signals from all strategies on all pairs
            all_signals: List[Dict] = []
            for symbol in all_symbols:
                df = self._refresh_bars(symbol)
                if df.empty or len(df) < 50:
                    continue

                signals = self._evaluate_all_strategies(symbol, df)
                all_signals.extend(signals)

                # Log per-pair summary
                for sig in signals:
                    if sig["signal"] != "hold":
                        log.info(
                            f"  [{symbol}] {sig['strategy']}: {sig['signal'].upper()} "
                            f"conviction={sig['conviction']:.2f}"
                        )

            # Arbitrate
            equity = self._equity()
            arbitrator = SignalArbitrator(account_equity=equity)
            actions = arbitrator.arbitrate(all_signals, held, self._cooldowns)

            # Execute
            for action in actions:
                symbol = action["symbol"]
                if action["action"] == "exit" and symbol in positions:
                    price = float(self._cache.get(symbol, pd.DataFrame()).get("close", pd.Series()).iloc[-1]) if symbol in self._cache and not self._cache[symbol].empty else float("nan")
                    self._exit(symbol, positions[symbol].qty, current_price=price)
                elif action["action"] == "enter" and symbol not in positions:
                    if self._has_pending_order(symbol):
                        log.info(f"  {symbol}: skipped — pending order exists")
                        continue
                    price = float(self._cache[symbol]["close"].iloc[-1]) if symbol in self._cache and not self._cache[symbol].empty else action["entry_price"]
                    self._enter(symbol, price, action)

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
