"""
tests/test_conviction.py
━━━━━━━━━━━━━━━━━━━━━━━━
Unit tests for conviction scoring across all four crypto strategies.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
if str(STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(STRATEGY_IDE))

import numpy as np
import pandas as pd
import pytest


def make_crypto_ohlcv(n_bars: int = 300, seed: int = 42, freq: str = "1h") -> pd.DataFrame:
    """Generate realistic crypto OHLCV data with enough bars for indicator warmup."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n_bars, freq=freq)
    close = 100 + np.cumsum(rng.normal(0, 1.5, n_bars))
    close = np.maximum(close, 10)  # floor at $10
    high = close + rng.uniform(0.5, 3.0, n_bars)
    low = close - rng.uniform(0.5, 3.0, n_bars)
    volume = rng.uniform(500_000, 5_000_000, n_bars)
    return pd.DataFrame(
        {"open": close + rng.normal(0, 0.5, n_bars), "high": high,
         "low": low, "close": close, "volume": volume},
        index=idx,
    )


class TestTrendFollowingConviction:

    def test_conviction_column_exists_after_generate_signals(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        assert "conviction" in df.columns, "generate_signals must add 'conviction' column"

    def test_conviction_is_zero_when_no_entry(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        no_entry = df[df["signal"] != 1]
        assert (no_entry["conviction"] == 0.0).all(), "Conviction must be 0 when signal != 1"

    def test_conviction_between_zero_and_one_on_entry(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        # Try multiple seeds to find one that generates an entry
        for seed in range(42, 100):
            df = make_crypto_ohlcv(seed=seed)
            df = strat.populate_indicators(df)
            df = strat.generate_signals(df)
            entries = df[df["signal"] == 1]
            if not entries.empty:
                assert entries["conviction"].between(0.0, 1.0).all(), \
                    f"Conviction must be in [0, 1], got {entries['conviction'].values}"
                return
        pytest.skip("No entry signals generated across seeds 42-99")
