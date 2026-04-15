# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Alpaca Bot is a Python trading system that runs live equity and crypto scanners against the Alpaca brokerage API, with a FastAPI web dashboard and a Strategy IDE for backtesting. It trades real money (or paper) via Alpaca.

## Commands

```bash
# Run everything (equity monitor + equity scanner + crypto scanner + web dashboard)
python run_all.py

# Run selectively
python run_all.py --no-crypto      # equity + web only
python run_all.py --no-equity      # crypto + web (weekend mode)
python run_all.py --no-web         # scanners only
python run_all.py --no-monitor     # skip standalone equity monitor

# Run individual services
python scanner/run_equity_monitor.py   # equity monitor (always-on, 60s polls)
python scanner/run_scanner.py          # equity scanner (15-min, NYSE hours)
python scanner/run_crypto_scanner.py   # crypto scanner (1h, 24/7)
python webapp/server.py                # web dashboard on :8000

# macOS auto-start (launchd)
./macos_setup.sh                       # install + start as LaunchAgent
./macos_setup.sh status                # check if running
./macos_setup.sh uninstall             # stop + remove

# Backtest via CLI
cd strategy_ide
python main.py --mode backtest --strategy mean_reversion --symbol SPY --start 2023-01-01
python main.py --mode backtest --strategy mean_reversion --universe large_cap --resolution 15

# Run tests
python -m pytest strategy_ide/tests/ -v
```

## Architecture

**Orchestrator** — `run_all.py` manages four services as subprocesses with auto-restart and exponential backoff. Logs to `logs/`. Legacy logs mirrored to `/tmp/bot_logs/` for the dashboard. On macOS, `macos_setup.sh` installs a LaunchAgent that keeps `run_all.py` alive across reboots/crashes.

**Scanner layer** (`scanner/`):
- `live_scanner.py` — 15-min polling loop for equities. Fetches bars via Alpaca/yfinance, evaluates strategy signals, submits orders via Alpaca Trading API. Background thread re-runs screener periodically.
- `crypto_scanner.py` — Same pattern for crypto pairs (24/7, 1h bars).
- `screener.py` / `crypto_screener.py` — Mean-reversion screeners that rank candidates by BB/RSI oversold signals.
- `run_scanner.py` / `run_crypto_scanner.py` — Entry points that wire strategy params + watchlist + equity monitor together.

**Strategy IDE** (`strategy_ide/`):
- `strategies/base_strategy.py` — ABC all strategies inherit. Two methods: `populate_indicators(df)` and `generate_signals(df)` (adds `signal` column: 1=enter, -1=exit, 0=hold). Optionally sets `stop_price`/`take_profit_price` columns.
- Strategy implementations: `mean_reversion`, `hybrid_trend_mr` (200-SMA regime filter + BB/RSI), `trend_following`, and four `crypto_*` variants.
- `backtester/engine.py` — Vectorized backtester. Risk-based position sizing via `risk/sizing.py`. Auto-detects bar frequency for Sharpe annualization.
- `data/fetcher.py` — Dual source: Alpaca Data API for multi-year intraday history, yfinance for live scanner's short lookback. `crypto_fetcher.py` for crypto bars.
- `config.py` — Global settings, universe definitions (`BACKTEST_UNIVERSES`), risk defaults. Loads `.env` for API keys.
- `optimization/hyperopt_runner.py` — Hyperparameter optimization with train/test split.
- `monitor/equity_monitor.py` — Background thread polling account equity, fires alerts on daily loss or drawdown breaches. Writes CSV logs to `equity_logs/`.

**Web dashboard** (`webapp/`):
- `server.py` — FastAPI app on port 8000. Endpoints: `/api/account`, `/api/positions`, `/api/orders`, `/api/equity-log`, `/api/screener`, `/api/backtest` (POST), `/api/hyperopt` (POST), `/api/crypto/*`. Serves static files from `webapp/static/`.
- Frontend is vanilla JS in `webapp/static/js/app.js`.

## Key Patterns

- **Environment**: Python 3.13, venv at `.venv/`. API keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`, `FINNHUB_API_KEY`) loaded from `.env` files via `python-dotenv`.
- **Data flow**: Raw OHLCV DataFrame → `strategy.populate_indicators()` → `strategy.generate_signals()` (adds `signal` column) → backtester or live scanner acts on signals.
- **Resolution strings** (Finnhub legacy used throughout): `"1"`, `"5"`, `"15"`, `"30"`, `"60"` for minutes, `"D"` for daily.
- **sys.path manipulation**: Many entry points add both repo root and `strategy_ide/` to `sys.path` so that imports like `from strategies.mean_reversion import ...` and `from strategy_ide.strategies.mean_reversion import ...` both work.
- **Equity scanner** uses `HybridTrendMRStrategy` with params from cross-ticker hyperopt analysis. Priority tickers: META, JPM, MSFT.
- **Crypto scanner** uses `CryptoTrendFollowingStrategy` focused on AVAX/USD with 500-trial hyperopt-validated params.
