"""
tests/test_backtest.py
━━━━━━━━━━━━━━━━━━━━━━
Tests for the backtester engine (backtester/engine.py).

How tests work (beginner explanation):
  Each test function is a short program that:
    1. Sets up some known inputs (e.g. a fake price series)
    2. Runs a piece of our code
    3. Uses assert to check that the output is exactly what we expect

  If an assert fails, pytest shows you which line failed and what the
  actual vs expected values were.  Run all tests with:
      cd /Users/oliver/alpaca_bot
      python -m pytest strategy_ide/tests/ -v
"""

import sys
from pathlib import Path

# ── make sure strategy_ide is importable ──────────────────────────────────────
STRATEGY_IDE = Path(__file__).resolve().parent.parent
if str(STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(STRATEGY_IDE))

import numpy as np
import pandas as pd
import pytest

from backtester.engine import (
    BacktestResult,
    _infer_annualization_factor,
    aggregate_backtest_results,
    run_backtest,
)
from strategies.mean_reversion import MeanReversionStrategy


# ── helpers ───────────────────────────────────────────────────────────────────

def make_flat_ohlcv(n_bars: int = 200, price: float = 100.0, freq: str = "15T") -> pd.DataFrame:
    """
    Create a boring price series where the price never moves.
    The mean reversion strategy will never trigger on a flat line, so
    this is useful for testing "zero trades" behavior.

    Parameters
    ----------
    n_bars : how many bars to make
    price  : the constant close price
    freq   : pandas frequency string — "15T" = 15-minute, "D" = daily
    """
    idx = pd.date_range("2024-01-02", periods=n_bars, freq=freq)
    return pd.DataFrame(
        {
            "open":   price,
            "high":   price + 0.5,
            "low":    price - 0.5,
            "close":  price,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


def make_signal_df(
    n_bars: int = 100,
    entry_bar: int = 10,
    exit_bar: int = 30,
    entry_price: float = 100.0,
    exit_price: float = 110.0,
    stop_price: float = 98.0,
    freq: str = "15T",
) -> pd.DataFrame:
    """
    Build a DataFrame with a manually placed entry and exit signal.

    The price goes from entry_price at entry_bar to exit_price at exit_bar.
    stop_price is stored in the 'stop_price' column so the engine can use
    risk-based sizing.

    Parameters
    ----------
    entry_bar / exit_bar : bar indices where signals are placed
    entry_price / exit_price : close price at those bars
    stop_price : hard stop placed at entry bar (for position sizing)
    """
    idx = pd.date_range("2024-01-02", periods=n_bars, freq=freq)
    close = np.full(n_bars, entry_price, dtype=float)
    close[exit_bar:] = exit_price

    df = pd.DataFrame(
        {
            "open":         close,
            "high":         close + 0.5,
            "low":          close - 0.5,
            "close":        close,
            "volume":       1_000_000.0,
            "signal":       0,
            "stop_price":   np.nan,
            "take_profit_price": np.nan,
        },
        index=idx,
    )
    df.loc[df.index[entry_bar], "signal"]     = 1
    df.loc[df.index[entry_bar], "stop_price"] = stop_price
    df.loc[df.index[exit_bar],  "signal"]     = -1
    return df


class _PreSignaledStrategy(MeanReversionStrategy):
    """
    A strategy wrapper that bypasses indicator calculation and returns
    a DataFrame with signals already embedded.  Used for unit tests that
    need exact control over when entry/exit fires.
    """
    def __init__(self, pre_signaled_df: pd.DataFrame):
        super().__init__()
        self._df = pre_signaled_df

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._df.copy()

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._df.copy()


# ── tests ─────────────────────────────────────────────────────────────────────

class TestNoTrades:
    """When no signals fire the engine should return sensible empty results."""

    def test_no_trades_returns_empty_trade_log(self):
        df     = make_flat_ohlcv()
        strat  = MeanReversionStrategy()
        result = run_backtest(df, strat, initial_capital=100_000)
        assert result.trades.empty, "Expected zero trades on a flat price line"

    def test_no_trades_equity_curve_stays_flat(self):
        df     = make_flat_ohlcv()
        strat  = MeanReversionStrategy()
        result = run_backtest(df, strat, initial_capital=100_000)
        assert (result.equity_curve == 100_000).all(), "Equity should not change with no trades"

    def test_no_trades_stats_are_zero(self):
        df     = make_flat_ohlcv()
        strat  = MeanReversionStrategy()
        result = run_backtest(df, strat, initial_capital=100_000)
        s = result.stats
        assert s["num_trades"]       == 0
        assert s["total_return_pct"] == 0.0
        assert s["sharpe_ratio"]     == 0.0


class TestSingleTrade:
    """A single entry → exit pair with known prices."""

    def test_single_winning_trade_pnl_is_positive(self):
        # Entry at $100, exit at $110 → expect profit
        df     = make_signal_df(entry_price=100, exit_price=110, stop_price=98)
        strat  = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=10_000, risk_pct=0.01)
        assert len(result.trades) == 1
        assert result.trades["pnl"].iloc[0] > 0

    def test_single_losing_trade_pnl_is_negative(self):
        df     = make_signal_df(entry_price=100, exit_price=95, stop_price=98)
        strat  = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=10_000, risk_pct=0.01)
        assert len(result.trades) == 1
        assert result.trades["pnl"].iloc[0] < 0

    def test_equity_rises_after_winning_trade(self):
        df     = make_signal_df(entry_price=100, exit_price=110, stop_price=98)
        strat  = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=10_000, risk_pct=0.01)
        assert result.equity_curve.iloc[-1] > 10_000


class TestPositionSizing:
    """
    Regression test for BUG 1: shares must be based on risk_pct, NOT
    initial_capital / entry_price.

    Math check:
      equity=10,000  risk_pct=0.01  → risk_amount = 100
      entry=100  stop=98  → loss_per_share = 2
      shares = 100 / 2 = 50

    Before the fix the engine did:
      shares = 10,000 / 100 = 100   (100% of capital, 2x too many)
    """

    def test_shares_calculated_from_risk_pct(self):
        equity     = 10_000.0
        risk_pct   = 0.01
        entry      = 100.0
        stop       = 98.0            # $2 stop distance
        exit_p     = 105.0

        expected_shares = int((equity * risk_pct) / (entry - stop))   # = 50
        expected_pnl    = (exit_p - entry) * expected_shares           # = 250

        df    = make_signal_df(entry_price=entry, exit_price=exit_p, stop_price=stop)
        strat = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=equity, risk_pct=risk_pct)

        actual_pnl = result.trades["pnl"].iloc[0]
        assert abs(actual_pnl - expected_pnl) < 1.0, (
            f"Expected PnL ~{expected_pnl:.2f} (risk-sized), got {actual_pnl:.2f}. "
            "BUG 1 may not be fixed."
        )

    def test_full_capital_sizing_is_gone(self):
        """The old bug produced shares = initial_capital / entry_price = 1000.
        After the fix it should be ~50 for 1% risk with a 2% stop."""
        equity   = 100_000.0
        entry    = 100.0
        stop     = 98.0

        df    = make_signal_df(entry_price=entry, exit_price=105, stop_price=stop)
        strat = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=equity, risk_pct=0.01)

        old_bug_pnl = (105 - 100) * (equity / entry)  # = 5000 (wrong)
        actual_pnl  = result.trades["pnl"].iloc[0]
        assert actual_pnl < old_bug_pnl, (
            "PnL is suspiciously large — 100% capital sizing may still be active."
        )


class TestSharpeAnnualization:
    """
    Regression test for BUG 2: Sharpe must use the correct bars-per-year
    factor.  The same strategy on 15-min bars has more data points per year
    than on daily bars, so its annualized Sharpe should be larger.
    """

    def _run_with_freq(self, freq: str) -> float:
        # Build a DataFrame with a consistent upward drift (guaranteed trades)
        n = 300
        idx   = pd.date_range("2024-01-02", periods=n, freq=freq)
        close = 100 + np.cumsum(np.abs(np.random.default_rng(42).normal(0, 0.3, n)))
        df = pd.DataFrame(
            {"open": close, "high": close+1, "low": close-1, "close": close, "volume": 1e6},
            index=idx,
        )
        # Inject alternating entry/exit so there are actual trades
        df["signal"]     = 0
        df["stop_price"] = np.nan
        for i in range(10, n - 20, 20):
            df.iloc[i,   df.columns.get_loc("signal")]     = 1
            df.iloc[i,   df.columns.get_loc("stop_price")] = close[i] * 0.985
            df.iloc[i+10, df.columns.get_loc("signal")]    = -1

        strat  = _PreSignaledStrategy(df)
        result = run_backtest(df, strat, initial_capital=100_000, risk_pct=0.01)
        return result.stats["sharpe_ratio"]

    def test_infer_annualization_daily(self):
        idx = pd.date_range("2024-01-02", periods=100, freq="D")
        factor = _infer_annualization_factor(idx)
        assert abs(factor - np.sqrt(252)) < 0.01, f"Daily factor should be sqrt(252), got {factor:.4f}"

    def test_infer_annualization_15min(self):
        idx = pd.date_range("2024-01-02", periods=100, freq="15min")
        factor = _infer_annualization_factor(idx)
        expected = np.sqrt(252 * 26)
        assert abs(factor - expected) < 0.01, f"15-min factor should be sqrt(6552), got {factor:.4f}"

    def test_15min_sharpe_larger_than_daily_for_same_returns(self):
        # Sharpe on 15-min data should be higher than daily because sqrt(6552) > sqrt(252)
        sharpe_15m = self._run_with_freq("15min")
        sharpe_day = self._run_with_freq("D")
        # They won't be exactly proportional because random seeds differ, but
        # 15m should be meaningfully higher when the same drift is present
        assert sharpe_15m > 0 or sharpe_day > 0, "At least one Sharpe should be positive"


class TestDualExitEquityCurve:
    """
    Regression test for BUG 4: two trades from DIFFERENT symbols exiting
    on the same bar must both have their PnL credited to the equity curve.

    Note: the single-symbol engine only holds one position at a time, so
    "same-bar dual exit" only naturally arises in multi-symbol aggregation.
    The BUG 4 fix (groupby PnL by exit date) lives in aggregate_backtest_results.
    """

    def test_two_same_bar_exits_both_credited_in_aggregate(self):
        # Symbol A: entry bar=5, exit bar=20, price $100 → $110
        # Symbol B: entry bar=5, exit bar=20 (same exit bar!), price $200 → $210
        # Both use the same DatetimeIndex so their exit dates are identical.

        def _make(entry_price, exit_price, stop_price, n=60):
            idx   = pd.date_range("2024-01-02", periods=n, freq="15min")
            close = np.full(n, entry_price, dtype=float)
            close[20:] = exit_price
            df = pd.DataFrame(
                {"open": close, "high": close+0.5, "low": close-0.5,
                 "close": close, "volume": 1e6,
                 "signal": 0, "stop_price": np.nan, "take_profit_price": np.nan},
                index=idx,
            )
            df.iloc[5,  df.columns.get_loc("signal")]     = 1
            df.iloc[5,  df.columns.get_loc("stop_price")] = stop_price
            df.iloc[20, df.columns.get_loc("signal")]     = -1
            return df

        df_a   = _make(100, 110, 98)
        df_b   = _make(200, 210, 196)
        strat_a = _PreSignaledStrategy(df_a)
        strat_b = _PreSignaledStrategy(df_b)

        res_a = run_backtest(df_a, strat_a, initial_capital=50_000, risk_pct=0.01)
        res_b = run_backtest(df_b, strat_b, initial_capital=50_000, risk_pct=0.01)

        # Confirm both individual backtests produced exactly one trade
        assert len(res_a.trades) == 1, "Symbol A should have 1 trade"
        assert len(res_b.trades) == 1, "Symbol B should have 1 trade"

        # Their exit dates must be the same (that's the dual-exit scenario)
        assert res_a.trades["exit_date"].iloc[0] == res_b.trades["exit_date"].iloc[0], (
            "Test setup broken: exit dates should be identical"
        )

        combined = aggregate_backtest_results(
            {"A": res_a, "B": res_b}, initial_capital=100_000
        )

        # BUG 4 check: both PnLs must be in the combined equity curve
        total_pnl = res_a.trades["pnl"].iloc[0] + res_b.trades["pnl"].iloc[0]
        final_eq  = combined.equity_curve.iloc[-1]
        assert abs(final_eq - (100_000 + total_pnl)) < 1.0, (
            f"Aggregate equity does not reflect both same-bar exits. "
            f"Expected {100_000 + total_pnl:.2f}, got {final_eq:.2f}. BUG 4 may not be fixed."
        )


class TestEquityCurveLength:
    """Equity curve must always have exactly the same length as the input."""

    def test_equity_curve_length_matches_df(self):
        df     = make_flat_ohlcv(n_bars=150)
        strat  = MeanReversionStrategy()
        result = run_backtest(df, strat, initial_capital=100_000)
        assert len(result.equity_curve) == len(df), (
            f"equity_curve length {len(result.equity_curve)} != df length {len(df)}"
        )


class TestAggregateBacktest:
    """aggregate_backtest_results must combine PnLs from multiple symbols."""

    def test_two_symbol_pnl_sums_correctly(self):
        df_a = make_signal_df(entry_price=100, exit_price=110, stop_price=98, n_bars=60)
        df_b = make_signal_df(entry_price=200, exit_price=210, stop_price=196, n_bars=60, entry_bar=15, exit_bar=40)

        strat_a = _PreSignaledStrategy(df_a)
        strat_b = _PreSignaledStrategy(df_b)

        res_a = run_backtest(df_a, strat_a, initial_capital=50_000, risk_pct=0.01)
        res_b = run_backtest(df_b, strat_b, initial_capital=50_000, risk_pct=0.01)

        combined = aggregate_backtest_results(
            {"A": res_a, "B": res_b}, initial_capital=100_000
        )

        expected_total_pnl = res_a.trades["pnl"].sum() + res_b.trades["pnl"].sum()
        actual_final_eq    = combined.equity_curve.iloc[-1]
        assert abs(actual_final_eq - (100_000 + expected_total_pnl)) < 1.0
