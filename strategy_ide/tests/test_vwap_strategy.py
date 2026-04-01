"""
tests/test_vwap_strategy.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests for the VWAP Reversion strategy.

Run:
    python -m pytest strategy_ide/tests/test_vwap_strategy.py -v
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
if str(STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(STRATEGY_IDE))

import numpy as np
import pandas as pd
import pytest

from strategies.vwap_reversion import VWAPReversionStrategy
from backtester.engine import run_backtest


# ── helpers ──────────────────────────────────────────────────────────────────

def make_intraday_ohlcv(
    n_days: int = 5,
    bars_per_day: int = 78,   # 78 × 5min = 6.5 hours
    base_price: float = 100.0,
    volatility: float = 0.002,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Generate realistic multi-day intraday OHLCV with 5-min bars.
    Includes intraday mean-reverting dips to trigger VWAP reversion entries.
    """
    rng = np.random.default_rng(seed)
    total_bars = n_days * bars_per_day
    dates = []
    for d in range(n_days):
        day = pd.Timestamp("2024-06-03") + pd.Timedelta(days=d)
        # Skip weekends
        while day.weekday() >= 5:
            day += pd.Timedelta(days=1)
        start = day.replace(hour=9, minute=30)
        dates.extend(
            [start + pd.Timedelta(minutes=5 * i) for i in range(bars_per_day)]
        )

    idx = pd.DatetimeIndex(dates[:total_bars])
    # Price: random walk with slight upward drift and periodic dips
    returns = rng.normal(0.0001, volatility, total_bars)
    # Inject dips in the middle of each day to trigger entries
    for d in range(n_days):
        dip_start = d * bars_per_day + 30  # ~2.5 hours in
        dip_end = dip_start + 5
        if dip_end < total_bars:
            returns[dip_start:dip_end] = -volatility * 3  # strong dip
            returns[dip_end:dip_end + 8] = volatility * 2  # recovery

    close = base_price * np.exp(np.cumsum(returns))
    high = close * (1 + rng.uniform(0, volatility, total_bars))
    low = close * (1 - rng.uniform(0, volatility, total_bars))
    volume = rng.uniform(500_000, 2_000_000, total_bars)

    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ── tests ────────────────────────────────────────────────────────────────────

class TestVWAPIndicators:
    """VWAP and bands should be computed correctly."""

    def test_vwap_column_exists(self):
        df = make_intraday_ohlcv()
        strat = VWAPReversionStrategy()
        df = strat.populate_indicators(df)
        assert "vwap" in df.columns
        assert "vwap_lower" in df.columns
        assert "vwap_upper" in df.columns
        assert "rsi" in df.columns

    def test_vwap_resets_daily(self):
        """VWAP should reset each day — first bar's VWAP ≈ that bar's typical price."""
        df = make_intraday_ohlcv(n_days=3)
        strat = VWAPReversionStrategy()
        df = strat.populate_indicators(df)

        # Group by date, check first bar's VWAP equals its typical price
        for _, group in df.groupby(df.index.date):
            first = group.iloc[0]
            tp = (first["high"] + first["low"] + first["close"]) / 3.0
            assert abs(first["vwap"] - tp) < 0.01, (
                f"First bar VWAP {first['vwap']:.2f} should ≈ typical price {tp:.2f}"
            )

    def test_bar_of_day_starts_at_1(self):
        df = make_intraday_ohlcv(n_days=2)
        strat = VWAPReversionStrategy()
        df = strat.populate_indicators(df)
        for _, group in df.groupby(df.index.date):
            assert group["bar_of_day"].iloc[0] == 1


class TestVWAPSignals:
    """Strategy should generate entry/exit signals."""

    def test_signals_column_exists(self):
        df = make_intraday_ohlcv()
        strat = VWAPReversionStrategy()
        df = strat.run(df)
        assert "signal" in df.columns
        assert set(df["signal"].unique()).issubset({-1, 0, 1})

    def test_generates_entries_on_dips(self):
        """With injected dips, the strategy should produce at least some entries."""
        df = make_intraday_ohlcv(n_days=10, volatility=0.003, seed=123)
        strat = VWAPReversionStrategy(buy_rsi=45, vwap_dev_mult=1.0)
        df = strat.run(df)
        entries = (df["signal"] == 1).sum()
        assert entries > 0, "Expected at least one entry signal on dip data"

    def test_no_entries_in_first_hour(self):
        """Entries should be suppressed before min_bars_in_day."""
        df = make_intraday_ohlcv(n_days=5)
        strat = VWAPReversionStrategy(min_bars_in_day=12)
        df = strat.run(df)
        df_with_bod = strat.populate_indicators(make_intraday_ohlcv(n_days=5))
        early_entries = df[(df["signal"] == 1)].index
        for ts in early_entries:
            day_start = ts.replace(hour=9, minute=30)
            minutes_in = (ts - day_start).total_seconds() / 60
            assert minutes_in >= 55, (
                f"Entry at {ts} is too early ({minutes_in:.0f} min into day)"
            )


class TestVWAPBacktest:
    """Integration: VWAP strategy should produce trades through the backtester."""

    def test_produces_trades(self):
        df = make_intraday_ohlcv(n_days=15, volatility=0.003, seed=99)
        strat = VWAPReversionStrategy(buy_rsi=45, vwap_dev_mult=1.0)
        result = run_backtest(df, strat, initial_capital=100_000, risk_pct=0.01)
        assert len(result.trades) >= 1, (
            f"Expected trades, got {len(result.trades)}"
        )

    def test_equity_curve_length(self):
        df = make_intraday_ohlcv(n_days=5)
        strat = VWAPReversionStrategy()
        result = run_backtest(df, strat, initial_capital=100_000)
        assert len(result.equity_curve) == len(df)
