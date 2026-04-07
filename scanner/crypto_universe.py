"""
scanner/crypto_universe.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
Dynamic Universe Ranker for crypto multi-strategy scanner.

Scores all available crypto pairs by trailing volatility (ATR%) and dollar
volume, returning the top K pairs worth scanning.
"""

import logging
import os
import sys
import threading
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta

_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger(__name__)

FALLBACK_PAIRS: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOGE/USD",
]

ALL_CRYPTO_PAIRS: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AAVE/USD", "AVAX/USD",
    "DOGE/USD", "LTC/USD", "UNI/USD", "DOT/USD", "MATIC/USD", "XRP/USD",
]


def rank_universe(
    bars_by_symbol: Dict[str, pd.DataFrame],
    top_k: int = 8,
    min_daily_dollar_volume: float = 50_000.0,
    min_atr_pct: float = 0.01,
) -> List[str]:
    if not bars_by_symbol:
        return []

    scores = []
    for symbol, df in bars_by_symbol.items():
        if df.empty or len(df) < 20:
            continue

        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        if atr is None or atr.dropna().empty:
            continue
        latest_atr = float(atr.dropna().iloc[-1])
        latest_close = float(df["close"].iloc[-1])
        atr_pct = latest_atr / latest_close if latest_close > 0 else 0

        dollar_vol = df["volume"].rolling(20).mean()
        if dollar_vol.dropna().empty:
            continue
        avg_daily_dollar_vol = float(dollar_vol.dropna().iloc[-1])

        if avg_daily_dollar_vol < min_daily_dollar_volume:
            continue
        if atr_pct < min_atr_pct:
            continue

        scores.append({
            "symbol": symbol,
            "atr_pct": atr_pct,
            "dollar_vol": avg_daily_dollar_vol,
        })

    if not scores:
        return []

    scores_df = pd.DataFrame(scores)
    scores_df["vol_rank"] = scores_df["atr_pct"].rank(pct=True)
    scores_df["dvol_rank"] = scores_df["dollar_vol"].rank(pct=True)
    scores_df["composite"] = 0.6 * scores_df["vol_rank"] + 0.4 * scores_df["dvol_rank"]
    scores_df = scores_df.sort_values("composite", ascending=False)
    return scores_df["symbol"].head(top_k).tolist()


class UniverseRanker:
    def __init__(
        self,
        refresh_interval: int = 1800,
        top_k: int = 8,
        pairs: Optional[List[str]] = None,
    ):
        self.refresh_interval = refresh_interval
        self.top_k = top_k
        self._all_pairs = pairs or ALL_CRYPTO_PAIRS
        self._universe: List[str] = list(FALLBACK_PAIRS)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def get_universe(self) -> List[str]:
        with self._lock:
            return list(self._universe)

    def _refresh(self):
        try:
            from data.crypto_fetcher import fetch_crypto_bars_bulk
        except ImportError:
            from strategy_ide.data.crypto_fetcher import fetch_crypto_bars_bulk

        try:
            bars = fetch_crypto_bars_bulk(self._all_pairs, resolution="60", n_bars=168)
            ranked = rank_universe(bars, top_k=self.top_k)
            if ranked:
                with self._lock:
                    self._universe = ranked
                log.info(f"Universe refreshed: {ranked}")
            else:
                log.warning("Universe ranker returned empty — keeping previous universe")
        except Exception as e:
            log.error(f"Universe refresh failed: {e} — keeping previous universe")

    def _loop(self):
        while True:
            self._refresh()
            time.sleep(self.refresh_interval)

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="universe-ranker"
        )
        self._thread.start()
        log.info(f"Universe ranker started (refresh every {self.refresh_interval}s, top {self.top_k})")

    def refresh_now(self):
        self._refresh()
