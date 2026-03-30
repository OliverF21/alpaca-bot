"""
optimization/hyperopt_runner.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hyperparameter optimization for any BaseStrategy subclass.
Supports single in/out-of-sample splits and walk-forward optimization.

Usage:
    from optimization.hyperopt_runner import HyperoptRunner
    from strategies.mean_reversion import MeanReversionStrategy

    runner = HyperoptRunner(
        strategy_class=MeanReversionStrategy,
        df=df,
        max_evals=200,
    )
    best_params, report = runner.optimize()
"""

import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

import numpy as np
import pandas as pd
from hyperopt import STATUS_FAIL, STATUS_OK, Trials, fmin, hp, tpe
from hyperopt.pyll import scope

from strategies.base_strategy import BaseStrategy

warnings.filterwarnings("ignore")


# ─── Search space (MeanReversionStrategy) ─────────────────────────────────────
# Keys must exactly match MeanReversionStrategy.__init__ parameter names.
MEAN_REVERSION_SPACE: Dict[str, Any] = {
    "bb_window":       scope.int(hp.quniform("bb_window",  10,  50, 1)),
    "bb_std":          hp.uniform("bb_std",                1.5, 3.0),
    "rsi_window":      scope.int(hp.quniform("rsi_window", 7,   21, 1)),
    "buy_rsi":         scope.int(hp.quniform("buy_rsi",    25,  45, 1)),
    "sell_rsi":        scope.int(hp.quniform("sell_rsi",   50,  70, 1)),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",         0.01, 0.05),
    "take_profit_pct": hp.uniform("take_profit_pct",       0.02, 0.10),
}

# ─── Search space (CryptoMeanReversionStrategy) ────────────────────────────────
# Wider ranges reflect crypto's higher volatility and 24/7 market structure.
CRYPTO_MR_SPACE: Dict[str, Any] = {
    "bb_window":       scope.int(hp.quniform("bb_window",   10, 40, 1)),
    "bb_std":          hp.uniform("bb_std",                 1.5, 3.5),
    "rsi_window":      scope.int(hp.quniform("rsi_window",  7,  21, 1)),
    "buy_rsi":         scope.int(hp.quniform("buy_rsi",     20, 38, 1)),
    "sell_rsi":        scope.int(hp.quniform("sell_rsi",    60, 78, 1)),
    "atr_stop_mult":   hp.uniform("atr_stop_mult",          1.5, 4.0),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",          0.03, 0.07),
    "take_profit_pct": hp.uniform("take_profit_pct",        0.05, 0.15),
    "min_hold_bars":   scope.int(hp.quniform("min_hold_bars", 1, 5, 1)),
}

# ─── Search space (CryptoTrendFollowingStrategy) ────────────────────────────────
# EMA crossover + ADX + ATR trailing stop for trending crypto markets.
CRYPTO_TREND_SPACE: Dict[str, Any] = {
    "fast_ema":        scope.int(hp.quniform("fast_ema",      5, 20, 1)),
    "slow_ema":        scope.int(hp.quniform("slow_ema",     20, 50, 1)),
    "adx_threshold":   hp.uniform("adx_threshold",           15, 35),
    "atr_stop_mult":   hp.uniform("atr_stop_mult",           2.0, 5.0),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",           0.03, 0.08),
    "take_profit_pct": hp.uniform("take_profit_pct",         0.08, 0.25),
}

# ─── Search space (CryptoBreakoutStrategy) ─────────────────────────────────────
# Donchian channel breakout + volume confirmation + ATR stop.
CRYPTO_BREAKOUT_SPACE: Dict[str, Any] = {
    "channel_window":  scope.int(hp.quniform("channel_window", 12, 48, 1)),
    "vol_mult":        hp.uniform("vol_mult",                  1.2, 2.5),
    "atr_stop_mult":   hp.uniform("atr_stop_mult",             1.5, 4.0),
    "min_hold_bars":   scope.int(hp.quniform("min_hold_bars",  2,  8, 1)),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",             0.03, 0.08),
    "take_profit_pct": hp.uniform("take_profit_pct",           0.06, 0.20),
}

# ─── Search space (CryptoSupertrendStrategy) ────────────────────────────────────
# ATR-based adaptive trend bands. Key insight: fewer params = less overfitting.
# multiplier is the dominant lever — smaller = more signals, larger = fewer but cleaner.
CRYPTO_SUPERTREND_SPACE: Dict[str, Any] = {
    "atr_period":      scope.int(hp.quniform("atr_period",    7,  21, 1)),
    "multiplier":      hp.uniform("multiplier",               1.5, 5.0),
    "rsi_min":         hp.uniform("rsi_min",                  0.0, 55.0),  # 0 = disabled
    "min_hold_bars":   scope.int(hp.quniform("min_hold_bars", 1,   5, 1)),
    "stop_loss_pct":   hp.uniform("stop_loss_pct",            0.03, 0.10),
    "take_profit_pct": hp.uniform("take_profit_pct",          0.10, 0.40),  # let trends run
}


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
    equity_curve:   pd.Series = None

    def __post_init__(self):
        if self.equity_curve is None:
            self.equity_curve = pd.Series(dtype=float)

    def is_valid(self) -> bool:
        return self.n_trades >= 5 and not np.isnan(self.sharpe_ratio)


@dataclass
class OptimizationResult:
    best_params:     Dict[str, Any]
    in_sample:       BacktestStats
    out_of_sample:   BacktestStats
    all_trials:      Trials
    timestamp:       str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


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


class VectorizedBacktester:
    """
    Lightweight vectorized backtester for HyperoptRunner.
    Long only, one position at a time, fill at next bar open.
    """

    def __init__(self, commission: float = 0.001):
        self.commission = commission

    def run(self, df: pd.DataFrame) -> BacktestStats:
        df = df.copy().reset_index(drop=True)
        if "signal" not in df.columns:
            raise ValueError("DataFrame must have 'signal' column.")
        equity = 1.0
        in_position = False
        entry_price = 0.0
        trades = []
        equity_curve = [1.0]
        for i in range(1, len(df)):
            prev_signal = df.at[i - 1, "signal"]
            bar = df.iloc[i]
            fill_price = bar["open"]
            if prev_signal == 1 and not in_position:
                entry_price = fill_price * (1 + self.commission)
                in_position = True
            elif prev_signal == -1 and in_position:
                exit_price = fill_price * (1 - self.commission)
                pnl = (exit_price - entry_price) / entry_price
                equity *= (1 + pnl)
                trades.append(pnl)
                in_position = False
            equity_curve.append(equity)
        if in_position:
            exit_price = df.iloc[-1]["close"] * (1 - self.commission)
            pnl = (exit_price - entry_price) / entry_price
            equity *= (1 + pnl)
            trades.append(pnl)
        return self._compute_stats(trades, pd.Series(equity_curve))

    def _compute_stats(self, trades: List[float], equity_curve: pd.Series) -> BacktestStats:
        n = len(trades)
        if n == 0:
            return BacktestStats(n_trades=0, sharpe_ratio=-999.0)
        arr = np.array(trades)
        wins = arr[arr > 0]
        losses = arr[arr < 0]
        total_return = float(equity_curve.iloc[-1] - 1)
        avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0
        win_rate = len(wins) / n if n > 0 else 0.0
        profit_factor = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and losses.sum() != 0 else float("inf")
        eq_returns = equity_curve.pct_change().dropna()
        sharpe = float(eq_returns.mean() / eq_returns.std() * np.sqrt(252 * 6.5)) if eq_returns.std() > 0 else 0.0
        roll_max = equity_curve.cummax()
        drawdowns = (equity_curve - roll_max) / roll_max
        max_drawdown = float(drawdowns.min())
        return BacktestStats(
            total_return=total_return, sharpe_ratio=sharpe, max_drawdown=max_drawdown,
            win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
            n_trades=n, equity_curve=equity_curve,
        )


class HyperoptRunner:
    """
    Optimize any BaseStrategy subclass over a price DataFrame.
    """

    def __init__(
        self,
        strategy_class: Type[BaseStrategy],
        df: pd.DataFrame,
        search_space: Optional[Dict[str, Any]] = None,
        max_evals: int = 200,
        objective: str = "sharpe_ratio",
        train_pct: float = 0.70,
        commission: float = 0.001,
        min_trades: int = 5,
    ):
        self.strategy_class = strategy_class
        self.df = df.copy().reset_index(drop=True)
        self.search_space = search_space or MEAN_REVERSION_SPACE
        self.max_evals = max_evals
        self.objective = objective
        self.train_pct = train_pct
        self.commission = commission
        self.min_trades = min_trades
        self.backtester = VectorizedBacktester(commission=commission)
        self._validate()

    def _validate(self):
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(self.df.columns)
        if missing:
            raise ValueError(f"DataFrame missing columns: {missing}")
        if len(self.df) < 100:
            raise ValueError("DataFrame too short — need at least 100 bars.")

    def _objective(self, params: Dict[str, Any], df: pd.DataFrame) -> Dict:
        try:
            strategy = self.strategy_class(**params)
            annotated = strategy.run(df)
            stats = self.backtester.run(annotated)
            if not stats.is_valid() or stats.n_trades < self.min_trades:
                return {"status": STATUS_FAIL, "loss": 999.0}
            loss = -getattr(stats, self.objective)
            return {"status": STATUS_OK, "loss": loss, "stats": stats, "params": params}
        except Exception:
            return {"status": STATUS_FAIL, "loss": 999.0}

    def optimize(self, verbose: bool = False) -> tuple:
        split = int(len(self.df) * self.train_pct)
        train_df = self.df.iloc[:split].reset_index(drop=True)
        test_df = self.df.iloc[split:].reset_index(drop=True)
        trials = Trials()
        fmin(
            fn=lambda p: self._objective(p, train_df),
            space=self.search_space,
            algo=tpe.suggest,
            max_evals=self.max_evals,
            trials=trials,
            verbose=verbose,
        )
        best_params = self._extract_best_params(trials)
        in_sample_stats = self._evaluate(best_params, train_df)
        out_sample_stats = self._evaluate(best_params, test_df)
        result = OptimizationResult(
            best_params=best_params,
            in_sample=in_sample_stats,
            out_of_sample=out_sample_stats,
            all_trials=trials,
        )
        return best_params, result

    def walk_forward(self, n_splits: int = 5, verbose: bool = False) -> List[WalkForwardWindow]:
        window_size = len(self.df) // (n_splits + 1)
        windows = []
        for i in range(n_splits):
            train_end_idx = (i + 1) * window_size
            test_end_idx = train_end_idx + window_size
            train_df = self.df.iloc[:train_end_idx].reset_index(drop=True)
            test_df = self.df.iloc[train_end_idx:test_end_idx].reset_index(drop=True)
            if len(test_df) < 20:
                break
            trials = Trials()
            fmin(
                fn=lambda p: self._objective(p, train_df),
                space=self.search_space,
                algo=tpe.suggest,
                max_evals=self.max_evals,
                trials=trials,
                verbose=False,
            )
            best_params = self._extract_best_params(trials)
            train_stats = self._evaluate(best_params, train_df)
            test_stats = self._evaluate(best_params, test_df)
            def _ts(d, idx):
                try:
                    return pd.Timestamp(d.index[idx])
                except Exception:
                    return pd.Timestamp(idx)
            windows.append(WalkForwardWindow(
                window_index=i + 1,
                train_start=_ts(train_df, 0), train_end=_ts(train_df, -1),
                test_start=_ts(test_df, 0), test_end=_ts(test_df, -1),
                best_params=best_params, train_stats=train_stats, test_stats=test_stats,
            ))
        return windows

    def _evaluate(self, params: Dict[str, Any], df: pd.DataFrame) -> BacktestStats:
        try:
            strategy = self.strategy_class(**params)
            annotated = strategy.run(df)
            return self.backtester.run(annotated)
        except Exception:
            return BacktestStats()

    def _extract_best_params(self, trials: Trials) -> Dict[str, Any]:
        ok_trials = [t for t in trials.trials if t["result"]["status"] == STATUS_OK]
        if not ok_trials:
            raise RuntimeError("No successful trials.")
        best_trial = min(ok_trials, key=lambda t: t["result"]["loss"])
        raw = best_trial["result"]["params"]
        clean = {}
        for k, v in raw.items():
            if isinstance(v, (np.integer,)):
                clean[k] = int(v)
            elif isinstance(v, (np.floating,)):
                clean[k] = float(round(v, 4))
            else:
                clean[k] = v
        return clean
