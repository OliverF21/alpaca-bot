"""
strategy_ide/research/crypto_research.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automated multi-pair × multi-strategy research pipeline.

Runs hyperopt (TPE Bayesian search) across every pair × strategy
combination, then ranks results by out-of-sample Sharpe ratio.

Usage:
    cd /Users/oliver/alpaca_bot
    python strategy_ide/research/crypto_research.py

    # Quick smoke test (2 pairs × 2 strategies × 20 trials):
    python strategy_ide/research/crypto_research.py --quick

Output:
    strategy_ide/research/crypto_results.json  — full results, machine-readable
    (also prints a ranked table to stdout)

Results are appended on re-runs so you can build up a record over time.
Use --overwrite to start fresh.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

_REPO = Path(__file__).resolve().parent.parent.parent
_IDE  = Path(__file__).resolve().parent.parent
for p in [str(_REPO), str(_IDE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(_REPO / ".env")

import pandas as pd

from data.crypto_fetcher import fetch_crypto_bars_range
from backtester.engine import run_backtest
from optimization.hyperopt_runner import (
    HyperoptRunner,
    CRYPTO_MR_SPACE,
    CRYPTO_TREND_SPACE,
    CRYPTO_BREAKOUT_SPACE,
    CRYPTO_SUPERTREND_SPACE,
)
from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
from strategies.crypto_breakout import CryptoBreakoutStrategy
from strategies.crypto_supertrend import CryptoSupertrendStrategy

# ── Configuration ─────────────────────────────────────────────────────────────

PAIRS = [
    # Tier 1: highest liquidity (previously tested)
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "AVAX/USD",
    "LINK/USD",
    # Tier 2: additional pairs (new)
    "DOGE/USD",
    "LTC/USD",
    "UNI/USD",
    "DOT/USD",
    "MATIC/USD",
    "XRP/USD",
    "AAVE/USD",
]

STRATEGIES = [
    {
        "name":  "crypto_mean_reversion",
        "class": CryptoMeanReversionStrategy,
        "space": CRYPTO_MR_SPACE,
        "label": "Mean Reversion",
    },
    {
        "name":  "crypto_trend_following",
        "class": CryptoTrendFollowingStrategy,
        "space": CRYPTO_TREND_SPACE,
        "label": "Trend Following",
    },
    {
        "name":  "crypto_breakout",
        "class": CryptoBreakoutStrategy,
        "space": CRYPTO_BREAKOUT_SPACE,
        "label": "Breakout",
    },
    {
        "name":  "crypto_supertrend",
        "class": CryptoSupertrendStrategy,
        "space": CRYPTO_SUPERTREND_SPACE,
        "label": "Supertrend",
    },
]

# Full research: 2021-01-01 → 2024-12-31
# Covers BTC bull run (2021), crypto crash (2022), recovery (2023-2024)
START       = "2021-01-01"
END         = "2024-12-31"
RESOLUTION  = "60"     # 1-hour bars
MAX_EVALS   = 100      # full run — increase for more thorough search
TRAIN_PCT   = 0.70     # 70% in-sample, 30% out-of-sample
MIN_TRADES  = 5        # skip trials with fewer trades (insufficient evidence)

# Quick mode
QUICK_PAIRS      = ["BTC/USD", "SOL/USD"]
QUICK_STRATEGIES = ["crypto_supertrend", "crypto_breakout"]
QUICK_MAX_EVALS  = 20

OUTPUT_FILE = Path(__file__).parent / "crypto_results.json"


# ── Research runner ────────────────────────────────────────────────────────────

def run_research(
    pairs: List[str],
    strategies: List[Dict],
    start: str,
    end: str,
    max_evals: int,
    train_pct: float = TRAIN_PCT,
    min_trades: int  = MIN_TRADES,
) -> List[Dict]:
    """Run hyperopt on all pair × strategy combinations. Returns sorted results."""

    results: List[Dict] = []
    total = len(pairs) * len(strategies)
    done  = 0

    print(f"\n{'━'*72}")
    print(f"  Crypto Strategy Research Pipeline")
    print(f"  {len(pairs)} pairs × {len(strategies)} strategies = {total} runs")
    print(f"  {max_evals} hyperopt trials each  |  period {start} → {end}")
    print(f"  Train/test split: {int(train_pct*100)}/{int((1-train_pct)*100)}")
    print(f"{'━'*72}\n")

    for pair in pairs:
        print(f"▶ Fetching {pair} data...")
        try:
            df = fetch_crypto_bars_range(pair, start, end, resolution=RESOLUTION)
        except Exception as e:
            print(f"  ✗ Data fetch failed for {pair}: {e}")
            continue

        if len(df) < 200:
            print(f"  ✗ Insufficient data for {pair}: {len(df)} bars")
            continue

        df.index = pd.to_datetime(df.index)
        print(f"  {len(df)} bars  ({df.index[0].date()} → {df.index[-1].date()})")

        # ── Buy-and-hold baseline for this pair ───────────────────────────────
        oos_start_idx = int(len(df) * TRAIN_PCT)
        df_oos_slice  = df.iloc[oos_start_idx:]
        bh_full_return = round((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100, 2)
        bh_oos_return  = round((df_oos_slice["close"].iloc[-1] / df_oos_slice["close"].iloc[0] - 1) * 100, 2)
        bh_full_dd = round(((df["close"] / df["close"].cummax()) - 1).min() * 100, 2)
        print(f"  B&H full={bh_full_return:+.1f}%  B&H OOS={bh_oos_return:+.1f}%  peak_dd={bh_full_dd:.1f}%", flush=True)

        for strat_cfg in strategies:
            done += 1
            tag = f"[{done}/{total}]"
            print(f"\n{tag} {pair} × {strat_cfg['label']}  ({max_evals} trials)...", flush=True)
            t0 = time.time()

            try:
                runner = HyperoptRunner(
                    strategy_class = strat_cfg["class"],
                    df             = df,
                    search_space   = strat_cfg["space"],
                    max_evals      = max_evals,
                    objective      = "sharpe_ratio",
                    train_pct      = train_pct,
                    min_trades     = min_trades,
                )
                best_params, report = runner.optimize()

                is_stats  = report.in_sample
                oos_stats = report.out_of_sample
                elapsed   = round(time.time() - t0, 1)

                # Baseline: default params (no optimization)
                strat_default = strat_cfg["class"]()
                df_ind = strat_default.populate_indicators(df.copy())
                df_sig = strat_default.generate_signals(df_ind)
                base_result = run_backtest(df_ind, strat_default, initial_capital=100_000, risk_pct=0.01)
                base_sharpe = round(base_result.stats.get("sharpe_ratio", 0), 3)

                entry: Dict[str, Any] = {
                    "pair":          pair,
                    "strategy":      strat_cfg["name"],
                    "strategy_label":strat_cfg["label"],
                    "is_sharpe":     round(is_stats.sharpe_ratio, 3),
                    "oos_sharpe":    round(oos_stats.sharpe_ratio, 3),
                    "is_return":     round(is_stats.total_return * 100, 2),
                    "oos_return":    round(oos_stats.total_return * 100, 2),
                    "bh_oos_return": bh_oos_return,
                    "vs_bh":         round(oos_stats.total_return * 100 - bh_oos_return, 2),
                    "bh_full_return":bh_full_return,
                    "is_drawdown":   round(is_stats.max_drawdown * 100, 2),
                    "oos_drawdown":  round(oos_stats.max_drawdown * 100, 2),
                    "is_trades":     is_stats.n_trades,
                    "oos_trades":    oos_stats.n_trades,
                    "is_win_rate":   round(is_stats.win_rate * 100, 1),
                    "oos_win_rate":  round(oos_stats.win_rate * 100, 1),
                    "overfit_gap":   round(abs(is_stats.sharpe_ratio - oos_stats.sharpe_ratio), 3),
                    "base_sharpe":   base_sharpe,  # unoptimized baseline
                    "best_params":   best_params,
                    "max_evals":     max_evals,
                    "period":        f"{start} → {end}",
                    "elapsed_sec":   elapsed,
                    "timestamp":     datetime.now().isoformat(),
                }
                results.append(entry)

                verdict = "✓ GOOD" if oos_stats.sharpe_ratio >= 0.8 else ("~ OK" if oos_stats.sharpe_ratio >= 0.5 else "✗ WEAK")
                vs_bh_str = f"{entry['vs_bh']:+.1f}% vs B&H"
                bh_flag   = "🏆 BEATS B&H" if entry["vs_bh"] > 0 else ""
                print(
                    f"  {verdict}  IS Sharpe={is_stats.sharpe_ratio:.3f}  "
                    f"OOS Sharpe={oos_stats.sharpe_ratio:.3f}  "
                    f"OOS return={oos_stats.total_return*100:+.1f}%  "
                    f"{vs_bh_str}  {bh_flag}  ({elapsed}s)",
                    flush=True,
                )

            except Exception as e:
                elapsed = round(time.time() - t0, 1)
                print(f"  ✗ Failed: {e}  ({elapsed}s)")
                results.append({
                    "pair": pair,
                    "strategy": strat_cfg["name"],
                    "strategy_label": strat_cfg["label"],
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

    return results


def print_ranking(results: List[Dict]):
    """Print a human-readable ranked table."""
    valid = [r for r in results if "oos_sharpe" in r]
    valid.sort(key=lambda x: x["oos_sharpe"], reverse=True)

    print(f"\n{'━'*90}")
    print(f"  Results ranked by Out-of-Sample Sharpe Ratio")
    print(f"{'━'*90}")
    header = f"{'Pair':<12}{'Strategy':<22}{'IS Shr':>7}{'OOS Shr':>8}{'OOS Ret':>9}{'vs B&H':>9}{'Trades':>7}{'WinRate':>9}"
    print(header)
    print("─" * 97)
    for r in valid:
        verdict = "✓" if r["oos_sharpe"] >= 0.8 else ("~" if r["oos_sharpe"] >= 0.5 else "✗")
        vs_bh   = r.get("vs_bh", float("nan"))
        bh_flag = "🏆" if vs_bh > 0 else "  "
        print(
            f"{verdict} {r['pair']:<10}"
            f"{r['strategy_label']:<22}"
            f"{r['is_sharpe']:>7.3f}"
            f"{r['oos_sharpe']:>8.3f}"
            f"{r['oos_return']:>+8.1f}%"
            f"{vs_bh:>+8.1f}%"
            f"  {bh_flag}"
            f"{r['oos_trades']:>6}"
            f"{r['oos_win_rate']:>8.1f}%"
        )
    print("─" * 97)
    print(f"\nLegend: ✓ OOS Sharpe ≥ 0.8 (launch-ready)  ~ ≥ 0.5 (acceptable)  ✗ < 0.5 (weak)  🏆 beats B&H\n")

    # Best config summary
    if valid:
        best = valid[0]
        print(f"Best configuration: {best['pair']} × {best['strategy_label']}")
        print(f"  OOS Sharpe: {best['oos_sharpe']}  |  OOS Return: {best['oos_return']:+.1f}%  |  Overfit gap: {best['overfit_gap']}")
        print(f"  Best params: {json.dumps(best['best_params'], indent=4)}")


def save_results(results: List[Dict], output_file: Path, overwrite: bool = False):
    """Save results to JSON, appending to existing file unless overwrite=True."""
    if output_file.exists() and not overwrite:
        with open(output_file) as f:
            existing = json.load(f)
        # Deduplicate by (pair, strategy, timestamp prefix to hour)
        existing_keys = {(r["pair"], r["strategy"]): i for i, r in enumerate(existing)}
        for r in results:
            key = (r.get("pair"), r.get("strategy"))
            if key in existing_keys:
                existing[existing_keys[key]] = r  # update in-place
            else:
                existing.append(r)
        results = existing

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {output_file}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto strategy research pipeline")
    parser.add_argument("--quick",      action="store_true", help="Quick mode: 2 pairs, 2 strategies, 20 trials")
    parser.add_argument("--overwrite",  action="store_true", help="Overwrite existing results (default: append/update)")
    parser.add_argument("--evals",      type=int, default=None, help="Override max_evals")
    parser.add_argument("--start",      type=str, default=START, help=f"Start date (default: {START})")
    parser.add_argument("--end",        type=str, default=END,   help=f"End date (default: {END})")
    parser.add_argument("--pairs",      type=str, default=None, help="Comma-separated pairs to run, e.g. SOL/USD,AVAX/USD")
    parser.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy names, e.g. crypto_mean_reversion,crypto_breakout")
    args = parser.parse_args()

    if args.quick:
        pairs      = QUICK_PAIRS
        strats     = [s for s in STRATEGIES if s["name"] in QUICK_STRATEGIES]
        max_evals  = args.evals or QUICK_MAX_EVALS
        print("Quick mode: 2 pairs × 2 strategies × 20 trials")
    else:
        pairs      = PAIRS if not args.pairs else [p.strip() for p in args.pairs.split(",")]
        strat_filter = set(args.strategies.split(",")) if args.strategies else None
        strats     = STRATEGIES if not strat_filter else [s for s in STRATEGIES if s["name"] in strat_filter]
        max_evals  = args.evals or MAX_EVALS

    results = run_research(
        pairs      = pairs,
        strategies = strats,
        start      = args.start,
        end        = args.end,
        max_evals  = max_evals,
    )

    valid = [r for r in results if "oos_sharpe" in r]
    if valid:
        print_ranking(valid)
        save_results(results, OUTPUT_FILE, overwrite=args.overwrite)
    else:
        print("\nNo valid results — check for data or strategy errors above.")
