"""
Indicator wrappers using pandas_ta.
All functions take a DataFrame with OHLCV columns and return the same DataFrame
with new indicator columns added (or a Series when a single series is returned).
"""

from typing import Optional

import pandas as pd

try:
    import pandas_ta as ta
except ImportError as e:
    raise ImportError(
        "pandas_ta is required for indicators. Install: pip install pandas-ta"
    ) from e


def bollinger_bands(
    df: pd.DataFrame,
    close_col: str = "close",
    length: int = 20,
    std: float = 2.0,
    prefix: str = "bb_",
) -> pd.DataFrame:
    """
    Add Bollinger Bands columns to df.

    New columns: {prefix}lower, {prefix}mid, {prefix}upper, {prefix}bandwidth, {prefix}basis.
    """
    out = ta.bbands(
        df[close_col],
        length=length,
        std=std,
    )
    if out is None or out.empty:
        raise ValueError("pandas_ta bbands returned empty. Check close_col and data.")
    # bbands returns DataFrame with columns like BBU_20_2.0, BBL_20_2.0, BBM_20_2.0
    cols = list(out.columns)
    rename = {}
    for c in cols:
        if "BBL" in c or "lower" in c.lower():
            rename[c] = f"{prefix}lower"
        elif "BBU" in c or "upper" in c.lower():
            rename[c] = f"{prefix}upper"
        elif "BBM" in c or "mid" in c.lower() or "basis" in c.lower():
            rename[c] = f"{prefix}mid"
        elif "BBB" in c or "bandwidth" in c.lower():
            rename[c] = f"{prefix}bandwidth"
        elif "BBP" in c or "percent" in c.lower():
            rename[c] = f"{prefix}pct"
    out = out.rename(columns=rename)
    for c in out.columns:
        if c not in df.columns:
            df = df.copy()
            df[c] = out[c]
    return df


def rsi(
    df: pd.DataFrame,
    close_col: str = "close",
    length: int = 14,
    col_name: str = "rsi",
) -> pd.DataFrame:
    """Add RSI column to df."""
    s = ta.rsi(df[close_col], length=length)
    if s is None:
        raise ValueError("pandas_ta rsi returned empty.")
    df = df.copy()
    df[col_name] = s
    return df


def macd(
    df: pd.DataFrame,
    close_col: str = "close",
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    prefix: str = "macd_",
) -> pd.DataFrame:
    """
    Add MACD line, signal line, and histogram to df.

    New columns: {prefix}macd, {prefix}signal, {prefix}hist.
    """
    out = ta.macd(df[close_col], fast=fast, slow=slow, signal=signal)
    if out is None or out.empty:
        raise ValueError("pandas_ta macd returned empty.")
    # MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
    rename = {}
    for c in out.columns:
        if "MACD_" in c and "h" not in c.lower() and "s" not in c.lower():
            rename[c] = f"{prefix}macd"
        elif "MACDs" in c or "signal" in c.lower():
            rename[c] = f"{prefix}signal"
        elif "MACDh" in c or "hist" in c.lower():
            rename[c] = f"{prefix}hist"
    out = out.rename(columns=rename)
    for c in out.columns:
        if c not in df.columns:
            df = df.copy()
            df[c] = out[c]
    return df


def atr(
    df: pd.DataFrame,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    length: int = 14,
    col_name: str = "atr",
) -> pd.DataFrame:
    """Add ATR (Average True Range) column to df."""
    s = ta.atr(df[high_col], df[low_col], df[close_col], length=length)
    if s is None:
        raise ValueError("pandas_ta atr returned empty.")
    df = df.copy()
    df[col_name] = s
    return df
