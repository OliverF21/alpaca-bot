"""
CLI entry point for the Strategy IDE.
Modes: backtest | paper | live

Usage:
  python main.py --mode backtest --strategy mean_reversion --symbol SPY --start 2023-01-01
  python main.py --mode backtest --strategy mean_reversion --universe large_cap --resolution 15  # screener universe, 15m bars
  python main.py --mode paper --strategy mean_reversion
  python main.py --mode live --strategy mean_reversion   # prompts for confirmation
"""

import argparse
import os
import sys
import time
from pathlib import Path
from datetime import date, timedelta

# Load .env before any imports that read env (e.g. config, fetcher)
_ROOT = Path(__file__).resolve().parent
_env_file = _ROOT / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)
except ImportError:
    pass
# Fallback: if dotenv didn't set FINNHUB_API_KEY (e.g. encoding), parse .env ourselves
if not os.environ.get("FINNHUB_API_KEY", "").strip() and _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("FINNHUB_API_KEY=") and "=" in line:
                    _, _, value = line.partition("=")
                    if value.strip():
                        os.environ["FINNHUB_API_KEY"] = value.strip()
                    break
    except Exception:
        pass

import pandas as pd

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import DEFAULT_SYMBOL, DEFAULT_UNIVERSE, BACKTEST_UNIVERSES, require_alpaca_credentials
from data.fetcher import fetch_bars
from strategies.mean_reversion import MeanReversionStrategy
from strategies.template import TemplateStrategy
from strategies.vwap_reversion import VWAPReversionStrategy
from backtester.engine import run_backtest, aggregate_backtest_results
from backtester.results import save_backtest

STRATEGIES = {
    "mean_reversion": MeanReversionStrategy,
    "vwap_reversion": VWAPReversionStrategy,
    "template": TemplateStrategy,
}

# Default backtest window: 4 years (daily); 90 days for 15m (more bars, more trades)
_BACKTEST_DAYS = 4 * 365
_BACKTEST_DAYS_15M = 90
_RATE_LIMIT_DELAY = 1.1  # seconds between symbols when fetching universe


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def cmd_backtest(args: argparse.Namespace) -> None:
    strategy_cls = STRATEGIES.get(args.strategy)
    if not strategy_cls:
        print(f"Unknown strategy: {args.strategy}. Choose from: {list(STRATEGIES.keys())}", file=sys.stderr)
        sys.exit(1)

    # Default: 15m for universe (more trades/day), daily for single-symbol
    resolution = (args.resolution or ("15" if args.universe else "D")).strip().upper()
    if resolution not in ("1", "5", "15", "30", "60", "D"):
        resolution = "15"
    # 15m default period shorter so we get enough bars without huge fetch
    default_days = _BACKTEST_DAYS_15M if resolution != "D" else _BACKTEST_DAYS
    start = args.start or (date.today() - timedelta(days=default_days))
    end = args.end or date.today()

    if args.universe:
        # Multi-symbol backtest: screener universe (same as live scanner)
        universe_name = args.universe.lower()
        if universe_name not in BACKTEST_UNIVERSES:
            print(f"Unknown universe: {args.universe}. Choose from: {list(BACKTEST_UNIVERSES.keys())}", file=sys.stderr)
            sys.exit(1)
        symbols = BACKTEST_UNIVERSES[universe_name]
        print(f"Backtesting {args.strategy} on universe '{args.universe}' ({len(symbols)} symbols) from {start} to {end} (resolution={resolution})...")
        strategy = strategy_cls()
        capital_per_symbol = args.capital / len(symbols)
        results_by_symbol = {}
        tf = "1Day" if resolution == "D" else ("1Hour" if resolution == "60" else f"{resolution}Min")
        for i, symbol in enumerate(symbols):
            try:
                df = fetch_bars(symbol, start, end, timeframe=tf, use_cache=False)
            except Exception:
                continue
            if df is None or df.empty or len(df) < 30:
                continue
            res = run_backtest(df, strategy, initial_capital=capital_per_symbol)
            res.trades["symbol"] = symbol
            results_by_symbol[symbol] = res
            if i < len(symbols) - 1:
                time.sleep(_RATE_LIMIT_DELAY)
        result = aggregate_backtest_results(results_by_symbol, args.capital)
        # Market: use SPY as proxy
        try:
            df_spy = fetch_bars("SPY", start, end, timeframe="1Day" if resolution == "D" else f"{resolution}Min", use_cache=False)
            if df_spy is not None and not df_spy.empty and "close" in df_spy.columns:
                close = df_spy["close"].sort_index()
                market_return_pct = (float(close.iloc[-1]) / float(close.iloc[0])) - 1.0
            else:
                market_return_pct = None
        except Exception:
            market_return_pct = None
        run_dir = save_backtest(
            result,
            strategy_name=args.strategy,
            symbol=f"universe_{universe_name}",
            start=str(start),
            end=str(end),
            initial_capital=args.capital,
            market_return_pct=market_return_pct,
        )
        # Trades per day
        if not result.trades.empty and "exit_date" in result.trades.columns:
            result.trades["exit_date"] = pd.to_datetime(result.trades["exit_date"])
            trades_per_day = result.trades.groupby(result.trades["exit_date"].dt.date).size()
            avg_trades_per_day = float(trades_per_day.mean()) if len(trades_per_day) else 0
        else:
            avg_trades_per_day = 0
        print(f"\nSaved to: {run_dir}")
        print(f"  Symbols with data: {len(results_by_symbol)}")
        print(f"  Total trades: {result.stats['num_trades']}")
        print(f"  Avg trades/day: {avg_trades_per_day:.1f}")
        if market_return_pct is not None:
            print("\n--- Strategy vs market (SPY) ---")
            print(f"  Strategy return:     {result.stats['total_return_pct']*100:.2f}%")
            print(f"  Market (buy & hold): {market_return_pct*100:.2f}%")
        print("\n--- Stats ---")
        _print_stats(result.stats)
        if not result.trades.empty:
            print(result.trades.head(20).to_string())
            if len(result.trades) > 20:
                print(f"  ... and {len(result.trades) - 20} more")
        return

    # Single-symbol backtest
    symbol = args.symbol or DEFAULT_SYMBOL
    print(f"Backtesting {args.strategy} on {symbol} from {start} to {end} (resolution={resolution})...")
    tf = "1Day" if resolution == "D" else ("1Hour" if resolution == "60" else f"{resolution}Min")
    df = fetch_bars(symbol, start, end, timeframe=tf, use_cache=False)
    strategy = strategy_cls()
    result = run_backtest(df, strategy, initial_capital=args.capital)
    close = df["close"].sort_index()
    market_return_pct = (float(close.iloc[-1]) / float(close.iloc[0])) - 1.0
    run_dir = save_backtest(
        result,
        strategy_name=args.strategy,
        symbol=symbol,
        start=str(start),
        end=str(end),
        initial_capital=args.capital,
        market_return_pct=market_return_pct,
    )
    print(f"\nSaved to: {run_dir}")
    print("\n--- Strategy vs market ---")
    print(f"  Strategy return:     {result.stats['total_return_pct']*100:.2f}%")
    print(f"  Market (buy & hold): {market_return_pct*100:.2f}%")
    print(f"  Difference:          {(result.stats['total_return_pct'] - market_return_pct)*100:+.2f}%")
    print("\n--- Stats ---")
    _print_stats(result.stats)
    print(f"\nTrades: {len(result.trades)}")
    if not result.trades.empty:
        print(result.trades.to_string())


def _print_stats(stats: dict) -> None:
    pct_keys = {"total_return_pct", "max_drawdown_pct", "avg_win_pct", "avg_loss_pct"}
    for k, v in stats.items():
        if isinstance(v, float) and k in pct_keys:
            print(f"  {k}: {v*100:.2f}%")
        elif isinstance(v, float):
            print(f"  {k}: {v:.2f}")
        else:
            print(f"  {k}: {v}")


def _confirm_paper_or_live(mode: str, strategy_name: str, watchlist: list) -> bool:
    """
    Print mode, strategy params, and watchlist; require user to type 'yes' to continue.
    Returns True if confirmed, False otherwise.
    """
    strategy_cls = STRATEGIES.get(strategy_name)
    strategy = strategy_cls() if strategy_cls else None
    print()
    print("=" * 60)
    print(f"  MODE:    {mode.upper()}")
    print(f"  Strategy: {strategy_name}")
    if strategy:
        for k, v in strategy.describe().items():
            print(f"    {k}: {v}")
    print(f"  Watchlist: {watchlist[:10]}{'...' if len(watchlist) > 10 else ''}")
    if len(watchlist) > 10:
        print(f"            ({len(watchlist)} symbols total)")
    print("=" * 60)
    confirm = input("Type 'yes' to continue: ")
    return confirm.strip().lower() == "yes"


def cmd_paper(args: argparse.Namespace) -> None:
    """Run in paper trading mode (with confirmation)."""
    require_alpaca_credentials()
    watchlist = args.symbols or DEFAULT_UNIVERSE
    if not _confirm_paper_or_live("paper", args.strategy, watchlist):
        print("Aborted.")
        sys.exit(0)
    print("Paper trading mode. Strategy:", args.strategy)
    print("Universe:", watchlist)
    from execution.alpaca_broker import get_account_equity
    eq = get_account_equity()
    print(f"Account equity: ${eq:,.2f}")
    print("(Paper trading loop not implemented in this stub; use scanner/run_scanner.py or dashboard for signals.)")


def cmd_live(args: argparse.Namespace) -> None:
    """Run in live trading mode (with confirmation)."""
    require_alpaca_credentials()
    from config import ALPACA_PAPER
    if ALPACA_PAPER:
        print("Warning: ALPACA_PAPER is true in .env. Live orders will go to paper account.")
    watchlist = args.symbols or DEFAULT_UNIVERSE
    if not _confirm_paper_or_live("live", args.strategy, watchlist):
        print("Aborted.")
        sys.exit(0)
    print("Live trading mode. Strategy:", args.strategy)
    print("Universe:", watchlist)
    from execution.alpaca_broker import get_account_equity
    eq = get_account_equity()
    print(f"Account equity: ${eq:,.2f}")
    print("(Live trading loop not implemented in this stub; use scanner/run_scanner.py or dashboard for signals.)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy IDE CLI")
    parser.add_argument("--mode", choices=["backtest", "paper", "live"], required=True)
    parser.add_argument("--strategy", default="mean_reversion", choices=list(STRATEGIES.keys()))
    parser.add_argument("--symbol", default=None, help="Single symbol (backtest)")
    parser.add_argument("--universe", default=None, choices=list(BACKTEST_UNIVERSES.keys()), help="Screener universe for multi-symbol backtest (same as live)")
    parser.add_argument("--resolution", default=None, help="Bar size: D (daily) or 15 (15m). Default D for single-symbol, 15 for universe (more trades/day)")
    parser.add_argument("--symbols", nargs="+", default=None, help="Symbol list (paper/live)")
    parser.add_argument("--start", type=_parse_date, default=None, help="Backtest start YYYY-MM-DD")
    parser.add_argument("--end", type=_parse_date, default=None, help="Backtest end YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial capital for backtest")
    args = parser.parse_args()

    if args.mode == "backtest":
        cmd_backtest(args)
    elif args.mode == "paper":
        cmd_paper(args)
    else:
        cmd_live(args)


if __name__ == "__main__":
    main()
