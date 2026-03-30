"""
data/crypto_fetcher.py
━━━━━━━━━━━━━━━━━━━━━━
Crypto data via Alpaca CryptoHistoricalDataClient.

Uses the same API keys as the equity scanner — no new credentials required.
Alpaca supports: BTC/USD, ETH/USD, SOL/USD, LINK/USD, AAVE/USD, AVAX/USD,
                 DOGE/USD, LTC/USD, UNI/USD, DOT/USD, MATIC/USD, XRP/USD

Key differences from fetcher.py (StockBarsRequest):
  - CryptoBarsRequest has NO 'feed' parameter (no SIP/IEX distinction for crypto)
  - CryptoBarsRequest has NO 'adjustment' parameter (no corporate actions)
  - Symbol format is "BTC/USD" (slash format), not "BTCUSD"
  - Crypto trades 24/7 → trading_minutes_per_day = 1440, not 390
  - Default resolution "60" (1-hour bars) — better signal/noise than 15-min

Public API mirrors fetcher.py exactly so the backtester needs zero changes:
  fetch_crypto_bars_range(symbol, start, end, resolution="60") -> pd.DataFrame
  fetch_crypto_bars(symbol, resolution="60", n_bars=100)       -> pd.DataFrame
  fetch_crypto_bars_bulk(symbols, resolution, n_bars)          -> Dict[str, pd.DataFrame]
  fetch_crypto_latest_bar(symbol, resolution)                  -> Optional[pd.Series]
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd
from dotenv import load_dotenv

# Load .env (same keys used by equity scanner)
_ROOT = Path(__file__).resolve().parent.parent  # strategy_ide/
load_dotenv(_ROOT / ".env")
load_dotenv(_ROOT.parent / ".env", override=False)

# Reuse _clean() from fetcher — identical normalization logic
from data.fetcher import _clean

log = logging.getLogger(__name__)

_BULK_DELAY = 0.3  # seconds between symbols in bulk fetch

# Crypto-supported resolutions → Alpaca TimeFrame
_CRYPTO_RESOLUTIONS = {"1", "5", "15", "30", "60"}

# Minutes per resolution (for lookback calculation)
_RESOLUTION_MINUTES = {
    "1": 1, "5": 5, "15": 15, "30": 30, "60": 60,
}

# ── Alpaca crypto client (lazy init) ─────────────────────────────────────────

_crypto_client = None


def _get_crypto_client():
    """Return (and cache) a CryptoHistoricalDataClient using env API keys."""
    global _crypto_client
    if _crypto_client is not None:
        return _crypto_client
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        api_key    = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            log.warning("Alpaca API keys not found — crypto fetch unavailable")
            return None
        _crypto_client = CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        return _crypto_client
    except ImportError:
        log.warning("alpaca-py not installed — crypto fetch unavailable")
        return None


def _fetch_range_alpaca_crypto(
    symbol: str,
    start: str,   # "YYYY-MM-DD"
    end: str,     # "YYYY-MM-DD" (inclusive)
    resolution: str,
) -> Optional[pd.DataFrame]:
    """
    Fetch crypto bars from Alpaca CryptoHistoricalDataClient.
    Returns None if unavailable or no data returned.

    Note: CryptoBarsRequest does NOT accept 'feed' or 'adjustment' params.
    """
    client = _get_crypto_client()
    if client is None:
        return None

    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        _TF_MAP = {
            "1":  TimeFrame(1,  TimeFrameUnit.Minute),
            "5":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15": TimeFrame(15, TimeFrameUnit.Minute),
            "30": TimeFrame(30, TimeFrameUnit.Minute),
            "60": TimeFrame(1,  TimeFrameUnit.Hour),
        }
        tf = _TF_MAP.get(resolution)
        if tf is None:
            log.warning(f"  {symbol}: unsupported crypto resolution '{resolution}'")
            return None

        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt   = datetime.strptime(end,   "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )

        req  = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start_dt,
            end=end_dt,
        )
        bars = client.get_crypto_bars(req)
        raw  = bars.df

        if raw.empty:
            return None

        # Drop the outer "symbol" level if MultiIndex
        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.droplevel(0)

        return _clean(raw)

    except Exception as e:
        log.warning(f"  {symbol}: crypto Alpaca fetch failed — {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_crypto_bars_range(
    symbol: str,
    start: Union[str, datetime],
    end: Union[str, datetime],
    resolution: str = "60",
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars for a crypto pair over a date range.

    Parameters
    ----------
    symbol     : Pair in slash format, e.g. "BTC/USD"
    start, end : "YYYY-MM-DD" string or datetime
    resolution : "1", "5", "15", "30", "60" (minutes)
    use_cache  : If True (default), cache results to disk — dramatically speeds
                 up repeated research runs.  Set False to force a fresh fetch.

    Returns
    -------
    pd.DataFrame  columns: open, high, low, close, volume
                  index: timezone-naive DatetimeIndex (sorted ascending)
    """
    def _to_str(d):
        if isinstance(d, datetime):
            return d.strftime("%Y-%m-%d")
        return str(d)

    start_str = _to_str(start)
    end_str   = _to_str(end)

    if resolution not in _CRYPTO_RESOLUTIONS:
        log.warning(f"  {symbol}: resolution '{resolution}' not supported for crypto, using '60'")
        resolution = "60"

    # ── Disk cache ────────────────────────────────────────────────────────────
    # Cache key: <symbol>_<start>_<end>_<resolution>.parquet
    # Stored in strategy_ide/data/cache/ alongside the fetcher.
    # Avoids the 5-15 minute Alpaca API fetches on every research run.
    if use_cache:
        _sym_safe = symbol.replace("/", "-")
        _cache_dir = Path(__file__).parent / "cache"
        _cache_dir.mkdir(exist_ok=True)
        _cache_file = _cache_dir / f"{_sym_safe}_{start_str}_{end_str}_{resolution}.parquet"
        if _cache_file.exists():
            log.debug(f"  {symbol}: loading from cache {_cache_file.name}")
            try:
                return pd.read_parquet(_cache_file)
            except Exception as e:
                log.warning(f"  {symbol}: cache read failed ({e}), re-fetching")
                _cache_file.unlink(missing_ok=True)

    df = _fetch_range_alpaca_crypto(symbol, start_str, end_str, resolution)
    if df is not None and not df.empty:
        if use_cache:
            try:
                df.to_parquet(_cache_file)
                log.debug(f"  {symbol}: cached to {_cache_file.name}")
            except Exception as e:
                log.warning(f"  {symbol}: cache write failed — {e}")
        return df

    log.warning(f"  {symbol}: crypto fetch returned no data for {start_str}→{end_str}")
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def fetch_crypto_bars(
    symbol: str,
    resolution: str = "60",
    n_bars: int = 100,
) -> pd.DataFrame:
    """
    Fetch the last N bars for a crypto pair (live scanner / screener pattern).

    Crypto trades 24/7 so the lookback uses 1440 minutes/day (not 390).
    Returns last n_bars rows.
    """
    from datetime import timedelta

    res_minutes = _RESOLUTION_MINUTES.get(resolution, 60)
    # 3× buffer for safety (no weekends/holidays for crypto, but still generous)
    calendar_days_needed = max(int((n_bars * res_minutes / 1440) * 3), 2)

    end_dt    = datetime.now(tz=timezone.utc)
    start_dt  = end_dt - timedelta(days=calendar_days_needed)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    log.debug(f"  {symbol}: fetch last {n_bars} crypto {resolution}m bars (lookback={calendar_days_needed}d)")

    # use_cache=False: this function is called by the live scanner every hour.
    # The cache key is date-based (start_str/end_str) and doesn't change within
    # a day, so caching here would return stale data for every intra-day poll.
    df = fetch_crypto_bars_range(symbol, start_str, end_str, resolution, use_cache=False)
    if df.empty:
        return df

    if len(df) > n_bars:
        df = df.tail(n_bars)
    return df


def fetch_crypto_bars_bulk(
    symbols: List[str],
    resolution: str = "60",
    n_bars: int = 100,
    delay: float = _BULK_DELAY,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch bars for multiple crypto pairs (last N bars each).
    Symbols with no data are omitted from the result.
    """
    results: Dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols):
        try:
            df = fetch_crypto_bars(symbol, resolution=resolution, n_bars=n_bars)
            if not df.empty:
                results[symbol] = df
        except Exception as e:
            log.warning(f"  {symbol}: crypto bulk fetch failed — {e}")
        if i < len(symbols) - 1:
            time.sleep(delay)
    log.info(f"Crypto bulk fetch: {len(results)}/{len(symbols)} symbols returned data")
    return results


def fetch_crypto_latest_bar(
    symbol: str,
    resolution: str = "60",
) -> Optional[pd.Series]:
    """Fetch the most recent completed bar for a crypto pair."""
    try:
        df = fetch_crypto_bars(symbol, resolution=resolution, n_bars=3)
        if df.empty:
            return None
        return df.iloc[-1]
    except Exception as e:
        log.warning(f"  {symbol}: latest crypto bar fetch failed — {e}")
        return None
