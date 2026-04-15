"""
scanner/vol_filter.py
======================
GARCH(1,1)-based volatility forecasting for adaptive stop sizing
and a "too volatile to trade" gate.

Based on Ch09 of ML for Trading (ARCH/GARCH models).

Usage:
    vf = VolatilityFilter()
    result = vf.analyze(df)   # df = OHLCV with 100+ bars
    result.stop_mult          # multiplier to scale ATR stops
    result.vol_regime         # "low", "normal", "high", "extreme"
    result.allow_entry        # False if vol is extreme

Thread-safe: called from main poll loop only.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Try to import arch; fall back to simple std-based estimation if unavailable
try:
    from arch import arch_model
    _HAS_ARCH = True
except ImportError:
    _HAS_ARCH = False
    log.warning("VolFilter: 'arch' package not installed — using rolling-std fallback")


@dataclass
class VolResult:
    """Result of volatility analysis for a single symbol."""
    current_vol: float       # annualized realized vol
    forecast_vol: float      # 1-step-ahead GARCH forecast (annualized)
    vol_ratio: float         # forecast / long-run average
    vol_regime: str          # "low", "normal", "high", "extreme"
    stop_mult: float         # multiplier for ATR-based stops (1.0 = normal)
    allow_entry: bool        # False if vol is extreme — sit on hands
    forecast_1h_pct: float   # expected 1h move as % of price


class VolatilityFilter:
    """
    GARCH-based volatility filter.

    Parameters
    ----------
    lookback : int
        Rolling window for realized vol (default 24 = 1 day of 1h bars).
    long_lookback : int
        Long-term vol window for normalization (default 168 = 1 week).
    extreme_threshold : float
        Vol ratio above which entries are blocked (default 2.5).
    high_threshold : float
        Vol ratio above which stops are widened (default 1.5).
    low_threshold : float
        Vol ratio below which stops can be tightened (default 0.6).
    min_bars : int
        Minimum bars needed for GARCH fit (default 100).
    """

    def __init__(
        self,
        lookback: int = 24,
        long_lookback: int = 168,
        extreme_threshold: float = 2.5,
        high_threshold: float = 1.5,
        low_threshold: float = 0.6,
        min_bars: int = 100,
    ):
        self.lookback = lookback
        self.long_lookback = long_lookback
        self.extreme_threshold = extreme_threshold
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        self.min_bars = min_bars

    def _garch_forecast(self, returns: pd.Series) -> Optional[float]:
        """Fit GARCH(1,1) and return 1-step-ahead volatility forecast."""
        if not _HAS_ARCH:
            return None

        try:
            # Scale returns to percentage for numerical stability
            scaled = returns * 100
            model = arch_model(
                scaled,
                vol="Garch",
                p=1, q=1,
                mean="Zero",
                rescale=False,
            )
            result = model.fit(disp="off", show_warning=False)
            # Forecast 1-step ahead
            forecast = result.forecast(horizon=1)
            variance = forecast.variance.iloc[-1, 0]
            # Convert back from percentage
            return np.sqrt(variance) / 100
        except Exception as e:
            log.debug(f"GARCH fit failed: {e}")
            return None

    def analyze(self, df: pd.DataFrame) -> VolResult:
        """Analyze volatility for a single symbol's OHLCV data.

        Parameters
        ----------
        df : DataFrame with 'close' column, at least min_bars rows.

        Returns
        -------
        VolResult with regime classification and stop multiplier.
        """
        close = df["close"].astype(float)
        log_ret = np.log(close / close.shift(1)).dropna()

        if len(log_ret) < self.min_bars:
            return VolResult(
                current_vol=0, forecast_vol=0, vol_ratio=1.0,
                vol_regime="normal", stop_mult=1.0, allow_entry=True,
                forecast_1h_pct=0,
            )

        # Realized vol (short window)
        realized_vol = float(log_ret.iloc[-self.lookback:].std())
        # Long-run average vol
        long_vol = float(log_ret.iloc[-self.long_lookback:].std())

        # GARCH forecast (or fallback to realized)
        garch_vol = self._garch_forecast(log_ret.iloc[-self.min_bars:])
        forecast_vol = garch_vol if garch_vol is not None else realized_vol

        # Annualize (1h bars → ~8760 bars/year)
        ann_factor = np.sqrt(8760)
        ann_realized = realized_vol * ann_factor
        ann_forecast = forecast_vol * ann_factor

        # Vol ratio: how far is forecast from long-run average?
        vol_ratio = forecast_vol / long_vol if long_vol > 0 else 1.0

        # Classify regime
        if vol_ratio >= self.extreme_threshold:
            regime = "extreme"
            allow_entry = False
            stop_mult = 2.0  # double-wide stops for held positions
        elif vol_ratio >= self.high_threshold:
            regime = "high"
            allow_entry = True
            stop_mult = 1.5  # widen stops 50%
        elif vol_ratio <= self.low_threshold:
            regime = "low"
            allow_entry = True
            stop_mult = 0.8  # tighten stops 20% in quiet markets
        else:
            regime = "normal"
            allow_entry = True
            stop_mult = 1.0

        # Expected 1h move as pct
        forecast_1h_pct = forecast_vol * 100

        return VolResult(
            current_vol=ann_realized,
            forecast_vol=ann_forecast,
            vol_ratio=round(vol_ratio, 3),
            vol_regime=regime,
            stop_mult=round(stop_mult, 2),
            allow_entry=allow_entry,
            forecast_1h_pct=round(forecast_1h_pct, 3),
        )

    def analyze_universe(self, cache: dict) -> dict:
        """Run vol analysis on each symbol's cached data.

        Parameters
        ----------
        cache : dict of symbol → DataFrame.

        Returns
        -------
        dict of symbol → VolResult.
        """
        results = {}
        for symbol, df in cache.items():
            if df is None or df.empty or len(df) < self.min_bars:
                results[symbol] = VolResult(
                    current_vol=0, forecast_vol=0, vol_ratio=1.0,
                    vol_regime="normal", stop_mult=1.0, allow_entry=True,
                    forecast_1h_pct=0,
                )
                continue
            try:
                results[symbol] = self.analyze(df)
            except Exception as e:
                log.warning(f"  VolFilter: {symbol} failed — {e}")
                results[symbol] = VolResult(
                    current_vol=0, forecast_vol=0, vol_ratio=1.0,
                    vol_regime="normal", stop_mult=1.0, allow_entry=True,
                    forecast_1h_pct=0,
                )
        return results
