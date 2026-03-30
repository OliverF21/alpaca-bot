"""
Save and load backtest results for analysis and retesting.
Every backtest run is persisted under backtest_results/<run_id>/.
"""

from pathlib import Path
from datetime import datetime
from typing import Any

import pandas as pd
import json

# Project root on path when imported from CLI/dashboard
import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from config import BACKTEST_RESULTS_DIR

from backtester.engine import BacktestResult


def _run_id(strategy_name: str, symbol: str) -> str:
    """Unique run id: timestamp_strategy_symbol (filesystem-safe)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = f"{strategy_name}_{symbol}".replace(" ", "_")
    return f"{ts}_{safe}"


def save_backtest(
    result: BacktestResult,
    strategy_name: str,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    initial_capital: float = 100_000.0,
    run_id: str | None = None,
    market_return_pct: float | None = None,
) -> Path:
    """
    Save a backtest run to BACKTEST_RESULTS_DIR/<run_id>/.
    Writes: meta.json, trades.csv, equity_curve.csv.
    If market_return_pct is provided (buy-and-hold over same period), it is stored in meta.
    """
    run_id = run_id or _run_id(strategy_name, symbol)
    run_dir = BACKTEST_RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "run_id": run_id,
        "strategy": strategy_name,
        "symbol": symbol,
        "start": str(start) if start else None,
        "end": str(end) if end else None,
        "initial_capital": initial_capital,
        "stats": result.stats,
        "saved_at": datetime.now().isoformat(),
    }
    if market_return_pct is not None:
        meta["market_return_pct"] = market_return_pct
    with open(run_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    if not result.trades.empty:
        result.trades.to_csv(run_dir / "trades.csv", index=False)
    else:
        (run_dir / "trades.csv").write_text("entry_date,exit_date,entry_price,exit_price,pnl,return_pct\n")

    eq = result.equity_curve.reset_index()
    eq.columns = ["date", "equity"]
    eq["date"] = eq["date"].astype(str)
    eq.to_csv(run_dir / "equity_curve.csv", index=False)

    return run_dir


def list_runs() -> list[dict[str, Any]]:
    """
    List all saved backtest runs (newest first).
    Each item: run_id, path, meta (if meta.json exists).
    """
    runs = []
    if not BACKTEST_RESULTS_DIR.exists():
        return runs
    for path in sorted(BACKTEST_RESULTS_DIR.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        meta_path = path / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass
        runs.append({"run_id": path.name, "path": path, "meta": meta})
    return runs


def load_backtest(run_id: str) -> tuple[BacktestResult, dict[str, Any]]:
    """
    Load a saved backtest by run_id.
    Returns (BacktestResult, meta dict).
    """
    run_dir = BACKTEST_RESULTS_DIR / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Backtest run not found: {run_id}")

    with open(run_dir / "meta.json") as f:
        meta = json.load(f)

    trades_path = run_dir / "trades.csv"
    if trades_path.exists():
        trades = pd.read_csv(trades_path)
        if "entry_date" in trades.columns:
            trades["entry_date"] = pd.to_datetime(trades["entry_date"])
        if "exit_date" in trades.columns:
            trades["exit_date"] = pd.to_datetime(trades["exit_date"])
    else:
        trades = pd.DataFrame(columns=["entry_date", "exit_date", "entry_price", "exit_price", "pnl", "return_pct"])

    eq_path = run_dir / "equity_curve.csv"
    if eq_path.exists():
        eq_df = pd.read_csv(eq_path)
        eq_df["date"] = pd.to_datetime(eq_df["date"])
        equity_curve = eq_df.set_index("date")["equity"]
    else:
        equity_curve = pd.Series(dtype=float)

    stats = meta.get("stats", {})
    result = BacktestResult(trades=trades, equity_curve=equity_curve, stats=stats)
    return result, meta
