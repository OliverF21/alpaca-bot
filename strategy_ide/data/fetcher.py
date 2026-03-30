"""
data/fetcher.py
━━━━━━━━━━━━━━━
Dual data source:
  • fetch_bars_range  — Alpaca Data API (historical, up to 4+ years of intraday)
  • fetch_bars        — yfinance for last-N-bars (live scanner, no auth needed)
  • fetch_bars_bulk   — thin wrapper over fetch_bars

Why two sources?
  - Alpaca: provides years of 15-min bars via their free data subscription.
    Already authenticated — no extra API key needed beyond your trading keys.
  - yfinance: zero-config, great for the live scanner's 60-bar lookback window.
    Limited to 60 days of intraday, so NOT suitable for multi-year backtests.

Resolution strings used throughout the codebase (Finnhub legacy):
  "1"  = 1-minute   "5"  = 5-minute   "15" = 15-minute
  "30" = 30-minute  "60" = 60-minute  "D"  = daily
"""

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

# ── Load .env so keys are available when the module is imported standalone ─────
_ROOT = Path(__file__).resolve().parent.parent  # strategy_ide/
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT.parent / ".env", override=False)  # repo root fallback

log = logging.getLogger(__name__)

_BULK_DELAY = 0.3  # seconds between symbols in fetch_bars_bulk

# ── Resolution / interval mappings ───────────────────────────────────────────

# Finnhub-style resolution strings → yfinance interval strings (for live fetch)
_RESOLUTION_TO_YF = {
    "1":  "1m",
    "5":  "5m",
    "15": "15m",
    "30": "30m",
    "60": "60m",
    "D":  "1d",
    "W":  "1wk",
    "M":  "1mo",
}

# Legacy timeframe strings (used by main.py --resolution flag) → yf interval
_TIMEFRAME_TO_YF = {
    "1Min":  "1m",  "1min":  "1m",
    "5Min":  "5m",  "5min":  "5m",
    "15Min": "15m", "15min": "15m",
    "30Min": "30m", "30min": "30m",
    "1Hour": "60m", "1hour": "60m",
    "1Day":  "1d",  "1day":  "1d",
}

# How many minutes each resolution represents (used to calculate lookback)
_RESOLUTION_MINUTES = {
    "1": 1, "5": 5, "15": 15, "30": 30,
    "60": 60, "D": 1440, "W": 10080, "M": 43200,
}

# Resolutions that Alpaca supports as intraday bars
_ALPACA_INTRADAY_RESOLUTIONS = {"1", "5", "15", "30", "60"}


def _to_yf_interval(resolution: str) -> str:
    if resolution in _RESOLUTION_TO_YF:
        return _RESOLUTION_TO_YF[resolution]
    if resolution in _TIMEFRAME_TO_YF:
        return _TIMEFRAME_TO_YF[resolution]
    return resolution  # pass through ("15m", "1d", etc.)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise any raw OHLCV DataFrame into the standard format:
      - Column names lowercased
      - Keep only open, high, low, close, volume
      - Index: timezone-naive DatetimeIndex named "timestamp"
      - Drop NaN rows, sort ascending
    """
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = float("nan")
    df = df[["open", "high", "low", "close", "volume"]]

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df = df.dropna()
    df = df.sort_index()
    df.index.name = "timestamp"
    return df


# ── Alpaca historical client (lazy init) ─────────────────────────────────────

_alpaca_client = None


def _get_alpaca_client():
    """
    Return (and cache) a StockHistoricalDataClient.
    Reads keys from environment — works with any .env that has
    ALPACA_API_KEY and ALPACA_SECRET_KEY.
    """
    global _alpaca_client
    if _alpaca_client is not None:
        return _alpaca_client
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        api_key    = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            log.warning("Alpaca API keys not found — historical fetch will use yfinance fallback")
            return None
        _alpaca_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        return _alpaca_client
    except ImportError:
        log.warning("alpaca-py not installed — historical fetch will use yfinance fallback")
        return None


def _fetch_range_alpaca(
    symbol: str,
    start: str,  # "YYYY-MM-DD"
    end: str,    # "YYYY-MM-DD" (inclusive)
    resolution: str,
) -> Optional[pd.DataFrame]:
    """
    Fetch intraday bars from Alpaca Data API.
    Returns None if Alpaca is unavailable or returns no data.

    Beginner note:
      Alpaca returns a DataFrame with a MultiIndex: (symbol, timestamp).
      We drop the symbol level and keep only the timestamp as the index.
    """
    client = _get_alpaca_client()
    if client is None:
        return None

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed

        _TF_MAP = {
            "1":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15": TimeFrame(15, TimeFrameUnit.Minute),
            "30": TimeFrame(30, TimeFrameUnit.Minute),
            "60": TimeFrame(1,  TimeFrameUnit.Hour),
        }
        tf = _TF_MAP.get(resolution)
        if tf is None:
            return None

        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # end is inclusive — Alpaca's end is also inclusive for bars
        end_dt   = datetime.strptime(end,   "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )

        req  = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start_dt,
            end=end_dt,
            adjustment="all",
            feed=DataFeed.IEX,   # free-tier feed (SIP requires paid subscription)
        )
        bars = client.get_stock_bars(req)
        raw  = bars.df

        if raw.empty:
            return None

        # Drop the outer "symbol" level of the MultiIndex
        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.droplevel(0)

        return _clean(raw)

    except Exception as e:
        log.warning(f"  {symbol}: Alpaca fetch failed — {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_bars_range(
    symbol: str,
    start: Union[str, "datetime", "date"],
    end: Union[str, "datetime", "date"],
    resolution: str = "D",
) -> pd.DataFrame:
    """
    Fetch OHLCV bars for a date range — used by backtests and the dashboard.

    For intraday resolutions (1/5/15/30/60) this uses the Alpaca Data API,
    which provides years of history on the free plan.
    For daily/weekly/monthly bars it falls back to yfinance (unlimited history).

    Parameters
    ----------
    symbol     : Ticker, e.g. "SPY"
    start, end : Date range as "YYYY-MM-DD" string, date, or datetime
    resolution : "1", "5", "15", "30", "60", "D" (Finnhub-style)

    Returns
    -------
    pd.DataFrame  columns: open, high, low, close, volume
                  index: timezone-naive DatetimeIndex (sorted ascending)
    """
    # Normalise start/end to "YYYY-MM-DD" strings
    def _to_str(d):
        if isinstance(d, datetime):
            return d.strftime("%Y-%m-%d")
        if isinstance(d, date):
            return d.strftime("%Y-%m-%d")
        return d  # already a string

    start_str = _to_str(start)
    end_str   = _to_str(end)

    log.debug(f"  {symbol}: fetch_bars_range {resolution} {start_str}→{end_str}")

    # ── Intraday: try Alpaca first ────────────────────────────────────────────
    if resolution in _ALPACA_INTRADAY_RESOLUTIONS:
        df = _fetch_range_alpaca(symbol, start_str, end_str, resolution)
        if df is not None and not df.empty:
            return df
        log.warning(f"  {symbol}: Alpaca returned no data, falling back to yfinance")

    # ── Daily/weekly/monthly (or Alpaca fallback): yfinance ──────────────────
    interval = _to_yf_interval(resolution)

    # yfinance end is exclusive — add 1 day to make it inclusive
    end_dt_yf = datetime.strptime(end_str, "%Y-%m-%d") + timedelta(days=1)
    end_str_yf = end_dt_yf.strftime("%Y-%m-%d")

    ticker = yf.Ticker(symbol)
    raw    = ticker.history(
        start=start_str, end=end_str_yf, interval=interval, auto_adjust=True
    )

    if raw.empty:
        log.warning(f"  {symbol}: yfinance returned no data for {start_str}→{end_str}")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    return _clean(raw)


def fetch_bars(
    symbol: str,
    start: Optional[Union[str, "date", "datetime"]] = None,
    end: Optional[Union[str, "date", "datetime"]] = None,
    timeframe: str = "1Day",
    use_cache: bool = True,
    resolution: str = "15",
    n_bars: int = 60,
) -> pd.DataFrame:
    """
    Two usage patterns:

    Pattern 1 — date range (backtest / dashboard):
        fetch_bars("SPY", start="2024-01-01", end="2024-12-01", timeframe="1Day")

    Pattern 2 — last N bars (live scanner / screener):
        fetch_bars("SPY", resolution="15", n_bars=60)

    Pattern 1 delegates to fetch_bars_range (Alpaca for intraday).
    Pattern 2 uses yfinance (fast, no auth, fine for 60-bar live window).

    Beginner note:
      Pattern 2 calculates how far back N bars is in calendar time, fetches
      a generous 3× buffer to account for weekends and holidays, then trims
      to the last N rows.
    """
    if start is not None and end is not None:
        interval = _TIMEFRAME_TO_YF.get(timeframe, _to_yf_interval(resolution))
        res_key  = {v: k for k, v in _RESOLUTION_TO_YF.items()}.get(interval, resolution)
        return fetch_bars_range(symbol, start, end, resolution=res_key)

    # Pattern 2: last n_bars to now (yfinance, fast)
    interval    = _to_yf_interval(resolution)
    res_minutes = _RESOLUTION_MINUTES.get(resolution, 15)

    trading_minutes_per_day = 390
    calendar_days_needed = max(
        int((n_bars * res_minutes / trading_minutes_per_day) * 3),
        7,
    )
    max_days = {"1m": 6, "5m": 59, "15m": 59, "30m": 59, "60m": 729}.get(interval, 3650)
    calendar_days_needed = min(calendar_days_needed, max_days)

    end_dt    = datetime.now()
    start_dt  = end_dt - timedelta(days=calendar_days_needed)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    log.debug(f"  {symbol}: downloading last {n_bars} {interval} bars (lookback={calendar_days_needed}d)")

    ticker = yf.Ticker(symbol)
    raw    = ticker.history(start=start_str, end=end_str, interval=interval, auto_adjust=True)

    if raw.empty:
        log.warning(f"  {symbol}: yfinance returned no data")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = _clean(raw)
    if len(df) > n_bars:
        df = df.tail(n_bars)

    log.debug(f"  {symbol}: got {len(df)} bars")
    return df


def fetch_bars_bulk(
    symbols: List[str],
    resolution: str = "15",
    n_bars: int = 60,
    delay: float = _BULK_DELAY,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch bars for multiple symbols (last N bars each).
    Symbols that fail or return no data are omitted from the result.
    """
    results: Dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols):
        try:
            df = fetch_bars(symbol, resolution=resolution, n_bars=n_bars)
            if not df.empty:
                results[symbol] = df
        except Exception as e:
            log.warning(f"  {symbol}: fetch failed — {e}")
        if i < len(symbols) - 1:
            time.sleep(delay)
    log.info(f"Bulk fetch: {len(results)}/{len(symbols)} symbols returned data")
    return results


def fetch_latest_bar(symbol: str, resolution: str = "15") -> Optional[pd.Series]:
    """
    Fetch just the most recent completed bar for a symbol.
    Returns None if no data is available.
    """
    try:
        df = fetch_bars(symbol, resolution=resolution, n_bars=3)
        if df.empty:
            return None
        return df.iloc[-1]
    except Exception as e:
        log.warning(f"  {symbol}: latest bar fetch failed — {e}")
        return None
