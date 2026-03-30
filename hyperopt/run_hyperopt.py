"""
hyperopt/run_hyperopt.py
━━━━━━━━━━━━━━━━━━━━━━━━
Example: optimize MeanReversionStrategy on SPY then validate walk-forward.

Run from repo root:
    python hyperopt/run_hyperopt.py
"""

import sys
import os
_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
sys.path.insert(0, _REPO)
sys.path.insert(0, _STRATEGY_IDE)

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_STRATEGY_IDE, ".env"))

from strategy_ide.data.fetcher import fetch_bars
from strategy_ide.strategies.mean_reversion import MeanReversionStrategy
from strategy_ide.optimization.hyperopt_runner import HyperoptRunner, MEAN_REVERSION_SPACE


if __name__ == "__main__":
    # ── 1. Fetch data (Finnhub: hourly = resolution "60") ──────────────────
    print("Fetching SPY hourly data...")
    df = fetch_bars("SPY", resolution="60", n_bars=1000)

    # ── 2. Single optimize + OOS test ─────────────────────────────────────
    runner = HyperoptRunner(
        strategy_class = MeanReversionStrategy,
        df             = df,
        search_space   = MEAN_REVERSION_SPACE,
        max_evals      = 200,
        objective      = "sharpe_ratio",   # or: "total_return", "profit_factor"
        train_pct      = 0.70,
        commission     = 0.001,
    )

    best_params, result = runner.optimize(verbose=True)

    # ── 3. Walk-forward validation ─────────────────────────────────────────
    print("\nRunning walk-forward optimization...")
    wf_results = runner.walk_forward(n_splits=5, verbose=True)

    # ── 4. Deploy best params to live strategy ─────────────────────────────
    print(f"\nBest params to deploy: {best_params}")
    strat = MeanReversionStrategy(**best_params)
    print(strat)
