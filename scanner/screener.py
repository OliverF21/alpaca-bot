"""
scanner/screener.py
━━━━━━━━━━━━━━━━━━━
Dynamic universe screener.

Instead of watching a fixed list of symbols, the screener maintains a broad
watchlist and every N minutes scans it for stocks that are:
  1. Actively traded (volume above threshold)
  2. Currently showing a mean reversion setup (near/below lower BB, RSI oversold)

Only symbols passing the screen are polled for signals by the live scanner.
This keeps the signal-to-noise ratio high and avoids wasting position slots
on random tickers.

Flow:
  ┌─────────────────────────────────────────────────────┐
  │  WATCHLIST (broad — 50-200 symbols)                 │
  │    ↓  every screen_interval (default 15 min)        │
  │  SCREENER  — fetch latest 15m bars, score each      │
  │    ↓  passes filter                                 │
  │  CANDIDATES — ranked by signal strength             │
  │    ↓  passed to LiveScanner                         │
  │  ORDERS — bracket entries submitted                 │
  └─────────────────────────────────────────────────────┘
"""

import logging
import os
from typing import Dict, List, Optional

import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta
from dotenv import load_dotenv

try:
    from strategy_ide.data.fetcher import fetch_bars_bulk
except ImportError:
    from data.fetcher import fetch_bars_bulk

load_dotenv()

log = logging.getLogger(__name__)


# ── Predefined watchlists ─────────────────────────────────────────────────────
# Swap WATCHLIST in run_scanner.py to any of these, or define your own.

WATCHLIST_SP100 = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK.B",
    "JPM", "V", "UNH", "XOM", "MA", "LLY", "JNJ", "PG", "HD", "MRK",
    "AVGO", "CVX", "ABBV", "COST", "PEP", "KO", "ADBE", "WMT", "BAC",
    "CRM", "TMO", "NFLX", "ACN", "MCD", "CSCO", "ABT", "LIN", "DHR",
    "TXN", "NEE", "PM", "ORCL", "QCOM", "AMD", "HON", "UPS", "MS",
    "AMGN", "LOW", "INTU", "SPGI", "GS", "CAT", "IBM", "ELV", "SYK",
    "NOW", "ISRG", "AXP", "DE", "BLK", "REGN", "ZTS", "GILD", "MMC",
    "ADI", "VRTX", "MO", "MDLZ", "PLD", "CI", "CME", "BSX", "DUK",
    "SO", "CL", "EQIX", "AON", "ITW", "SHW", "PGR", "WM", "FDX",
    "LRCX", "HCA", "APD", "NSC", "MCO", "KLAC", "TJX", "EMR", "ETN",
]

WATCHLIST_SECTOR_ETFS = [
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE",
    "QQQ", "SPY", "IWM", "DIA", "GLD", "SLV", "USO", "TLT", "HYG", "LQD",
]

WATCHLIST_LARGE_CAP = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "V", "UNH", "XOM", "HD", "BAC",
]


# ── Screener ──────────────────────────────────────────────────────────────────

class MeanReversionScreener:
    """
    Scans a watchlist for stocks exhibiting mean reversion setups on 15m bars.

    Parameters
    ----------
    watchlist : list[str]
        Full universe of symbols to scan.
    bb_window : int
        Bollinger Band window (should match your strategy).
    bb_std : float
        Bollinger Band std dev (should match your strategy).
    rsi_window : int
        RSI window (should match your strategy).
    max_rsi : float
        Maximum RSI to qualify as a candidate (pre-filter, slightly loose).
    min_volume_ratio : float
        Minimum ratio of latest bar volume to 20-bar avg (e.g. 1.2 = 20% above avg).
    min_price : float
        Minimum stock price to avoid penny stocks.
    max_candidates : int
        Max symbols to return per scan (ranked by signal strength).
    lookback_bars : int
        How many 15m bars to fetch per symbol for indicator warmup.
    """

    def __init__(
        self,
        watchlist: List[str]     = WATCHLIST_LARGE_CAP,
        bb_window: int           = 20,
        bb_std: float            = 2.0,
        rsi_window: int          = 14,
        max_rsi: float           = 38.0,      # slightly loose — screener pre-filters
        min_volume_ratio: float  = 1.1,       # 10% above avg volume
        min_price: float         = 5.0,       # exclude penny stocks
        max_candidates: int      = 10,
        lookback_bars: int       = 60,        # 60 x 15m = 15 hours of history
        resolution: str          = "15",      # bar resolution for data fetching
    ):
        self.watchlist         = [s.upper() for s in watchlist]
        self.bb_window         = bb_window
        self.bb_std            = bb_std
        self.rsi_window        = rsi_window
        self.max_rsi           = max_rsi
        self.min_volume_ratio  = min_volume_ratio
        self.min_price         = min_price
        self.max_candidates    = max_candidates
        self.lookback_bars     = lookback_bars
        self.resolution        = resolution

    # ── Data fetch (Finnhub REST — rate-limited) ───────────────────────────────

    def _fetch_bars(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        """Batch-fetch 15m bars from Finnhub. Rate limit: 1.1s between symbols."""
        try:
            result = fetch_bars_bulk(
                symbols,
                resolution=self.resolution,
                n_bars=self.lookback_bars,
            )
            return {s: df for s, df in result.items() if len(df) >= self.bb_window + self.rsi_window}
        except Exception as e:
            log.warning(f"Batch fetch failed: {e}")
            return {}

    # ── Score a single symbol ─────────────────────────────────────────────────

    def _score(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Score a symbol's setup quality. Returns None if it doesn't qualify.

        Score is composite (lower = stronger setup):
            - How far below the lower band  (bb_pct_b, negative = below band)
            - How oversold RSI is           (distance below max_rsi)
            - Volume confirmation           (ratio vs 20-bar avg)
        """
        try:
            bb = ta.bbands(df["close"], length=self.bb_window, std=self.bb_std)
            if bb is None or bb.empty:
                return None

            df = df.copy()
            # BUG 3 FIX: prefix search, immune to pandas_ta version differences
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
            df["rsi"]      = ta.rsi(df["close"], length=self.rsi_window)
            df["vol_sma"]  = df["volume"].rolling(20).mean()

            latest = df.iloc[-1]

            # Hard filters
            if latest["close"] < self.min_price:
                return None
            if pd.isna(latest["rsi"]) or pd.isna(latest["bb_lower"]):
                return None
            if latest["rsi"] > self.max_rsi:
                return None
            if latest["close"] >= latest["bb_lower"]:
                return None
            if latest["volume"] < latest["vol_sma"] * self.min_volume_ratio:
                return None

            # Signal strength score (lower = better setup)
            band_width  = latest["bb_upper"] - latest["bb_lower"]
            bb_pct_b    = (latest["close"] - latest["bb_lower"]) / band_width if band_width > 0 else 0
            rsi_score   = latest["rsi"] / self.max_rsi          # lower RSI = lower score
            band_score  = bb_pct_b                               # more negative = lower score
            vol_ratio   = latest["volume"] / latest["vol_sma"] if latest["vol_sma"] > 0 else 1

            # BUG 7 FIX: include volume in the ranking score.
            # A stock with 3x normal volume is a stronger setup than one with 1.1x.
            # We invert vol_ratio (1/vol) so higher volume = smaller (better) score.
            vol_score = 1.0 / max(vol_ratio, 1.0)
            composite_score = (rsi_score * 0.4) + (band_score * 0.4) + (vol_score * 0.2)

            return {
                "close":      round(float(latest["close"]),     2),
                "bb_lower":   round(float(latest["bb_lower"]),  2),
                "bb_mid":     round(float(latest["bb_mid"]),    2),
                "bb_upper":   round(float(latest["bb_upper"]),  2),
                "rsi":        round(float(latest["rsi"]),       2),
                "bb_pct_b":   round(float(bb_pct_b),            4),
                "vol_ratio":  round(float(vol_ratio),           2),
                "score":      round(float(composite_score),     4),
            }

        except Exception as e:
            log.debug(f"Scoring error: {e}")
            return None

    # ── Main scan ─────────────────────────────────────────────────────────────

    def scan(self) -> List[Dict]:
        """
        Run a full scan of the watchlist.
        Returns a list of candidate dicts ranked by signal strength (best first).

        Each candidate dict:
        {
            "symbol":   "AAPL",
            "close":    172.34,
            "bb_lower": 171.20,
            "bb_mid":   175.80,
            "rsi":      29.4,
            "bb_pct_b": -0.08,
            "vol_ratio": 1.4,
            "score":    0.31,    ← lower = stronger setup
        }
        """
        log.info(f"Scanning {len(self.watchlist)} symbols for mean reversion setups...")

        bars_by_symbol = self._fetch_bars(self.watchlist)
        candidates     = []

        for symbol, df in bars_by_symbol.items():
            result = self._score(df)
            if result is not None:
                result["symbol"] = symbol
                candidates.append(result)

        # Rank by composite score ascending (strongest setups first)
        candidates.sort(key=lambda x: x["score"])
        top = candidates[:self.max_candidates]

        if top:
            log.info(f"  Found {len(candidates)} setups — returning top {len(top)}:")
            for c in top:
                log.info(
                    f"    {c['symbol']:<6}  close={c['close']:.2f}  "
                    f"bb_lower={c['bb_lower']:.2f}  rsi={c['rsi']:.1f}  "
                    f"vol_ratio={c['vol_ratio']:.1f}x  score={c['score']:.3f}"
                )
        else:
            log.info("  No qualifying setups found this scan.")

        return top

    def symbols(self) -> List[str]:
        """Convenience: return just the symbol strings from the latest scan."""
        return [c["symbol"] for c in self.scan()]
