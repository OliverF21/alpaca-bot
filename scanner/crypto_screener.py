"""
scanner/crypto_screener.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
Crypto universe screener for 24/7 mean reversion setups.

Mirrors MeanReversionScreener in screener.py but tuned for crypto:
  - Uses fetch_crypto_bars_bulk instead of fetch_bars_bulk
  - No min_price filter (BTC=$60k, DOGE=$0.08 — range is meaningless)
  - Volume filter: 1.3× SMA20 (24/7 volume inflates average)
  - lookback_bars=100 (100 × 1hr = ~4 days — enough for warmup)
  - Score function identical: composite RSI + %B + volume (lower = stronger)

Pairs supported by Alpaca:
  BTC/USD, ETH/USD, SOL/USD, LINK/USD, AAVE/USD, AVAX/USD,
  DOGE/USD, LTC/USD, UNI/USD, DOT/USD, MATIC/USD, XRP/USD
"""

import logging
import os
import sys
from typing import Dict, List, Optional

import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta
from dotenv import load_dotenv

_REPO         = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv()
load_dotenv(os.path.join(_STRATEGY_IDE, ".env"))

try:
    from strategy_ide.data.crypto_fetcher import fetch_crypto_bars_bulk
except ImportError:
    from data.crypto_fetcher import fetch_crypto_bars_bulk

log = logging.getLogger(__name__)


# ── Watchlist ─────────────────────────────────────────────────────────────────

CRYPTO_WATCHLIST = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "LINK/USD",
    "AAVE/USD",
    "AVAX/USD",
    "DOGE/USD",
    "LTC/USD",
    "UNI/USD",
    "DOT/USD",
    "MATIC/USD",
    "XRP/USD",
]


# ── Screener ──────────────────────────────────────────────────────────────────

class CryptoMeanReversionScreener:
    """
    Scans CRYPTO_WATCHLIST for pairs showing mean reversion setups on 1h bars.

    Parameters
    ----------
    watchlist : list[str]
        Crypto pairs in slash format ("BTC/USD").
    bb_window : int
        Bollinger Band window.
    bb_std : float
        Bollinger Band std dev.
    rsi_window : int
        RSI window.
    max_rsi : float
        Maximum RSI for a qualifying entry (pre-filter, slightly loose).
    min_volume_ratio : float
        Minimum ratio of latest bar volume to 20-bar avg.
        1.3 is used (vs equity 1.1) because 24/7 trading inflates the avg.
    max_candidates : int
        Max pairs to return per scan, ranked by signal strength.
    lookback_bars : int
        How many 1h bars to fetch per pair for indicator warmup.
        100 bars ≈ 4 days of 1h data.
    """

    def __init__(
        self,
        watchlist: List[str]    = CRYPTO_WATCHLIST,
        bb_window: int          = 20,
        bb_std: float           = 2.0,
        rsi_window: int         = 14,
        max_rsi: float          = 35.0,       # slightly looser than strategy's 28
        min_volume_ratio: float = 1.3,        # 24/7 volume inflates SMA — need real spike
        max_candidates: int     = 6,
        lookback_bars: int      = 100,        # 100 × 1h ≈ 4 days
    ):
        self.watchlist        = watchlist
        self.bb_window        = bb_window
        self.bb_std           = bb_std
        self.rsi_window       = rsi_window
        self.max_rsi          = max_rsi
        self.min_volume_ratio = min_volume_ratio
        self.max_candidates   = max_candidates
        self.lookback_bars    = lookback_bars

    # ── Data fetch ────────────────────────────────────────────────────────────

    def _fetch_bars(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        try:
            result = fetch_crypto_bars_bulk(
                symbols,
                resolution="60",
                n_bars=self.lookback_bars,
            )
            min_bars = self.bb_window + self.rsi_window
            return {s: df for s, df in result.items() if len(df) >= min_bars}
        except Exception as e:
            log.warning(f"Crypto bulk fetch failed: {e}")
            return {}

    # ── Score a single pair ───────────────────────────────────────────────────

    def _score(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Score a pair's setup quality. Returns None if it doesn't qualify.
        Score is composite (lower = stronger setup):
            - How far below the lower band (bb_pct_b)
            - How oversold RSI is           (distance below max_rsi)
            - Volume confirmation           (ratio vs 20-bar avg)
        """
        try:
            bb = ta.bbands(df["close"], length=self.bb_window, std=self.bb_std)
            if bb is None or bb.empty:
                return None

            df = df.copy()
            # Version-safe prefix search (same pattern as crypto_mean_reversion.py)
            prefix_l = f"BBL_{self.bb_window}_"
            prefix_m = f"BBM_{self.bb_window}_"
            prefix_u = f"BBU_{self.bb_window}_"
            cols     = bb.columns.tolist()
            col_l = next((c for c in cols if c.startswith(prefix_l)), None)
            col_m = next((c for c in cols if c.startswith(prefix_m)), None)
            col_u = next((c for c in cols if c.startswith(prefix_u)), None)
            if col_l and col_m and col_u:
                df["bb_lower"] = bb[col_l]
                df["bb_mid"]   = bb[col_m]
                df["bb_upper"] = bb[col_u]
            else:
                df["bb_lower"] = bb.iloc[:, 0]
                df["bb_mid"]   = bb.iloc[:, 1]
                df["bb_upper"] = bb.iloc[:, 2]

            df["rsi"]     = ta.rsi(df["close"], length=self.rsi_window)
            df["vol_sma"] = df["volume"].rolling(20).mean()

            latest = df.iloc[-1]

            # No min_price filter — crypto price range spans 7+ orders of magnitude
            if pd.isna(latest["rsi"]) or pd.isna(latest["bb_lower"]):
                return None
            if latest["rsi"] > self.max_rsi:
                return None
            if latest["close"] >= latest["bb_lower"]:
                return None
            if latest["volume"] < latest["vol_sma"] * self.min_volume_ratio:
                return None

            # Signal strength score (lower = stronger setup)
            band_width = latest["bb_upper"] - latest["bb_lower"]
            bb_pct_b   = (
                (latest["close"] - latest["bb_lower"]) / band_width
                if band_width > 0 else 0
            )
            rsi_score = latest["rsi"] / self.max_rsi
            band_score = bb_pct_b
            vol_ratio  = (
                latest["volume"] / latest["vol_sma"]
                if latest["vol_sma"] > 0 else 1
            )
            vol_score       = 1.0 / max(vol_ratio, 1.0)
            composite_score = (rsi_score * 0.4) + (band_score * 0.4) + (vol_score * 0.2)

            return {
                "close":     round(float(latest["close"]),    8),
                "bb_lower":  round(float(latest["bb_lower"]), 8),
                "bb_mid":    round(float(latest["bb_mid"]),   8),
                "bb_upper":  round(float(latest["bb_upper"]), 8),
                "rsi":       round(float(latest["rsi"]),      2),
                "bb_pct_b":  round(float(bb_pct_b),           4),
                "vol_ratio": round(float(vol_ratio),          2),
                "score":     round(float(composite_score),    4),
            }

        except Exception as e:
            log.debug(f"Crypto scoring error: {e}")
            return None

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self) -> List[Dict]:
        """
        Run a full scan of the crypto watchlist.
        Returns candidates ranked by signal strength (best first), max max_candidates.

        Each candidate dict:
        {
            "symbol":    "ETH/USD",
            "close":     2150.0,
            "bb_lower":  2100.0,
            "bb_mid":    2300.0,
            "bb_upper":  2500.0,
            "rsi":       26.8,
            "bb_pct_b":  -0.12,
            "vol_ratio": 1.7,
            "score":     0.28,   ← lower = stronger setup
        }
        """
        log.info(f"Scanning {len(self.watchlist)} crypto pairs for mean reversion setups...")

        bars_by_symbol = self._fetch_bars(self.watchlist)
        candidates: List[Dict] = []

        for symbol, df in bars_by_symbol.items():
            result = self._score(df)
            if result is not None:
                result["symbol"] = symbol
                candidates.append(result)

        candidates.sort(key=lambda x: x["score"])
        top = candidates[:self.max_candidates]

        if top:
            log.info(f"  Found {len(candidates)} crypto setups — returning top {len(top)}:")
            for c in top:
                log.info(
                    f"    {c['symbol']:<10}  close={c['close']:.4g}  "
                    f"rsi={c['rsi']:.1f}  vol_ratio={c['vol_ratio']:.1f}x  "
                    f"score={c['score']:.3f}"
                )
        else:
            log.info("  No qualifying crypto setups found this scan.")

        return top

    def symbols(self) -> List[str]:
        """Convenience: return just the symbol strings from the latest scan."""
        return [c["symbol"] for c in self.scan()]
