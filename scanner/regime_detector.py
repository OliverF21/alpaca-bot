"""
scanner/regime_detector.py
===========================
GMM-based market regime detection for the crypto scanner.

Classifies the current market state into one of three regimes:
  - TRENDING:       directional move with expanding volatility
  - MEAN_REVERTING: range-bound, low momentum, stable volatility
  - CHOPPY:         high volatility but no direction (whipsaw risk)

The scanner uses this to gate which strategies are allowed to fire:
  - Trending     → trend_following, breakout, supertrend
  - Mean-reverting → mean_reversion
  - Choppy       → NO entries (sit on hands)

Based on Gaussian Mixture Models (Ch13 of ML for Trading):
  Features: rolling volatility, momentum, volume ratio
  Trained online: re-fit every N bars on a rolling window.

Thread-safe: the detector is called from the main poll loop only.
"""

import logging
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture

log = logging.getLogger(__name__)


class Regime(Enum):
    TRENDING = "trending"
    MEAN_REVERTING = "mean_reverting"
    CHOPPY = "choppy"


# Strategies allowed in each regime
REGIME_STRATEGIES = {
    Regime.TRENDING:       {"crypto_trend_following", "crypto_breakout", "crypto_supertrend"},
    Regime.MEAN_REVERTING: {"crypto_mean_reversion"},
    Regime.CHOPPY:         set(),  # no entries in choppy markets
}


class RegimeDetector:
    """
    Detects market regime from OHLCV data using a 3-component GMM.

    Parameters
    ----------
    lookback : int
        Number of bars for feature rolling windows (default 20).
    fit_window : int
        Number of bars used to fit the GMM (default 200).
    refit_every : int
        Re-fit the GMM every N calls to detect() (default 24 = ~1 day at 1h bars).
    """

    def __init__(
        self,
        lookback: int = 20,
        fit_window: int = 200,
        refit_every: int = 24,
    ):
        self.lookback = lookback
        self.fit_window = fit_window
        self.refit_every = refit_every

        self._gmm: Optional[GaussianMixture] = None
        self._label_map: dict = {}  # component index → Regime
        self._calls_since_fit = 0

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build regime features from OHLCV data.

        Returns DataFrame with columns: volatility, momentum, vol_ratio
        Rows with NaN are dropped.
        """
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        # Realized volatility: rolling std of log returns
        log_ret = np.log(close / close.shift(1))
        volatility = log_ret.rolling(self.lookback).std()

        # Momentum: rolling return over lookback period
        momentum = close.pct_change(self.lookback)

        # Volume ratio: current volume vs rolling mean
        vol_sma = volume.rolling(self.lookback).mean()
        vol_ratio = volume / vol_sma

        features = pd.DataFrame({
            "volatility": volatility,
            "momentum": momentum,
            "vol_ratio": vol_ratio,
        }, index=df.index).dropna()

        return features

    def _assign_labels(self, gmm: GaussianMixture) -> dict:
        """Map GMM component indices to regime labels based on cluster centers.

        Heuristic:
          - Highest abs(momentum) + highest volatility → TRENDING
          - Lowest abs(momentum) + lowest volatility  → MEAN_REVERTING
          - Everything else                            → CHOPPY
        """
        centers = gmm.means_  # shape (3, 3): [volatility, momentum, vol_ratio]
        n = centers.shape[0]

        # Score each component: trending = high |momentum| + high vol
        trending_score = np.abs(centers[:, 1]) + centers[:, 0]
        # Mean-reverting score: low |momentum| + low vol
        mr_score = -np.abs(centers[:, 1]) - centers[:, 0]

        label_map = {}
        trending_idx = int(np.argmax(trending_score))
        label_map[trending_idx] = Regime.TRENDING

        remaining = [i for i in range(n) if i != trending_idx]
        mr_idx = remaining[int(np.argmax([mr_score[i] for i in remaining]))]
        label_map[mr_idx] = Regime.MEAN_REVERTING

        for i in range(n):
            if i not in label_map:
                label_map[i] = Regime.CHOPPY

        return label_map

    def _fit(self, features: pd.DataFrame):
        """Fit GMM on the feature window."""
        X = features.values
        if len(X) < 50:
            log.warning("RegimeDetector: insufficient data to fit GMM (%d rows)", len(X))
            return

        gmm = GaussianMixture(
            n_components=3,
            covariance_type="full",
            n_init=3,
            random_state=42,
        )
        gmm.fit(X)
        self._gmm = gmm
        self._label_map = self._assign_labels(gmm)
        self._calls_since_fit = 0

        # Log cluster centers for debugging
        for idx, regime in self._label_map.items():
            c = gmm.means_[idx]
            log.info(
                f"  Regime cluster {regime.value}: "
                f"vol={c[0]:.4f}  momentum={c[1]:.4f}  vol_ratio={c[2]:.2f}"
            )

    def detect(self, df: pd.DataFrame) -> Regime:
        """Detect the current regime from the latest OHLCV data.

        Parameters
        ----------
        df : DataFrame
            OHLCV data with at least fit_window + lookback rows.

        Returns
        -------
        Regime enum value.
        """
        features = self._build_features(df)
        if features.empty:
            log.warning("RegimeDetector: no features — defaulting to CHOPPY")
            return Regime.CHOPPY

        # Re-fit periodically
        self._calls_since_fit += 1
        if self._gmm is None or self._calls_since_fit >= self.refit_every:
            fit_data = features.iloc[-self.fit_window:]
            self._fit(fit_data)

        if self._gmm is None:
            return Regime.CHOPPY

        # Predict regime for the latest bar
        latest = features.iloc[[-1]].values
        component = int(self._gmm.predict(latest)[0])
        regime = self._label_map.get(component, Regime.CHOPPY)

        return regime

    def detect_per_symbol(self, cache: dict) -> dict:
        """Run regime detection on each symbol's cached data.

        Parameters
        ----------
        cache : dict
            symbol → DataFrame of OHLCV bars.

        Returns
        -------
        dict of symbol → Regime.  Symbols with insufficient data get CHOPPY.
        """
        regimes = {}
        for symbol, df in cache.items():
            if df is None or df.empty or len(df) < self.fit_window:
                regimes[symbol] = Regime.CHOPPY
                continue
            try:
                regimes[symbol] = self.detect(df)
            except Exception as e:
                log.warning(f"  RegimeDetector: {symbol} failed — {e}")
                regimes[symbol] = Regime.CHOPPY
        return regimes

    def filter_strategies(self, regime: Regime, strategy_names: list) -> list:
        """Return only strategy names allowed in the given regime."""
        allowed = REGIME_STRATEGIES.get(regime, set())
        return [s for s in strategy_names if s in allowed]
