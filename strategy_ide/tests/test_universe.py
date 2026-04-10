"""
tests/test_universe.py
━━━━━━━━━━━━━━━━━━━━━━
Unit tests for Dynamic Universe Ranker.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
REPO_ROOT = STRATEGY_IDE.parent
for p in [str(STRATEGY_IDE), str(REPO_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
import pandas as pd
import pytest

from scanner.crypto_universe import rank_universe, FALLBACK_PAIRS


def make_pair_data(n_bars: int = 168, atr_pct: float = 0.03,
                   avg_volume: float = 1_000_000, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base_price = 100.0
    close = base_price + np.cumsum(rng.normal(0, base_price * atr_pct * 0.1, n_bars))
    close = np.maximum(close, 10)
    high = close * (1 + rng.uniform(0, atr_pct, n_bars))
    low = close * (1 - rng.uniform(0, atr_pct, n_bars))
    volume = rng.uniform(avg_volume * 0.5, avg_volume * 1.5, n_bars)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.date_range("2024-01-01", periods=n_bars, freq="1h"),
    )


class TestRankUniverse:

    def test_returns_list_of_strings(self):
        bars = {
            "BTC/USD": make_pair_data(atr_pct=0.04, avg_volume=5_000_000, seed=1),
            "ETH/USD": make_pair_data(atr_pct=0.03, avg_volume=3_000_000, seed=2),
            "DOGE/USD": make_pair_data(atr_pct=0.05, avg_volume=1_000_000, seed=3),
        }
        result = rank_universe(bars, top_k=2)
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)
        assert len(result) == 2

    def test_higher_volatility_ranks_higher(self):
        bars = {
            "HIGH_VOL/USD": make_pair_data(atr_pct=0.08, avg_volume=2_000_000, seed=1),
            "LOW_VOL/USD": make_pair_data(atr_pct=0.01, avg_volume=2_000_000, seed=2),
        }
        result = rank_universe(bars, top_k=2)
        assert result[0] == "HIGH_VOL/USD", "Higher volatility pair should rank first"

    def test_filters_low_volume_pairs(self):
        bars = {
            "GOOD/USD": make_pair_data(atr_pct=0.03, avg_volume=500_000, seed=1),
            "DEAD/USD": make_pair_data(atr_pct=0.03, avg_volume=1_000, seed=2),
        }
        result = rank_universe(bars, top_k=2, min_daily_dollar_volume=50_000)
        assert "DEAD/USD" not in result

    def test_respects_top_k(self):
        bars = {f"PAIR{i}/USD": make_pair_data(seed=i) for i in range(10)}
        result = rank_universe(bars, top_k=5)
        assert len(result) <= 5

    def test_empty_input_returns_empty(self):
        result = rank_universe({}, top_k=8)
        assert result == []

    def test_fallback_pairs_are_defined(self):
        assert len(FALLBACK_PAIRS) == 6
        assert "BTC/USD" in FALLBACK_PAIRS


class TestMinThresholds:

    def test_atr_pct_filter(self):
        bars = {
            "VOLATILE/USD": make_pair_data(atr_pct=0.05, avg_volume=500_000, seed=1),
            "FLAT/USD": make_pair_data(atr_pct=0.001, avg_volume=500_000, seed=2),
        }
        result = rank_universe(bars, top_k=2, min_atr_pct=0.01)
        assert "FLAT/USD" not in result
