# Strategy IDE

Local strategy development framework for algorithmic trading with Alpaca.

## Setup

```bash
cd strategy_ide
pip install -r requirements.txt
```

Copy your Alpaca credentials into this folder:

```bash
cp ../.env .env   # or: cp .env.example .env and edit
```

## CLI

```bash
# Backtest
python main.py --mode backtest --strategy mean_reversion --symbol SPY --start 2023-01-01 --end 2023-06-30

# Paper trading (account info)
python main.py --mode paper --strategy mean_reversion

# Live (prompts for confirmation)
python main.py --mode live --strategy mean_reversion
```

## Dashboard

```bash
streamlit run dashboard/app.py
```

Pages: **Backtest** (run backtest, equity curve, trades), **Live signals** (current indicators + signal per symbol), **Positions** (open Alpaca positions).

## Backtest results (saved automatically)

Every backtest (CLI or dashboard) is saved under **`backtest_results/<run_id>/`** for analysis and retesting:

- `meta.json` — strategy, symbol, date range, initial capital, stats, timestamp
- `trades.csv` — trade log (entry/exit dates, prices, PnL, return %)
- `equity_curve.csv` — equity over time

Example run id: `20250315_143022_mean_reversion_SPY`. Use the dashboard **Past results** page to load and inspect any run, or open the folder in your editor and share with Claude for analysis.

## Project layout

- `config.py` — settings, universe, risk defaults
- `data/fetcher.py` — Alpaca OHLCV fetch + cache
- `indicators/base.py` — BB, RSI, MACD, ATR
- `strategies/` — base class, mean reversion, template
- `backtester/engine.py` — vectorized backtester
- `backtester/results.py` — save/load backtest runs
- `risk/sizing.py` — position sizing
- `execution/alpaca_broker.py` — orders, positions, equity
- `dashboard/app.py` — Streamlit UI (Backtest, Past results, Live signals, Positions)
