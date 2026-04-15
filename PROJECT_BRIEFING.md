# Alpaca Bot — Project Briefing (2026-04-15)

## Overview

**Alpaca Bot** is a live algorithmic trading system running on the Alpaca brokerage API. It trades real/paper money via two separate scanners (equity + crypto), powered by a FastAPI web dashboard and a backtesting/hyperparameter optimization engine.

Currently deployed on:
- **macOS** (dev machine) — via LaunchAgent (`macos_setup.sh`), runs 24/7
- **Raspberry Pi** (long-term horizon) — specialized setup + dependencies (`pi_setup.sh`)
- **Docker** — compose config ready for production/cloud

---

## Architecture

### 1. **Orchestrator** (`run_all.py`)
Central process manager spawning four long-lived services:
- **Equity Monitor** — 60-second polling of account equity; fires loss/drawdown alerts
- **Equity Scanner** — 15-min poll loop (NYSE hours only)
- **Crypto Scanner** — 1-hour poll loop (24/7)
- **Web Dashboard** — FastAPI server on `:8000`

**Features:**
- Auto-restart with exponential backoff on crash
- Logs to `logs/<service>_YYYYMMDD.log` (daily rotation)
- Legacy log mirroring to `/tmp/bot_logs/` for dashboard consumption
- CLI flags: `--no-crypto`, `--no-equity`, `--no-web`, `--no-monitor`

**Critical:** Code changes to scanners do **NOT** take effect until restart. Zombie processes can accumulate.

---

## Scanner Layer

### **Equity Scanner** (`scanner/run_scanner.py` + `scanner/live_scanner.py`)
- **Strategy:** `HybridTrendMRStrategy` (200-SMA regime filter + Bollinger Band/RSI oversold signals)
- **Tickers:** META, JPM, MSFT (configurable via `run_scanner.py`)
- **Entry Condition:** oversold (RSI < 30, price below lower BB) + confirming regime
- **Order Sizing:** Risk-based (% of equity per signal)
- **Data:** Alpaca Data API (intraday bars) + yfinance fallback (live lookback)

**Run solo:** `python scanner/run_scanner.py`

---

### **Crypto Scanner** (`scanner/run_crypto_scanner.py` + `scanner/crypto_scanner.py`)
Multi-strategy system with ML enhancements (added 2026-04-08 to 2026-04-15).

#### **Core Strategies** (4 conviction-scored variants)
1. **CryptoTrendFollowingStrategy** (priority 4 in arbitration)
   - EMA crossover + ADX trend strength
   - Conviction: based on ADX value
2. **CryptoBreakoutStrategy** (priority 3)
   - 20-bar high breakout w/ ATR volatility filter
3. **CryptoSupertrendStrategy** (priority 2)
   - Supertrend indicator with Bollinger Band confirmation
4. **CryptoMeanReversionStrategy** (priority 1)
   - RSI oversold + BB mean-reversion setup

#### **Signal Arbitrator** (`scanner/signal_arbitrator.py`)
Resolves conflicts when strategies disagree on the same pair:
- **If multiple enter signals** → ranked by conviction, execute top K (position limit)
- **If enters vs exits conflict** → hold unless exit signals dominate
- **Cooldown:** 3-bar lockout after exit to prevent churn
- **Position limit:** `min(floor(equity / 5000), 6)` positions max
- **Risk tiering:** 1% standard entry, 2% for high-conviction

#### **Universe Selection** (`scanner/crypto_universe.py`)
Dynamically ranks top-8 crypto pairs every 30 minutes by:
- **Trailing ATR%** (volatility score)
- **Dollar volume** (minimum $50k daily)
- **Fallback pairs:** BTC, ETH, SOL, AVAX, LINK, DOGE

#### **New ML Enhancements** (Last Week)

**Regime Detector** (`scanner/regime_detector.py`)
- GMM (Gaussian Mixture Model) classifies market into: **TRENDING** / **MEAN_REVERTING** / **CHOPPY**
- Input features: rolling volatility, momentum, volume ratio
- Online re-fit every N bars on rolling window
- **Gating:** Only allows trend-following strategies in TRENDING regime, mean-reversion in MEAN_REVERTING, sits idle in CHOPPY
- Based on Ch13 of *ML for Trading* (López de Prado)

**Volatility Filter** (`scanner/vol_filter.py`)
- GARCH(1,1) model forecasts next-period volatility
- Outputs:
  - `stop_mult` — scale ATR stops by volatility regime (lower in extreme vol)
  - `vol_regime` — "low", "normal", "high", "extreme"
  - `allow_entry` — kill switch if volatility extreme
- Falls back to rolling std if `arch` library unavailable (Pi compatibility)
- Based on Ch09 of *ML for Trading*

---

## Strategy IDE (`strategy_ide/`)

**Backtesting + Optimization Framework**

### Available Strategies
**Equity:**
- `mean_reversion` — BB/RSI oversold
- `hybrid_trend_mr` — SMA regime + mean-reversion
- `trend_following` — EMA crossover
- `vwap_reversion` — VWAP mean reversion

**Crypto:**
- `crypto_trend_following`, `crypto_breakout`, `crypto_supertrend`, `crypto_mean_reversion`

### Base Strategy ABC (`strategy_ide/strategies/base_strategy.py`)
All strategies implement:
```python
def populate_indicators(df: DataFrame) -> DataFrame:
    # Add TA columns (RSI, MACD, SMA, ATR, etc.)
    
def generate_signals(df: DataFrame) -> DataFrame:
    # Add 'signal' column (1=enter, -1=exit, 0=hold)
    # Optionally set 'stop_price', 'take_profit_price'
```

### Backtester (`strategy_ide/backtester/engine.py`)
- **Vectorized execution** on historical OHLCV bars
- **Risk-based position sizing** via `strategy_ide/risk/sizing.py`
- **Auto-detection** of bar frequency for Sharpe annualization
- **Output:** equity curve, Sharpe ratio, max drawdown, win rate, PnL

### Hyperparameter Optimization (`strategy_ide/optimization/hyperopt_runner.py`)
- **Bayesian optimization** with train/test split
- **Finding:** Optimal params but **no auto-apply loop**
  - Results returned to browser UI but must be manually copied to `run_crypto_scanner.py`
  - Future: JSON config file + scanner reload mechanism

### CLI Usage
```bash
cd strategy_ide
python main.py --mode backtest --strategy mean_reversion --symbol SPY --start 2023-01-01
python main.py --mode backtest --strategy mean_reversion --universe large_cap --resolution 15
python -m pytest tests/ -v
```

### Data Layer (`strategy_ide/data/`)
- **fetcher.py** — multi-year Alpaca Data API history + yfinance fallback
- **crypto_fetcher.py** — crypto bars via Alpaca
- **Config** — `strategy_ide/config.py` loads `.env` keys, defines `BACKTEST_UNIVERSES`

---

## Web Dashboard (`webapp/`)

**Tech:** FastAPI + vanilla JS on port `:8000`

### API Endpoints (key subset)
```
GET  /health                    → Liveness (Docker health check)
GET  /api/account              → Account balance, buying power, margin
GET  /api/positions            → Current open positions
GET  /api/orders               → Order history
GET  /api/equity-log           → Daily account equity CSV
GET  /api/screener             → Last equity screener results
POST /api/backtest             → Run strategy backtest
POST /api/hyperopt             → Run hyperparameter optimization
GET  /api/crypto/*             → Crypto-specific endpoints (universe, arbitrator state)
```

### Frontend (`webapp/static/`)
- **index.html** — responsive dashboard layout
- **app.js** — fetch endpoints, live grid updates, equity chart (Plotly)
- **style.css** — dark theme

### Dashboard Capabilities
- View account equity, positions, open/filled orders
- Run backtests on demand (returns equity curve + metrics)
- Hyperopt crypto strategies (returns best_params for manual copy)
- Monitor arbitrator state (which pairs live now, conviction scores)
- Live equity log chart with drawdown highlights

---

## Deployment

### **macOS (Current Primary)**
```bash
./macos_setup.sh              # Install LaunchAgent, start services
./macos_setup.sh status       # Check running services
./macos_setup.sh uninstall    # Stop + remove
```
- LaunchAgent file: `~/Library/LaunchAgents/com.alpaca.bot.plist`
- Survives reboots/crashes
- Logs: `~/Library/Logs/alpaca_bot.log` + `logs/` directory

### **Raspberry Pi**
```bash
./pi_setup.sh                 # Compile/install dependencies, enable numba JIT disable
```
- Specialized: avoids llvmlite (ARM v7 incompatibility)
- Uses `requirements-pi.txt` (pandas-ta with `--no-deps`)
- Hardened Python import via `sitecustomize.py`

### **Docker**
```bash
docker-compose up             # Spin up bot in container
```
- `docker-compose.yml` — mounts `.env`, maps `:8000`, auto-restarts
- `Dockerfile` — Python 3.13 slim base, installs deps
- Health check: `/health` endpoint every 10s

---

## Environment & Configuration

### **.env (Required)**
```ini
ALPACA_API_KEY=<key>
ALPACA_SECRET_KEY=<secret>
ALPACA_PAPER=true             # or false for live
FINNHUB_API_KEY=<optional>    # used in older screeners (legacy)
```

### **Key Patterns**
- **Resolution strings** (Finnhub legacy): `"1"`, `"5"`, `"15"`, `"30"`, `"60"` (minutes), `"D"` (daily)
- **sys.path manipulation:** Many entry points add repo root + `strategy_ide/` so imports work both ways:
  - `from strategies.mean_reversion import ...`
  - `from strategy_ide.strategies.mean_reversion import ...`
- **Python 3.13** required, virtual env at `.venv/`
- **Logging:** DEBUG/INFO to files, console output suppressed in background services

### **Data Flow**
```
Raw OHLCV DataFrame
    ↓
strategy.populate_indicators()      [adds TA columns: RSI, MACD, SMA, ATR, etc.]
    ↓
strategy.generate_signals()         [adds 'signal' column: 1=enter, -1=exit, 0=hold]
    ↓
live_scanner / backtester
    ↓
Alpaca Trading API (orders) / CSV logs
```

---

## Known Gaps & Future Work

### **Hyperopt Gap**
- Hyperopt finds optimal params but no feedback loop to scanner
- **Workaround:** manually copy params from UI to `scanner/run_crypto_scanner.py`
- **Future:** JSON config file + scanner reload on startup/signal

### **Process Management**
- Code changes to scanners require manual restart
- Zombie processes can accumulate over weeks
- **Mitigation:** periodic `pkill -f "run_*_scanner.py"` or orchestrator restart

### **Equity Monitor Alerts**
- Fires on daily loss % or drawdown breach
- Currently logs to file only (no email/Slack integration yet)

---

## Recent Work Summary (Last Week)

| Date | Feature | Files |
|------|---------|-------|
| 2026-04-08 | ML regime detection | `regime_detector.py` (+220 LOC) |
| 2026-04-08 | GARCH volatility filter | `vol_filter.py` (+208 LOC) |
| 2026-04-08 to -15 | Crypto scanner fixes, route fixes | `crypto_scanner.py`, `webapp/server.py` |
| 2026-04-14 | Settings tweaks | `.claude/settings.local.json` |
| 2026-04-15 | Last update | Regime + vol filter integration |

**Total scanner code:** ~3k LOC
- `crypto_scanner.py`: 609 LOC (orchestration, multi-pair, multi-strategy)
- `live_scanner.py`: 636 LOC (equity scanning)
- `signal_arbitrator.py`: 124 LOC (conflict resolution, position limits)
- `crypto_universe.py`: 138 LOC (dynamic pair ranking)
- `regime_detector.py`: 220 LOC (GMM regime classification)
- `vol_filter.py`: 208 LOC (GARCH volatility forecasting)

---

## Quick Start (for Claude Pro)

### Run Everything
```bash
cd /Users/oliver/VSCode\ Repos/alpaca-bot
python run_all.py
# → monitor + equity scanner + crypto scanner + web @ :8000
```

### Run Crypto Scanner Only (for testing)
```bash
python scanner/run_crypto_scanner.py
```

### Run Backtest
```bash
cd strategy_ide
python main.py --mode backtest --strategy crypto_trend_following --symbol BTC/USD --start 2025-01-01
```

### Watch Logs
```bash
tail -f logs/crypto_scanner_*.log
```

### Dashboard
```
http://localhost:8000
```

### Restart Services (if code changed)
```bash
pkill -f run_all.py
python run_all.py     # auto-restart
```

---

## Key Files to Know

| File | Purpose |
|------|---------|
| `run_all.py` | Orchestrator; all services start here |
| `scanner/crypto_scanner.py` | Main crypto scanner loop (1h, 4 strategies) |
| `scanner/regime_detector.py` | GMM-based market regime (new) |
| `scanner/vol_filter.py` | GARCH volatility filter (new) |
| `scanner/signal_arbitrator.py` | Multi-strategy conflict resolution |
| `scanner/crypto_universe.py` | Dynamic pair ranking |
| `scanner/live_scanner.py` | Equity scanner (HybridTrendMR) |
| `scanner/run_equity_monitor.py` | Account equity watcher |
| `webapp/server.py` | FastAPI backend (all routes) |
| `strategy_ide/strategies/` | 8 strategy implementations |
| `strategy_ide/backtester/engine.py` | Vectorized backtester |
| `strategy_ide/optimization/hyperopt_runner.py` | Bayesian hyperopt |
| `.env` | API keys (not in git) |
| `CLAUDE.md` | This project's documentation |

---

## Status

- ✅ Multi-strategy crypto scanner live (4 strategies + arbitration)
- ✅ Equity scanner operational (META, JPM, MSFT)
- ✅ Web dashboard fully functional
- ✅ ML regime detection deployed
- ✅ GARCH volatility filtering active
- ✅ Backtester + hyperopt working
- ⚠️ Hyperopt results require manual param copy (no auto-apply)
- ⚠️ Zombie processes can accumulate (monitor `ps aux`)

---

Generated: 2026-04-15 | Questions: Refer to CLAUDE.md or memory (user_oliver.md, project_*.md, feedback_*.md)
