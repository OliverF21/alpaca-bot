"""
Global settings for the Strategy IDE.
All config is loaded from environment where applicable; defaults here.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

# Load .env from project root (strategy_ide/)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path, override=True)

# ---------------------------------------------------------------------------
# Alpaca (from .env — orders & positions only; no data)
# ---------------------------------------------------------------------------
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER: bool = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Finnhub (from .env — market data / bars)
# ---------------------------------------------------------------------------
FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

def require_alpaca_credentials() -> None:
    """Raise if API keys are missing. Call before live/paper trading."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise ValueError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env. "
            "Copy .env.example to .env and fill in your keys."
        )


# ---------------------------------------------------------------------------
# Universe & timeframes
# ---------------------------------------------------------------------------
DEFAULT_UNIVERSE: list[str] = ["SPY", "QQQ", "IWM"]
DEFAULT_SYMBOL: str = "SPY"
DEFAULT_TIMEFRAME: str = "5min"  # Finnhub: "1", "5", "15", "30", "60", "D"

# Screener watchlists — same universe as live scanner; used for multi-symbol backtest
BACKTEST_UNIVERSES: dict[str, list[str]] = {
    "large_cap": [
        "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
        "META", "TSLA", "JPM", "V", "UNH", "XOM", "HD", "BAC",
    ],
    "sector_etfs": [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE",
        "QQQ", "SPY", "IWM", "DIA", "GLD", "SLV", "USO", "TLT", "HYG", "LQD",
    ],
    "sp100": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK.B",
        "JPM", "V", "UNH", "XOM", "MA", "LLY", "JNJ", "PG", "HD", "MRK",
        "AVGO", "CVX", "ABBV", "COST", "PEP", "KO", "ADBE", "WMT", "BAC",
        "CRM", "TMO", "NFLX", "ACN", "MCD", "CSCO", "ABT", "LIN", "DHR",
        "TXN", "NEE", "PM", "ORCL", "QCOM", "AMD", "HON", "UPS", "MS",
    ],
}


# ---------------------------------------------------------------------------
# Backtest results (every run saved for analysis / retest)
# ---------------------------------------------------------------------------
BACKTEST_RESULTS_DIR: Path = Path(__file__).resolve().parent / "backtest_results"
BACKTEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Risk (defaults; strategies can override)
# ---------------------------------------------------------------------------
DEFAULT_RISK_PCT_PER_TRADE: float = 0.01  # 1% of equity per trade
DEFAULT_MAX_POSITION_PCT: float = 0.25    # max 25% of equity in one position
DEFAULT_ATR_STOP_MULTIPLIER: float = 2.0  # ATR-based stop: 2x ATR
DEFAULT_KELLY_FRACTION: float = 0.25      # fractional Kelly (0.25 = quarter Kelly)
