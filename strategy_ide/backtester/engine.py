"""
Vectorized backtester: takes a strategy + OHLCV DataFrame, returns trades and stats.
No per-bar loop; uses pandas for performance.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
import sys
from pathlib import Path

import pandas as pd
import numpy as np

# Make sure risk/sizing.py is importable regardless of working directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from risk.sizing import fixed_risk_shares

if TYPE_CHECKING:
    from strategies.base_strategy import BaseStrategy


@dataclass
class BacktestResult:
    """Container for backtest outputs."""
    trades: pd.DataFrame       # trade log
    equity_curve: pd.Series    # equity over time
    stats: dict                # summary statistics


def run_backtest(
    df: pd.DataFrame,
    strategy: "BaseStrategy",
    initial_capital: float = 100_000.0,
    risk_pct: float = 0.01,
    stop_loss_pct: float = 0.015,
) -> BacktestResult:
    """
    Run a vectorized backtest on OHLCV data with the given strategy.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: open, high, low, close, volume.
        Index: DatetimeIndex (sorted).
    strategy : BaseStrategy
        Strategy instance with run(df) -> df with 'signal' column.
    initial_capital : float
        Starting equity.
    risk_pct : float
        Fraction of current equity risked per trade (e.g. 0.01 = 1%).
        If the trade hits its stop, you lose at most risk_pct * equity.
    stop_loss_pct : float
        Fallback stop distance as a fraction of entry price, used only when
        the strategy did not set a 'stop_price' column on the entry bar.

    Returns
    -------
    BacktestResult
        trades: columns [entry_date, exit_date, entry_price, exit_price, pnl, return_pct]
        equity_curve: Series index=date, value=equity
        stats: total_return, sharpe_ratio, max_drawdown, win_rate, avg_win, avg_loss
    """
    if "close" not in df.columns:
        raise ValueError("DataFrame must have a 'close' column.")
    df = df.sort_index()
    df = strategy.run(df)
    if "signal" not in df.columns:
        raise ValueError("Strategy must add a 'signal' column (1=enter, -1=exit, 0=hold).")

    # Vectorized trade detection: entry on 1, exit on -1
    signal = df["signal"].astype(int)
    close = df["close"].reindex(signal.index).ffill().bfill()

    # Entry/exit points (boolean Series)
    entries = (signal == 1).reindex(df.index).fillna(False)
    exits = (signal == -1).reindex(df.index).fillna(False)

    # Build trade log: pair each entry with the next exit (no re-entry until exit)
    in_trade = False
    trade_start_idx: int | None = None
    trade_rows: list[dict] = []
    n = len(df)
    for i in range(n):
        if entries.iloc[i]:
            if not in_trade:
                in_trade = True
                trade_start_idx = i
        if exits.iloc[i]:
            if in_trade and trade_start_idx is not None:
                entry_date = df.index[trade_start_idx]
                exit_date = df.index[i]
                entry_price = float(close.iloc[trade_start_idx])
                exit_price = float(close.iloc[i])
                ret_pct = (exit_price - entry_price) / entry_price

                # BUG 1 FIX: use risk-based position sizing instead of all-in.
                # current_equity grows (or shrinks) as previous trades settle.
                current_equity = initial_capital + sum(r["pnl"] for r in trade_rows)
                if (
                    "stop_price" in df.columns
                    and not pd.isna(df["stop_price"].iloc[trade_start_idx])
                ):
                    stop_price_val = float(df["stop_price"].iloc[trade_start_idx])
                    shares = fixed_risk_shares(
                        equity=current_equity,
                        price=entry_price,
                        risk_pct=risk_pct,
                        stop_loss_price=stop_price_val,
                    )
                else:
                    shares = fixed_risk_shares(
                        equity=current_equity,
                        price=entry_price,
                        risk_pct=risk_pct,
                        stop_loss_pct=stop_loss_pct,
                    )
                shares = max(shares, 1)

                trade_rows.append({
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl": (exit_price - entry_price) * shares,
                    "return_pct": ret_pct,
                })
                in_trade = False
                trade_start_idx = None

    # Force-close any position still open at the final bar.
    # Without this, trend strategies that ride a multi-year bull run
    # never record their largest winning trade.
    if in_trade and trade_start_idx is not None:
        entry_date  = df.index[trade_start_idx]
        exit_date   = df.index[-1]
        entry_price = float(close.iloc[trade_start_idx])
        exit_price  = float(close.iloc[-1])
        ret_pct     = (exit_price - entry_price) / entry_price
        current_equity = initial_capital + sum(r["pnl"] for r in trade_rows)
        if (
            "stop_price" in df.columns
            and not pd.isna(df["stop_price"].iloc[trade_start_idx])
        ):
            stop_price_val = float(df["stop_price"].iloc[trade_start_idx])
            shares = fixed_risk_shares(
                equity=current_equity, price=entry_price,
                risk_pct=risk_pct, stop_loss_price=stop_price_val,
            )
        else:
            shares = fixed_risk_shares(
                equity=current_equity, price=entry_price,
                risk_pct=risk_pct, stop_loss_pct=stop_loss_pct,
            )
        shares = max(shares, 1)
        trade_rows.append({
            "entry_date":  entry_date,
            "exit_date":   exit_date,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "pnl":         (exit_price - entry_price) * shares,
            "return_pct":  ret_pct,
        })

    if not trade_rows:
        trades = pd.DataFrame(
            columns=["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "return_pct"]
        )
        equity_curve = pd.Series(initial_capital, index=df.index)
        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            stats=_compute_stats(trades, initial_capital, equity_curve),
        )

    trades = pd.DataFrame(
        trade_rows,
        columns=["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "return_pct"],
    )

    # BUG 4 FIX: group PnL by exit date so multiple trades exiting the same
    # bar all get credited — the old single-counter approach dropped the second.
    pnl_by_date = trades.groupby("exit_date")["pnl"].sum()

    equity = initial_capital
    equity_curve = pd.Series(index=df.index, dtype=float)
    equity_curve.iloc[0] = initial_capital
    for i in range(1, len(df)):
        bar_date = df.index[i]
        if bar_date in pnl_by_date.index:
            equity += float(pnl_by_date[bar_date])
        equity_curve.iloc[i] = equity
    equity_curve = equity_curve.ffill().bfill()

    stats = _compute_stats(trades, initial_capital, equity_curve)
    return BacktestResult(trades=trades, equity_curve=equity_curve, stats=stats)


def _infer_annualization_factor(index: pd.DatetimeIndex) -> float:
    """
    Auto-detect bar frequency and return the correct sqrt(bars_per_year)
    annualization factor for the Sharpe ratio.

    Why this matters: Sharpe = mean_return / std_return * sqrt(periods_per_year).
    If your bars are 15 minutes apart there are 252*26 = 6,552 of them per year,
    NOT 252.  Using the wrong number makes intraday Sharpe look ~5x too high.
    """
    if len(index) < 3:
        return np.sqrt(252)
    diffs = pd.Series(index).diff().dropna()
    median_min = diffs.median().total_seconds() / 60
    if   median_min < 2:   bars_per_year = 252 * 390        # 1-min bars
    elif median_min < 8:   bars_per_year = 252 * 78         # 5-min bars
    elif median_min < 20:  bars_per_year = 252 * 26         # 15-min bars
    elif median_min < 40:  bars_per_year = 252 * 13         # 30-min bars
    elif median_min < 90:  bars_per_year = int(252 * 6.5)   # 60-min bars
    else:                  bars_per_year = 252              # daily bars
    return np.sqrt(bars_per_year)


def _compute_stats(
    trades: pd.DataFrame,
    initial_capital: float,
    equity_curve: pd.Series,
) -> dict:
    """Compute summary statistics."""
    if trades.empty:
        return {
            "total_return": 0.0,
            "total_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "num_trades": 0,
        }
    total_return = equity_curve.iloc[-1] - initial_capital
    total_return_pct = total_return / initial_capital

    # BUG 2 FIX: annualize Sharpe using the correct bars-per-year factor.
    # np.sqrt(252) is only right for daily bars; 15-min bars need sqrt(252*26).
    daily_rets = equity_curve.pct_change().dropna()
    annualization = _infer_annualization_factor(equity_curve.index)
    if daily_rets.std() == 0:
        sharpe = 0.0
    else:
        sharpe = annualization * daily_rets.mean() / daily_rets.std()

    # Max drawdown
    cummax = equity_curve.cummax()
    drawdown = equity_curve - cummax
    max_dd = drawdown.min()
    max_dd_pct = max_dd / cummax.max() if cummax.max() > 0 else 0.0

    # Win rate, avg win/loss from trades (return_pct is decimal)
    wins = trades[trades["return_pct"] > 0]
    losses = trades[trades["return_pct"] <= 0]
    win_rate = len(wins) / len(trades) if len(trades) > 0 else 0.0
    avg_win_pct = float(wins["return_pct"].mean()) if len(wins) > 0 else 0.0
    avg_loss_pct = float(losses["return_pct"].mean()) if len(losses) > 0 else 0.0

    return {
        "total_return": float(total_return),
        "total_return_pct": float(total_return_pct),
        "sharpe_ratio": float(sharpe),
        "max_drawdown": float(max_dd),
        "max_drawdown_pct": float(max_dd_pct),
        "win_rate": float(win_rate),
        "avg_win_pct": float(avg_win_pct),
        "avg_loss_pct": float(avg_loss_pct),
        "num_trades": len(trades),
    }


def aggregate_backtest_results(
    results_by_symbol: dict,
    initial_capital: float,
) -> BacktestResult:
    """
    Combine per-symbol backtest results into one portfolio result.
    Each result's trades must have a 'symbol' column (added by caller).
    Trades are merged and sorted by exit_date; equity = initial_capital + cumsum(pnl).
    """
    if not results_by_symbol:
        return BacktestResult(
            trades=pd.DataFrame(columns=["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "return_pct", "symbol"]),
            equity_curve=pd.Series([initial_capital]),
            stats=_compute_stats(pd.DataFrame(), initial_capital, pd.Series([initial_capital])),
        )
    all_trades = []
    for sym, res in results_by_symbol.items():
        if res.trades.empty:
            continue
        t = res.trades.copy()
        t["symbol"] = sym
        all_trades.append(t)
    if not all_trades:
        first = next(iter(results_by_symbol.values()))
        return BacktestResult(
            trades=pd.DataFrame(columns=["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "return_pct", "symbol"]),
            equity_curve=first.equity_curve,
            stats=_compute_stats(pd.DataFrame(), initial_capital, first.equity_curve),
        )
    trades = pd.concat(all_trades, ignore_index=True)
    trades = trades.sort_values("exit_date").reset_index(drop=True)
    # Build equity curve: one point before first exit (initial_capital), then after each exit
    equity = initial_capital
    first_ts = pd.Timestamp(trades["entry_date"].min()) - pd.Timedelta(days=1)
    eq_dates = [first_ts]
    eq_values = [initial_capital]
    for _, row in trades.iterrows():
        equity += float(row["pnl"])
        eq_dates.append(pd.Timestamp(row["exit_date"]))
        eq_values.append(equity)
    equity_curve = pd.Series(eq_values, index=pd.DatetimeIndex(eq_dates))
    stats = _compute_stats(trades, initial_capital, equity_curve)
    return BacktestResult(trades=trades, equity_curve=equity_curve, stats=stats)


if __name__ == "__main__":
    # Quick test with mean reversion
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from data.fetcher import fetch_bars
    from strategies.mean_reversion import MeanReversionStrategy

    df = fetch_bars("SPY", "2023-01-01", "2023-06-30", use_cache=True)
    strat = MeanReversionStrategy()
    res = run_backtest(df, strat, initial_capital=100_000.0)
    print("Trades:", len(res.trades))
    print("Stats:", res.stats)
    print(res.trades.head())
