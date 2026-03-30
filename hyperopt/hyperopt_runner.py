"""
optimization/hyperopt_runner.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hyperparameter optimization for any BaseStrategy subclass.
Supports single in/out-of-sample splits and walk-forward optimization.

Dependencies:
    pip install hyperopt numpy pandas

Usage (single optimization):
    from optimization.hyperopt_runner import HyperoptRunner
    from strategies.mean_reversion import MeanReversionStrategy

    runner = HyperoptRunner(
        strategy_class=MeanReversionStrategy,
        df=df,
        max_evals=200,
    )
    best_params, report = runner.optimize()
    print(report)

Usage (walk-forward):
    results = runner.walk_forward(n_splits=5)
    runner.print_walk_forward_report(results)
"""

import warnings
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

import numpy as np
import pandas as pd
from hyperopt import STATUS_FAIL, STATUS_OK, Trials, fmin, hp, tpe
from hyperopt.pyll import scope

from strategies.base_strategy import BaseStrategy

warnings.filterwarnings("ignore")


# ─── Search space definition ──────────────────────────────────────────────────
# Default search space for MeanReversionStrategy.
# Override by passing `search_space=` to HyperoptRunner.

MEAN_REVERSION_SPACE: Dict[str, Any] = {
    "bb_window":       scope.int(hp.quniform("bb_window",  10,  50, 1)),
    "bb_std":          hp.uniform("bb_std",                1.5, 3.0),
    "rsi_window":      scope.int(hp.quniform("rsi_window", 7,   21, 1)),
    "buy_rsi":         scope.int(hp.quniform("buy_rsi",    20,  40, 1)),
    "sell_rsi":        scope.int(hp.quniform("sell_rsi",   55,  75, 1)),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",         0.02, 0.06),
    "take_profit_pct": hp.uniform("take_profit_pct",       0.03, 0.10),
}


# ─── Result containers ────────────────────────────────────────────────────────

@dataclass
class BacktestStats:
    total_return:   float = 0.0
    sharpe_ratio:   float = 0.0
    max_drawdown:   float = 0.0
    win_rate:       float = 0.0
    avg_win:        float = 0.0
    avg_loss:       float = 0.0
    profit_factor:  float = 0.0
    n_trades:       int   = 0
    equity_curve:   pd.Series = field(default_factory=pd.Series)

    def is_valid(self) -> bool:
        """Reject degenerate results."""
        return self.n_trades >= 5 and not np.isnan(self.sharpe_ratio)


@dataclass
class OptimizationResult:
    best_params:     Dict[str, Any]
    in_sample:       BacktestStats
    out_of_sample:   BacktestStats
    all_trials:      Trials
    timestamp:       str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class WalkForwardWindow:
    window_index:  int
    train_start:   pd.Timestamp
    train_end:     pd.Timestamp
    test_start:    pd.Timestamp
    test_end:      pd.Timestamp
    best_params:   Dict[str, Any]
    train_stats:   BacktestStats
    test_stats:    BacktestStats


# ─── Vectorized backtester ────────────────────────────────────────────────────

class VectorizedBacktester:
    """
    Lightweight vectorized backtester used internally by HyperoptRunner.
    For full-featured backtesting use backtester/engine.py.

    Assumptions:
    - Long only
    - One position at a time
    - Fills at next bar's open (avoids lookahead bias)
    - Commission applied per trade (round-trip)
    """

    def __init__(self, commission: float = 0.001):
        self.commission = commission   # 0.1% per side default

    def run(self, df: pd.DataFrame) -> BacktestStats:
        """
        Run a backtest on a signal-annotated DataFrame.
        df must have columns: open, close, signal
        Returns BacktestStats.
        """
        df = df.copy().reset_index(drop=True)

        if "signal" not in df.columns:
            raise ValueError("DataFrame must have a 'signal' column. Run strategy.run(df) first.")

        equity      = 1.0
        in_position = False
        entry_price = 0.0
        trades      = []
        equity_curve = [1.0]

        for i in range(1, len(df)):
            prev_signal = df.at[i - 1, "signal"]
            bar         = df.iloc[i]
            fill_price  = bar["open"]   # fill at next open

            # ── Entry ──────────────────────────────────────────────────────
            if prev_signal == 1 and not in_position:
                entry_price = fill_price * (1 + self.commission)
                in_position = True

            # ── Exit ───────────────────────────────────────────────────────
            elif prev_signal == -1 and in_position:
                exit_price = fill_price * (1 - self.commission)
                pnl        = (exit_price - entry_price) / entry_price
                equity    *= (1 + pnl)
                trades.append(pnl)
                in_position = False

            equity_curve.append(equity)

        # Close any open position at the last bar
        if in_position:
            exit_price = df.iloc[-1]["close"] * (1 - self.commission)
            pnl        = (exit_price - entry_price) / entry_price
            equity    *= (1 + pnl)
            trades.append(pnl)

        return self._compute_stats(trades, pd.Series(equity_curve))

    # ── Stat computation ──────────────────────────────────────────────────────

    def _compute_stats(
        self, trades: List[float], equity_curve: pd.Series
    ) -> BacktestStats:
        n = len(trades)

        if n == 0:
            return BacktestStats(n_trades=0, sharpe_ratio=-999.0)

        arr        = np.array(trades)
        wins       = arr[arr > 0]
        losses     = arr[arr < 0]

        total_return  = float(equity_curve.iloc[-1] - 1)
        avg_win       = float(wins.mean())   if len(wins)   > 0 else 0.0
        avg_loss      = float(losses.mean()) if len(losses) > 0 else 0.0
        win_rate      = len(wins) / n if n > 0 else 0.0
        profit_factor = (
            (wins.sum() / abs(losses.sum()))
            if len(losses) > 0 and losses.sum() != 0
            else float("inf")
        )

        # Sharpe (annualized, assume daily equity changes from hourly data)
        eq_returns = equity_curve.pct_change().dropna()
        sharpe     = (
            float(eq_returns.mean() / eq_returns.std() * np.sqrt(252 * 6.5))
            if eq_returns.std() > 0 else 0.0
        )

        # Max drawdown
        roll_max     = equity_curve.cummax()
        drawdowns    = (equity_curve - roll_max) / roll_max
        max_drawdown = float(drawdowns.min())

        return BacktestStats(
            total_return  = total_return,
            sharpe_ratio  = sharpe,
            max_drawdown  = max_drawdown,
            win_rate      = win_rate,
            avg_win       = avg_win,
            avg_loss      = avg_loss,
            profit_factor = profit_factor,
            n_trades      = n,
            equity_curve  = equity_curve,
        )


# ─── Hyperopt Runner ──────────────────────────────────────────────────────────

class HyperoptRunner:
    """
    Optimize any BaseStrategy subclass over a price DataFrame.

    Parameters
    ----------
    strategy_class : Type[BaseStrategy]
        The strategy class to optimize (not an instance).
    df : pd.DataFrame
        Full OHLCV DataFrame for the asset/period to optimize over.
    search_space : dict, optional
        Hyperopt search space. Defaults to MEAN_REVERSION_SPACE.
    max_evals : int
        Number of hyperopt evaluations (default 200).
    objective : str
        Metric to maximize: 'sharpe_ratio', 'total_return', 'profit_factor'.
    train_pct : float
        Fraction of data used for in-sample optimization (default 0.7).
    commission : float
        Round-trip commission per trade (default 0.001 = 0.1%).
    min_trades : int
        Minimum trades required to consider a result valid (default 5).
    """

    def __init__(
        self,
        strategy_class: Type[BaseStrategy],
        df: pd.DataFrame,
        search_space: Optional[Dict[str, Any]] = None,
        max_evals: int      = 200,
        objective: str      = "sharpe_ratio",
        train_pct: float    = 0.70,
        commission: float   = 0.001,
        min_trades: int     = 5,
    ):
        self.strategy_class = strategy_class
        self.df             = df.copy().reset_index(drop=True)
        self.search_space   = search_space or MEAN_REVERSION_SPACE
        self.max_evals      = max_evals
        self.objective      = objective
        self.train_pct      = train_pct
        self.commission     = commission
        self.min_trades     = min_trades
        self.backtester     = VectorizedBacktester(commission=commission)

        self._validate()

    def _validate(self):
        required = {"open", "high", "low", "close", "volume"}
        missing  = required - set(self.df.columns)
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")
        if len(self.df) < 100:
            raise ValueError("DataFrame too short — need at least 100 bars.")

    # ── Core objective function ───────────────────────────────────────────────

    def _objective(self, params: Dict[str, Any], df: pd.DataFrame) -> Dict:
        try:
            strategy  = self.strategy_class(**params)
            annotated = strategy.run(df)
            stats     = self.backtester.run(annotated)

            if not stats.is_valid() or stats.n_trades < self.min_trades:
                return {"status": STATUS_FAIL, "loss": 999.0}

            loss = -getattr(stats, self.objective)

            return {
                "status": STATUS_OK,
                "loss":   loss,
                "stats":  stats,
                "params": params,
            }

        except Exception as e:
            return {"status": STATUS_FAIL, "loss": 999.0, "error": str(e)}

    # ── Single optimization ───────────────────────────────────────────────────

    def optimize(self, verbose: bool = True) -> tuple[Dict, OptimizationResult]:
        """
        Split df into train/test, optimize on train, evaluate on test.
        Returns (best_params, OptimizationResult).
        """
        split       = int(len(self.df) * self.train_pct)
        train_df    = self.df.iloc[:split].reset_index(drop=True)
        test_df     = self.df.iloc[split:].reset_index(drop=True)

        if verbose:
            print(f"\n{'━'*55}")
            print(f"  Hyperopt — {self.strategy_class.__name__}")
            print(f"  Train bars : {len(train_df)}  |  Test bars: {len(test_df)}")
            print(f"  Max evals  : {self.max_evals}")
            print(f"  Objective  : maximize {self.objective}")
            print(f"{'━'*55}\n")

        trials = Trials()
        fmin(
            fn        = lambda p: self._objective(p, train_df),
            space     = self.search_space,
            algo      = tpe.suggest,
            max_evals = self.max_evals,
            trials    = trials,
            verbose   = verbose,
        )

        best_params = self._extract_best_params(trials)

        # Evaluate best params on both splits
        in_sample_stats  = self._evaluate(best_params, train_df)
        out_sample_stats = self._evaluate(best_params, test_df)

        result = OptimizationResult(
            best_params   = best_params,
            in_sample     = in_sample_stats,
            out_of_sample = out_sample_stats,
            all_trials    = trials,
        )

        if verbose:
            self.print_report(result)

        return best_params, result

    # ── Walk-forward optimization ─────────────────────────────────────────────

    def walk_forward(
        self,
        n_splits: int   = 5,
        verbose: bool   = True,
    ) -> List[WalkForwardWindow]:
        """
        Walk-forward optimization:
            1. Divide df into (n_splits + 1) equal windows
            2. For each step i: optimize on windows [0..i], test on window [i+1]
            3. Aggregate OOS performance across all windows

        Returns a list of WalkForwardWindow results.
        """
        window_size = len(self.df) // (n_splits + 1)
        windows     = []

        if verbose:
            print(f"\n{'━'*55}")
            print(f"  Walk-Forward — {self.strategy_class.__name__}")
            print(f"  Splits : {n_splits}  |  Window size: {window_size} bars")
            print(f"{'━'*55}")

        for i in range(n_splits):
            train_end_idx = (i + 1) * window_size
            test_end_idx  = train_end_idx + window_size

            train_df = self.df.iloc[:train_end_idx].reset_index(drop=True)
            test_df  = self.df.iloc[train_end_idx:test_end_idx].reset_index(drop=True)

            if len(test_df) < 20:
                break

            if verbose:
                print(f"\n  Window {i+1}/{n_splits} — "
                      f"Train: {len(train_df)} bars | Test: {len(test_df)} bars")

            # Optimize on training window
            trials = Trials()
            fmin(
                fn        = lambda p: self._objective(p, train_df),
                space     = self.search_space,
                algo      = tpe.suggest,
                max_evals = self.max_evals,
                trials    = trials,
                verbose   = False,
            )

            best_params  = self._extract_best_params(trials)
            train_stats  = self._evaluate(best_params, train_df)
            test_stats   = self._evaluate(best_params, test_df)

            # Resolve timestamps if index is datetime
            def _ts(df, idx):
                try:
                    return pd.Timestamp(df.index[idx])
                except Exception:
                    return pd.Timestamp(idx)

            window = WalkForwardWindow(
                window_index = i + 1,
                train_start  = _ts(train_df, 0),
                train_end    = _ts(train_df, -1),
                test_start   = _ts(test_df,  0),
                test_end     = _ts(test_df,  -1),
                best_params  = best_params,
                train_stats  = train_stats,
                test_stats   = test_stats,
            )
            windows.append(window)

            if verbose:
                print(f"    Best params : {best_params}")
                print(f"    Train Sharpe: {train_stats.sharpe_ratio:.2f}  "
                      f"OOS Sharpe: {test_stats.sharpe_ratio:.2f}  "
                      f"OOS Trades: {test_stats.n_trades}")

        if verbose:
            self.print_walk_forward_report(windows)

        return windows

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _evaluate(self, params: Dict[str, Any], df: pd.DataFrame) -> BacktestStats:
        """Run strategy with given params on df and return stats."""
        try:
            strategy  = self.strategy_class(**params)
            annotated = strategy.run(df)
            return self.backtester.run(annotated)
        except Exception:
            return BacktestStats()

    def _extract_best_params(self, trials: Trials) -> Dict[str, Any]:
        """Pull the best trial's params, casting types cleanly."""
        ok_trials = [t for t in trials.trials if t["result"]["status"] == STATUS_OK]
        if not ok_trials:
            raise RuntimeError("No successful trials — check your data or search space.")

        best_trial  = min(ok_trials, key=lambda t: t["result"]["loss"])
        raw_params  = best_trial["result"]["params"]

        # Cast numpy ints/floats to native Python types
        clean = {}
        for k, v in raw_params.items():
            if isinstance(v, (np.integer,)):
                clean[k] = int(v)
            elif isinstance(v, (np.floating,)):
                clean[k] = float(round(v, 4))
            else:
                clean[k] = v

        return clean

    # ── Reporting ─────────────────────────────────────────────────────────────

    def print_report(self, result: OptimizationResult):
        ins = result.in_sample
        oos = result.out_of_sample

        print(f"\n{'━'*55}")
        print(f"  OPTIMIZATION RESULTS — {self.strategy_class.__name__}")
        print(f"{'━'*55}")
        print(f"  Best params:     {result.best_params}")
        print()
        print(f"  {'Metric':<20} {'In-Sample':>12} {'Out-of-Sample':>14}")
        print(f"  {'─'*46}")
        rows = [
            ("Total Return",   f"{ins.total_return*100:.2f}%",   f"{oos.total_return*100:.2f}%"),
            ("Sharpe Ratio",   f"{ins.sharpe_ratio:.2f}",        f"{oos.sharpe_ratio:.2f}"),
            ("Max Drawdown",   f"{ins.max_drawdown*100:.2f}%",   f"{oos.max_drawdown*100:.2f}%"),
            ("Win Rate",       f"{ins.win_rate*100:.1f}%",       f"{oos.win_rate*100:.1f}%"),
            ("Profit Factor",  f"{ins.profit_factor:.2f}",       f"{oos.profit_factor:.2f}"),
            ("Avg Win",        f"{ins.avg_win*100:.2f}%",        f"{oos.avg_win*100:.2f}%"),
            ("Avg Loss",       f"{ins.avg_loss*100:.2f}%",       f"{oos.avg_loss*100:.2f}%"),
            ("# Trades",       str(ins.n_trades),                str(oos.n_trades)),
        ]
        for label, ins_val, oos_val in rows:
            print(f"  {label:<20} {ins_val:>12} {oos_val:>14}")
        print(f"{'━'*55}\n")

    def print_walk_forward_report(self, windows: List[WalkForwardWindow]):
        print(f"\n{'━'*65}")
        print(f"  WALK-FORWARD SUMMARY")
        print(f"{'━'*65}")
        print(f"  {'Win':>4}  {'Train Sharpe':>13}  {'OOS Sharpe':>11}  "
              f"{'OOS Return':>11}  {'OOS DD':>8}  {'OOS Trades':>10}")
        print(f"  {'─'*60}")

        oos_sharpes  = []
        oos_returns  = []

        for w in windows:
            oos_sharpes.append(w.test_stats.sharpe_ratio)
            oos_returns.append(w.test_stats.total_return)
            print(
                f"  {w.window_index:>4}  "
                f"{w.train_stats.sharpe_ratio:>13.2f}  "
                f"{w.test_stats.sharpe_ratio:>11.2f}  "
                f"{w.test_stats.total_return*100:>10.2f}%  "
                f"{w.test_stats.max_drawdown*100:>7.2f}%  "
                f"{w.test_stats.n_trades:>10}"
            )

        print(f"  {'─'*60}")
        print(f"  {'Avg':>4}  {'':>13}  {np.mean(oos_sharpes):>11.2f}  "
              f"{np.mean(oos_returns)*100:>10.2f}%")
        print(f"{'━'*65}\n")
