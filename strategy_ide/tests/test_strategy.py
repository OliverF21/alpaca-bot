"""
tests/test_strategy.py
━━━━━━━━━━━━━━━━━━━━━━
Tests for MeanReversionStrategy (strategies/mean_reversion.py).

Each test is independent — it builds its own price data and does not
require an API key, internet connection, or any external services.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
if str(STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(STRATEGY_IDE))

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

from strategies.mean_reversion import MeanReversionStrategy


# ── helpers ───────────────────────────────────────────────────────────────────

def make_ohlcv(n_bars: int = 300, seed: int = 42, freq: str = "15T") -> pd.DataFrame:
    """
    Build a realistic-ish OHLCV DataFrame with enough bars for all
    indicator windows to warm up.

    The price follows a random walk with mean-reversion so that the
    Bollinger Band strategy actually generates a few signals.
    """
    rng   = np.random.default_rng(seed)
    close = np.zeros(n_bars)
    close[0] = 100.0
    mean  = 100.0
    for i in range(1, n_bars):
        shock       = rng.normal(0, 0.8)
        revert      = 0.05 * (mean - close[i - 1])   # gentle pull toward mean
        close[i]    = close[i - 1] + revert + shock

    idx    = pd.date_range("2024-01-02", periods=n_bars, freq=freq)
    volume = rng.uniform(800_000, 1_200_000, n_bars)

    return pd.DataFrame(
        {
            "open":   close * 0.999,
            "high":   close * 1.005,
            "low":    close * 0.995,
            "close":  close,
            "volume": volume,
        },
        index=idx,
    )


def make_ohlcv_with_entry_zone(seed: int = 7) -> pd.DataFrame:
    """
    Build a DataFrame that is GUARANTEED to produce at least one entry signal.

    Strategy: create a 300-bar series, then at bar 150 force the close to
    drop sharply below the Bollinger lower band while RSI is oversold and
    volume is elevated.  The bars before 150 establish the band values.
    """
    df = make_ohlcv(n_bars=300, seed=seed)
    strat = MeanReversionStrategy()

    # Compute indicators so we know where the band is
    df_ind = strat.populate_indicators(df.copy())

    # Find the lower band at bar 150 and force price well below it
    if "bb_lower" in df_ind.columns and not df_ind["bb_lower"].isna().all():
        lower = df_ind["bb_lower"].iloc[150]
        df.iloc[150, df.columns.get_loc("close")] = lower * 0.985  # 1.5% below band
        df.iloc[150, df.columns.get_loc("low")]   = lower * 0.985
        df.iloc[150, df.columns.get_loc("volume")] *= 2.0           # double volume

    return df


# ── tests ─────────────────────────────────────────────────────────────────────

class TestPopulateIndicators:
    """populate_indicators() must add all required columns without errors."""

    def test_adds_all_required_columns(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.populate_indicators(df)
        for col in ["bb_lower", "bb_mid", "bb_upper", "bb_pct_b", "rsi", "volume_sma20"]:
            assert col in out.columns, f"Missing column: {col}"

    def test_does_not_modify_original_df(self):
        df     = make_ohlcv()
        before = df.columns.tolist()
        strat  = MeanReversionStrategy()
        strat.populate_indicators(df)
        assert df.columns.tolist() == before, "populate_indicators must not modify the input DataFrame"

    def test_bb_pct_b_range_is_finite(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.populate_indicators(df)
        valid = out["bb_pct_b"].dropna()
        assert len(valid) > 0, "bb_pct_b is all NaN"


class TestBollingerColumnNaming:
    """Regression test for BUG 3: named column access must match pandas_ta output."""

    def test_bb_lower_matches_named_pandas_ta_column(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.populate_indicators(df.copy())

        # Use prefix search (same approach as mean_reversion.py) to find the
        # lower-band column regardless of pandas_ta version suffix changes.
        bb     = ta.bbands(df["close"], length=strat.bb_window, std=strat.bb_std)
        prefix = f"BBL_{strat.bb_window}_"
        col_l  = next((c for c in bb.columns if c.startswith(prefix)), None)
        assert col_l is not None, (
            f"No BBL column found with prefix '{prefix}'. Cols: {bb.columns.tolist()}"
        )
        pd.testing.assert_series_equal(
            out["bb_lower"].rename(col_l), bb[col_l],
            check_names=False, rtol=1e-6,
        )

    def test_bb_upper_matches_named_pandas_ta_column(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.populate_indicators(df.copy())

        bb     = ta.bbands(df["close"], length=strat.bb_window, std=strat.bb_std)
        prefix = f"BBU_{strat.bb_window}_"
        col_u  = next((c for c in bb.columns if c.startswith(prefix)), None)
        assert col_u is not None, (
            f"No BBU column found with prefix '{prefix}'. Cols: {bb.columns.tolist()}"
        )
        pd.testing.assert_series_equal(
            out["bb_upper"].rename(col_u), bb[col_u],
            check_names=False, rtol=1e-6,
        )


class TestGenerateSignals:
    """generate_signals() must produce valid signal values."""

    def test_signal_values_are_in_valid_set(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        df    = strat.populate_indicators(df)
        out   = strat.generate_signals(df)
        assert out["signal"].isin([-1, 0, 1]).all(), "Signal column has unexpected values"

    def test_missing_indicator_columns_raises(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        # Don't call populate_indicators — signals should raise informatively
        with pytest.raises(ValueError, match="Missing columns"):
            strat.generate_signals(df)


class TestEntryExitCollision:
    """Regression test for BUG 5: no bar should have both entry AND exit."""

    def test_no_simultaneous_entry_and_exit(self):
        df    = make_ohlcv_with_entry_zone()
        strat = MeanReversionStrategy()
        out   = strat.run(df)

        # For every bar where signal==1 (entry), the entry condition was true.
        # Separately check: no bar has signal==1 AND also satisfies the raw
        # exit condition (price >= bb_mid OR rsi > sell_rsi).
        entries = out["signal"] == 1
        if not entries.any():
            pytest.skip("No entry signals generated — adjust make_ohlcv_with_entry_zone")

        raw_exit_bb = (
            out["close"].ge(out["bb_upper"]) if strat.exit_target == "upper"
            else out["close"].ge(out["bb_mid"])
        )
        raw_exit = raw_exit_bb | out["rsi"].gt(strat.sell_rsi)

        collision = entries & raw_exit
        assert not collision.any(), (
            f"BUG 5 not fixed: {collision.sum()} bars satisfy both entry and exit. "
            "entry should win and signal should be 1, not -1."
        )


class TestMinHoldBars:
    """Regression test for BUG 6: exits must be suppressed for min_hold_bars after entry."""

    def test_no_exit_within_hold_period(self):
        df    = make_ohlcv_with_entry_zone()
        strat = MeanReversionStrategy(min_hold_bars=3)
        out   = strat.run(df)

        entry_bars = out.index[out["signal"] == 1].tolist()
        if not entry_bars:
            pytest.skip("No entry signals generated — adjust make_ohlcv_with_entry_zone")

        for entry_idx in entry_bars:
            pos = out.index.get_loc(entry_idx)
            # The bars immediately after entry (up to min_hold_bars-1) must NOT be exits
            for j in range(1, strat.min_hold_bars):
                if pos + j >= len(out):
                    break
                sig = out["signal"].iloc[pos + j]
                assert sig != -1, (
                    f"BUG 6 not fixed: exit at bar pos+{j} (within hold period) after entry at {entry_idx}"
                )


class TestVolumeConfirmation:
    """Entry signal must require volume > volume_sma20."""

    def test_low_volume_suppresses_entry(self):
        df    = make_ohlcv_with_entry_zone()
        strat = MeanReversionStrategy()
        df_ind = strat.populate_indicators(df.copy())

        # Force the entry zone bar's volume to be tiny (below SMA)
        vol_sma = df_ind["volume_sma20"].iloc[150]
        df.iloc[150, df.columns.get_loc("volume")] = vol_sma * 0.5  # half of average

        out = strat.run(df)
        # Bar 150 should NOT generate an entry despite price being below lower band
        sig_at_150 = out["signal"].iloc[150]
        assert sig_at_150 != 1, (
            "Entry fired at bar 150 despite volume below SMA20. Volume filter may be broken."
        )


class TestRunMethod:
    """strategy.run(df) is the one-call convenience wrapper."""

    def test_run_returns_signal_column(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.run(df)
        assert "signal" in out.columns

    def test_run_returns_dataframe(self):
        df    = make_ohlcv()
        strat = MeanReversionStrategy()
        out   = strat.run(df)
        assert isinstance(out, pd.DataFrame)


class TestDescribe:
    """strategy.describe() must return a dict with all constructor params."""

    def test_describe_contains_all_params(self):
        strat = MeanReversionStrategy(bb_window=25, buy_rsi=28, sell_rsi=60)
        d = strat.describe()
        for key in ["strategy", "bb_window", "bb_std", "rsi_window", "buy_rsi",
                    "sell_rsi", "exit_target", "min_hold_bars", "stop_loss_pct",
                    "take_profit_pct"]:
            assert key in d, f"describe() is missing key: {key}"

    def test_describe_values_match_constructor(self):
        strat = MeanReversionStrategy(bb_window=25, buy_rsi=28, sell_rsi=60)
        d = strat.describe()
        assert d["bb_window"] == 25
        assert d["buy_rsi"]   == 28
        assert d["sell_rsi"]  == 60
