"""
diagnostics/strategy_diagnostics.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run this BEFORE touching any strategy code.
It will tell you exactly where the pipeline is breaking.

Uses the strategy_ide framework: config, data.fetcher, strategies.mean_reversion.

Run from repo root (alpaca_bot):
    python diagnostics/strategy_diagnostics.py

Or from strategy_ide:
    python ../diagnostics/strategy_diagnostics.py

What it checks:
    1. Data fetch — bars from strategy_ide fetcher (same as backtest/dashboard).
    2. Indicator values — BB, RSI, bb_pct_b producing real numbers?
    3. Entry conditions — %B < entry_pct_b_max, RSI < buy_rsi (and optional volume).
    4. Exit conditions — close >= bb_mid or RSI > sell_rsi.
    5. Signal pipeline — generate_signals output and enter/exit/hold counts.
    6. Timeframe sanity — matches framework default (daily).
"""

import sys
import os
from pathlib import Path
from datetime import date, timedelta

# Add strategy_ide so we use the framework's config, fetcher, and strategy
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STRATEGY_IDE = _REPO_ROOT / "strategy_ide"
if _STRATEGY_IDE.is_dir() and str(_STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(_STRATEGY_IDE))

import pandas as pd
from dotenv import load_dotenv

# Load .env from strategy_ide (same as framework)
_env_path = _STRATEGY_IDE / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv(_REPO_ROOT / ".env")

from config import DEFAULT_SYMBOL, DEFAULT_TIMEFRAME
from data.fetcher import fetch_bars
from strategies.mean_reversion import MeanReversionStrategy

SEP  = "─" * 55
SEP2 = "━" * 55


def header(title: str) -> None:
    print(f"\n{SEP2}")
    print(f"  {title}")
    print(SEP2)


def ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️   {msg}")


def fail(msg: str) -> None:
    print(f"  ❌  {msg}")


def info(msg: str) -> None:
    print(f"  ℹ️   {msg}")


def run_diagnostics(
    symbol: str = None,
    timeframe: str = None,
    start: date = None,
    end: date = None,
) -> None:
    """
    Run diagnostics against the strategy_ide framework.
    Uses config.DEFAULT_SYMBOL and DEFAULT_TIMEFRAME if not provided.
    """
    symbol = symbol or DEFAULT_SYMBOL
    timeframe = timeframe or DEFAULT_TIMEFRAME
    end = end or date.today()
    # Default: ~4 years of daily bars (same as framework backtest default)
    if start is None:
        start = end - timedelta(days=4 * 365)

    strat = MeanReversionStrategy()
    required = ["bb_lower", "bb_mid", "bb_upper", "rsi", "bb_pct_b"]

    # ── Check 1: Data (framework fetcher) ─────────────────────────────────────
    header("CHECK 1 — Data Fetch (strategy_ide data.fetcher)")
    try:
        df = fetch_bars(symbol, start, end, timeframe=timeframe, use_cache=False)
        if df is None or df.empty:
            fail("Fetched DataFrame is empty.")
            return
        # Normalize column names to lowercase
        df = df.rename(columns={c: c.lower() for c in df.columns})
        ok(f"Fetched {len(df)} bars for {symbol}")
        info(f"Timeframe : {timeframe}")
        info(f"Date range: {df.index[0]}  →  {df.index[-1]}")
        if "close" in df.columns:
            info(f"Close range: ${df['close'].min():.2f} – ${df['close'].max():.2f}")

        if len(df) < 50:
            fail("Too few bars — indicators need at least 50. Widen date range.")
            return
    except Exception as e:
        fail(f"Data fetch failed: {e}")
        import traceback
        traceback.print_exc()
        return

    # ── Check 2: Indicators ───────────────────────────────────────────────────
    header("CHECK 2 — Indicator Values")
    try:
        df_ind = strat.populate_indicators(df.copy())

        for col in ["bb_upper", "bb_mid", "bb_lower", "bb_pct_b", "rsi"]:
            if col not in df_ind.columns:
                fail(f"Missing column: {col}")
            else:
                n_nan = df_ind[col].isna().sum()
                valid = df_ind[col].dropna()
                rng = f"{valid.min():.2f} – {valid.max():.2f}" if len(valid) else "N/A"
                if n_nan > 30:
                    warn(f"{col:<12} range={rng}  NaNs={n_nan}  (high NaN count — lookback?)")
                else:
                    ok(f"{col:<12} range={rng}  NaNs={n_nan}")

        latest = df_ind.iloc[-1]
        print()
        info(f"Latest close      : {latest['close']:.2f}")
        info(f"Latest bb_lower   : {latest['bb_lower']:.2f}")
        info(f"Latest bb_mid     : {latest['bb_mid']:.2f}")
        info(f"Latest bb_upper   : {latest['bb_upper']:.2f}")
        info(f"Latest bb_pct_b   : {latest.get('bb_pct_b', float('nan')):.3f}  (0=lower band, 1=upper)")
        info(f"Latest rsi        : {latest['rsi']:.2f}")

    except Exception as e:
        fail(f"populate_indicators() crashed: {e}")
        import traceback
        traceback.print_exc()
        return

    # ── Check 3: Entry conditions (framework logic: %B + RSI, optional volume) ───
    header("CHECK 3 — Entry Condition Breakdown")
    info(f"Strategy entry: bb_pct_b < {strat.entry_pct_b_max}  AND  RSI < {strat.buy_rsi}")
    if strat.use_volume_filter:
        info("Volume filter ON: volume > volume_sma20 required.")

    cond_pct_b = df_ind["bb_pct_b"].lt(strat.entry_pct_b_max)
    cond_rsi   = df_ind["rsi"].lt(strat.buy_rsi)
    cond_nan   = df_ind[required].notna().all(axis=1)
    cond_all   = cond_pct_b & cond_rsi & cond_nan
    if strat.use_volume_filter and "volume_sma20" in df_ind.columns:
        cond_all = cond_all & df_ind["volume"].gt(df_ind["volume_sma20"])

    n_pct_b = cond_pct_b.sum()
    n_rsi   = cond_rsi.sum()
    n_all   = cond_all.sum()
    pct_pct_b = cond_pct_b.mean() * 100
    pct_rsi   = cond_rsi.mean() * 100
    pct_all   = cond_all.mean() * 100

    info(f"Bars where bb_pct_b < {strat.entry_pct_b_max}     : {n_pct_b:>5}  ({pct_pct_b:.1f}%)")
    info(f"Bars where RSI < {strat.buy_rsi}                    : {n_rsi:>5}  ({pct_rsi:.1f}%)")
    info(f"Bars where BOTH + valid (entry zone)               : {n_all:>5}  ({pct_all:.1f}%)")

    print()
    if n_all == 0:
        fail("ZERO entry bars — conditions never fire together.")
        if pct_pct_b < 2:
            warn(f"%B < {strat.entry_pct_b_max} is very rare ({pct_pct_b:.1f}%). "
                 f"Try raising entry_pct_b_max (e.g. 0.4 or 0.5).")
        if pct_rsi < 2:
            warn(f"RSI < {strat.buy_rsi} is very rare ({pct_rsi:.1f}%). "
                 f"Try raising buy_rsi (e.g. 45) or use more history.")
    elif pct_all < 0.5:
        warn(f"Very few entry bars ({pct_all:.2f}%). Strategy will trade rarely.")
        info("Consider: raise buy_rsi or entry_pct_b_max, or add more symbols.")
    else:
        ok(f"Entry conditions firing {pct_all:.1f}% of bars — healthy signal frequency.")

    # ── Check 4: Exit conditions ──────────────────────────────────────────────
    header("CHECK 4 — Exit Condition Breakdown")
    cond_exit_bb  = df_ind["close"] >= df_ind["bb_mid"]
    cond_exit_rsi = df_ind["rsi"] > strat.sell_rsi
    cond_exit_all = cond_exit_bb | cond_exit_rsi

    info(f"Bars where close >= bb_mid             : {cond_exit_bb.sum():>5}  ({cond_exit_bb.mean()*100:.1f}%)")
    info(f"Bars where RSI > {strat.sell_rsi}                    : {cond_exit_rsi.sum():>5}  ({cond_exit_rsi.mean()*100:.1f}%)")
    info(f"Bars where exit fires (either)         : {cond_exit_all.sum():>5}  ({cond_exit_all.mean()*100:.1f}%)")

    # ── Check 5: Signal pipeline ──────────────────────────────────────────────
    header("CHECK 5 — Signal Pipeline")
    try:
        df_sig = strat.generate_signals(df_ind.copy())

        if "signal" not in df_sig.columns:
            fail("'signal' column missing from generate_signals() output.")
        else:
            n_enter = (df_sig["signal"] == 1).sum()
            n_exit  = (df_sig["signal"] == -1).sum()
            n_hold  = (df_sig["signal"] == 0).sum()
            ok("Signal column present")
            info(f"  Enter signals : {n_enter}")
            info(f"  Exit  signals : {n_exit}")
            info(f"  Hold          : {n_hold}")

            if n_enter == 0:
                fail("No entry signals. Issue is in entry conditions (%B + RSI), not pipeline.")
            elif n_exit == 0 and n_enter > 0:
                warn("No exit signals — positions would hold until stop/TP. Check sell_rsi / bb_mid.")
            else:
                ok("Pipeline looks healthy end-to-end.")
    except Exception as e:
        fail(f"generate_signals() crashed: {e}")
        info("Usually means populate_indicators() output is missing expected columns.")
        import traceback
        traceback.print_exc()
        return

    # ── Check 6: Timeframe (framework default = daily) ─────────────────────────
    header("CHECK 6 — Timeframe (framework default)")
    info(f"Framework DEFAULT_TIMEFRAME = {DEFAULT_TIMEFRAME}")
    if timeframe == "1Day" or "Day" in str(timeframe):
        ok("Daily bars — framework default. Mean reversion uses entry_pct_b_max + buy_rsi=40 for frequency.")
        info("To get more signals on daily: raise entry_pct_b_max (e.g. 0.4) or buy_rsi (e.g. 45).")
    elif "Hour" in str(timeframe):
        ok("Hourly bars — more signals; ensure enough history for BB/RSI warmup.")
    elif "Min" in str(timeframe):
        ok("Minute bars — most signals; consider tighter entry_pct_b_max to reduce noise.")

    # ── Summary ───────────────────────────────────────────────────────────────
    header("SUMMARY & RECOMMENDATIONS")
    if n_all == 0:
        fail("Strategy is not firing. Likely fixes:")
        print("  1. Raise entry_pct_b_max (e.g. 0.4 or 0.5)")
        print("  2. Raise buy_rsi (e.g. 40 → 45)")
        print("  3. Widen date range (more bars)")
        print("  4. Run Optimize page in dashboard to tune params")
    elif n_all < 5:
        warn("Strategy fires rarely. Likely fixes:")
        print("  1. Raise entry_pct_b_max or buy_rsi slightly")
        print("  2. Add more symbols to universe")
        print("  3. Run Optimize in dashboard for this symbol/period")
    else:
        ok("Signal frequency looks reasonable.")
        info("If returns are poor, run dashboard Optimize page to hyperopt params.")

    print(f"\n{SEP2}\n")


if __name__ == "__main__":
    run_diagnostics(
        symbol=DEFAULT_SYMBOL,
        timeframe=DEFAULT_TIMEFRAME,
        # start/end default to last ~4 years in run_diagnostics()
    )
