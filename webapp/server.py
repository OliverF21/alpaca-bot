"""
webapp/server.py
━━━━━━━━━━━━━━━━
FastAPI backend for the Alpaca Bot trading dashboard.

Run from repo root:
    python webapp/server.py
"""

import os
import sys
import asyncio
from pathlib import Path
from datetime import date, timedelta
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
_IDE  = _REPO / "strategy_ide"
for p in [str(_REPO), str(_IDE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(_REPO / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import uvicorn

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

_API_KEY    = os.getenv("ALPACA_API_KEY", "")
_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

_trader = TradingClient(_API_KEY, _SECRET_KEY, paper=_PAPER)

app = FastAPI(title="Alpaca Bot API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ── Health Check ──────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    """Liveness probe for Docker health checks."""
    return {"status": "ok"}


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse(_STATIC / "index.html")


# ── Account ───────────────────────────────────────────────────────────────────

@app.get("/api/account")
def get_account():
    try:
        a = _trader.get_account()
        equity      = float(a.equity)
        last_equity = float(a.last_equity)
        daily_pl    = equity - last_equity
        return {
            "equity":        equity,
            "last_equity":   last_equity,
            "cash":          float(a.cash),
            "buying_power":  float(a.buying_power),
            "long_mkt":      float(a.long_market_value),
            "daily_pl":      daily_pl,
            "daily_pl_pct":  daily_pl / last_equity * 100 if last_equity else 0,
            "paper":         _PAPER,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Positions ─────────────────────────────────────────────────────────────────

@app.get("/api/positions")
def get_positions():
    try:
        positions = _trader.get_all_positions()
        return [
            {
                "symbol":       p.symbol,
                "qty":          float(p.qty),
                "entry":        float(p.avg_entry_price),
                "current":      float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl":    float(p.unrealized_pl),
                "unrealized_plpc":  float(p.unrealized_plpc) * 100,
                "change_today":     float(p.change_today) * 100,
            }
            for p in positions
        ]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Orders ────────────────────────────────────────────────────────────────────

@app.get("/api/orders")
def get_orders(limit: int = 40):
    try:
        req    = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
        orders = _trader.get_orders(req)
        result = []
        for o in orders:
            result.append({
                "id":         str(o.id),
                "symbol":     o.symbol,
                "side":       o.side.value,
                "qty":        float(o.qty) if o.qty else 0,
                "fill_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "status":     o.status.value,
                "type":       o.type.value,
                "filled_at":  o.filled_at.isoformat() if o.filled_at else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            })
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Equity log ────────────────────────────────────────────────────────────────

@app.get("/api/equity-log")
def get_equity_log(days: int = 30):
    log_dir = _REPO / "equity_logs"
    if not log_dir.exists():
        return {"points": [], "summary": {}}
    cutoff = date.today() - timedelta(days=days)
    frames = []
    for f in sorted(log_dir.glob("equity_log_*.csv")):
        try:
            date_str = f.stem.replace("equity_log_", "")
            if date.fromisoformat(date_str) >= cutoff:
                frames.append(pd.read_csv(f, parse_dates=["timestamp"]))
        except Exception:
            pass
    if not frames:
        return {"points": [], "summary": {}}
    df = pd.concat(frames).sort_values("timestamp").drop_duplicates("timestamp")
    first = float(df["equity"].iloc[0])
    last  = float(df["equity"].iloc[-1])
    peak  = float(df["equity"].max())
    return {
        "points": [
            {"t": str(row["timestamp"]), "v": float(row["equity"])}
            for _, row in df.iterrows()
        ],
        "summary": {
            "start":    first,
            "current":  last,
            "peak":     peak,
            "return":   (last - first) / first * 100,
            "drawdown": (last - peak) / peak * 100,
        },
    }


# ── Screener ──────────────────────────────────────────────────────────────────

@app.get("/api/screener")
def run_screener(universe: str = "sp100", max_candidates: int = 15):
    try:
        from scanner.screener import (
            MeanReversionScreener,
            WATCHLIST_SP100, WATCHLIST_SECTOR_ETFS, WATCHLIST_LARGE_CAP,
        )
        wl_map = {
            "sp100":    WATCHLIST_SP100,
            "etfs":     WATCHLIST_SECTOR_ETFS,
            "largecap": WATCHLIST_LARGE_CAP,
        }
        wl = wl_map.get(universe, WATCHLIST_SP100)
        screener = MeanReversionScreener(
            watchlist=wl, bb_window=20, bb_std=2.0,
            rsi_window=14, max_rsi=38, max_candidates=max_candidates,
        )
        return screener.scan()
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:         str   = "AMZN"
    start:          str   = "2022-01-03"
    end:            str   = "2025-12-31"
    resolution:     str   = "15"
    strategy:       str   = "hybrid"
    initial_capital: float = 100_000
    risk_pct:       float = 0.01

@app.post("/api/backtest")
def run_backtest_api(req: BacktestRequest):
    try:
        from data.fetcher import fetch_bars_range
        from strategies.hybrid_trend_mr import HybridTrendMRStrategy
        from strategies.mean_reversion import MeanReversionStrategy
        from strategies.vwap_reversion import VWAPReversionStrategy
        from backtester.engine import run_backtest

        df = fetch_bars_range(req.symbol, req.start, req.end, resolution=req.resolution)
        df.index = pd.to_datetime(df.index)   # guard: ensure DatetimeIndex regardless of source
        if req.resolution == "15":
            df = df.between_time("14:30", "21:00")

        if df.empty:
            raise HTTPException(400, "No data returned for that symbol/range.")

        _STRAT_MAP = {
            "hybrid": HybridTrendMRStrategy,
            "mean_reversion": MeanReversionStrategy,
            "vwap_reversion": VWAPReversionStrategy,
        }
        strat_cls = _STRAT_MAP.get(req.strategy, MeanReversionStrategy)
        strat = strat_cls()
        result = run_backtest(df, strat, initial_capital=req.initial_capital, risk_pct=req.risk_pct)

        bh_ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100

        # Downsample equity curve (max 500 points for chart perf)
        eq = result.equity_curve
        if len(eq) > 500:
            step = len(eq) // 500
            eq   = eq.iloc[::step]

        trades = []
        if not result.trades.empty:
            for _, row in result.trades.head(200).iterrows():
                trades.append({
                    "entry_date":  str(row["entry_date"])[:16],
                    "exit_date":   str(row["exit_date"])[:16],
                    "entry_price": round(float(row["entry_price"]), 2),
                    "exit_price":  round(float(row["exit_price"]), 2),
                    "pnl":         round(float(row["pnl"]), 2),
                    "return_pct":  round(float(row["return_pct"]) * 100, 2),
                })

        return {
            "stats":  result.stats,
            "bh_ret": bh_ret,
            "equity_curve": [
                {"t": str(ts)[:10], "v": float(v)}
                for ts, v in zip(eq.index, eq.values)
            ],
            "trades": trades,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Hyperopt ──────────────────────────────────────────────────────────────────

class HyperoptRequest(BaseModel):
    symbol:          str   = "AMZN"
    start:           str   = "2022-01-03"
    end:             str   = "2025-12-31"
    resolution:      str   = "15"
    strategy:        str   = "mean_reversion"
    max_evals:       int   = 50
    train_pct:       float = 0.70
    objective:       str   = "sharpe_ratio"

@app.post("/api/hyperopt")
def run_hyperopt_api(req: HyperoptRequest):
    try:
        from optimization.hyperopt_runner import (
            HyperoptRunner,
            MEAN_REVERSION_SPACE,
            CRYPTO_MR_SPACE,
            CRYPTO_TREND_SPACE,
            CRYPTO_BREAKOUT_SPACE,
            CRYPTO_SUPERTREND_SPACE,
        )

        # Route to correct strategy class + search space + data fetcher
        _CRYPTO_STRATEGIES = {
            "crypto_mean_reversion",
            "crypto_trend_following",
            "crypto_breakout",
            "crypto_supertrend",
        }

        is_crypto = req.strategy in _CRYPTO_STRATEGIES

        if is_crypto:
            from data.crypto_fetcher import fetch_crypto_bars_range
            from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
            from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
            from strategies.crypto_breakout import CryptoBreakoutStrategy
            from strategies.crypto_supertrend import CryptoSupertrendStrategy
            _strat_map = {
                "crypto_mean_reversion":  (CryptoMeanReversionStrategy, CRYPTO_MR_SPACE),
                "crypto_trend_following": (CryptoTrendFollowingStrategy, CRYPTO_TREND_SPACE),
                "crypto_breakout":        (CryptoBreakoutStrategy,       CRYPTO_BREAKOUT_SPACE),
                "crypto_supertrend":      (CryptoSupertrendStrategy,     CRYPTO_SUPERTREND_SPACE),
            }
            strat_cls, search_space = _strat_map[req.strategy]
            df = fetch_crypto_bars_range(req.symbol, req.start, req.end, resolution=req.resolution)
            df.index = pd.to_datetime(df.index)
        else:
            from data.fetcher import fetch_bars_range
            from strategies.mean_reversion import MeanReversionStrategy
            strat_cls    = MeanReversionStrategy
            search_space = MEAN_REVERSION_SPACE
            df = fetch_bars_range(req.symbol, req.start, req.end, resolution=req.resolution)
            df.index = pd.to_datetime(df.index)
            if req.resolution == "15":
                df = df.between_time("14:30", "21:00")

        if df.empty:
            raise HTTPException(400, "No data returned for that symbol/range.")

        runner = HyperoptRunner(
            strategy_class=strat_cls,
            df=df,
            search_space=search_space,
            max_evals=req.max_evals,
            objective=req.objective,
            train_pct=req.train_pct,
            min_trades=2,
        )
        best_params, result = runner.optimize()

        # Build convergence curve: best loss so far at each trial
        import numpy as np
        losses = []
        for t in result.all_trials.trials:
            l = t["result"].get("loss", 999.0)
            losses.append(float(l) if l != 999.0 else None)

        best_so_far = []
        running_best = None
        for l in losses:
            if l is not None and (running_best is None or l < running_best):
                running_best = l
            best_so_far.append(float(running_best) if running_best is not None else None)

        def _stats(s):
            return {
                "total_return":  round(s.total_return * 100, 2),
                "sharpe_ratio":  round(s.sharpe_ratio, 3),
                "max_drawdown":  round(s.max_drawdown * 100, 2),
                "win_rate":      round(s.win_rate * 100, 1),
                "n_trades":      s.n_trades,
            }

        return {
            "best_params":    best_params,
            "in_sample":      _stats(result.in_sample),
            "out_of_sample":  _stats(result.out_of_sample),
            "convergence":    [
                {"trial": i + 1, "loss": l, "best": b}
                for i, (l, b) in enumerate(zip(losses, best_so_far))
            ],
            "n_trials": len(losses),
            "objective": req.objective,
        }
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e) or type(e).__name__
        if "AllTrialsFailed" in type(e).__name__ or not msg:
            raise HTTPException(500, "All optimization trials failed — try more evals, a wider date range, or a different symbol.")
        raise HTTPException(500, msg)


# ── Crypto: positions ─────────────────────────────────────────────────────────

@app.get("/api/crypto/positions")
def get_crypto_positions():
    """Return only crypto positions (symbol contains '/')."""
    try:
        positions = _trader.get_all_positions()
        return [
            {
                "symbol":           p.symbol,
                "qty":              float(p.qty),
                "entry":            float(p.avg_entry_price),
                "current":          float(p.current_price),
                "market_value":     float(p.market_value),
                "unrealized_pl":    float(p.unrealized_pl),
                "unrealized_plpc":  float(p.unrealized_plpc) * 100,
                "change_today":     float(p.change_today) * 100,
            }
            for p in positions
            if "/" in p.symbol
        ]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Crypto: backtest ──────────────────────────────────────────────────────────

class CryptoBacktestRequest(BaseModel):
    symbol:          str   = "BTC/USD"
    start:           str   = "2022-01-01"
    end:             str   = "2025-12-31"
    resolution:      str   = "60"        # 1-hour bars
    strategy:        str   = "crypto_mean_reversion"
    initial_capital: float = 100_000
    risk_pct:        float = 0.01

@app.post("/api/crypto/backtest")
def run_crypto_backtest_api(req: CryptoBacktestRequest):
    try:
        from data.crypto_fetcher import fetch_crypto_bars_range
        from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        from strategies.crypto_breakout import CryptoBreakoutStrategy
        from strategies.crypto_supertrend import CryptoSupertrendStrategy
        from backtester.engine import run_backtest

        _CRYPTO_STRATS = {
            "crypto_mean_reversion":  CryptoMeanReversionStrategy,
            "crypto_trend_following": CryptoTrendFollowingStrategy,
            "crypto_breakout":        CryptoBreakoutStrategy,
            "crypto_supertrend":      CryptoSupertrendStrategy,
        }
        strat_cls = _CRYPTO_STRATS.get(req.strategy, CryptoMeanReversionStrategy)

        df = fetch_crypto_bars_range(req.symbol, req.start, req.end, resolution=req.resolution)
        df.index = pd.to_datetime(df.index)
        # No between_time() — crypto trades 24/7

        if df.empty:
            raise HTTPException(400, "No crypto data returned for that pair/range.")

        strat  = strat_cls()
        result = run_backtest(df, strat, initial_capital=req.initial_capital, risk_pct=req.risk_pct)

        bh_ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100

        eq = result.equity_curve
        if len(eq) > 500:
            step = len(eq) // 500
            eq   = eq.iloc[::step]

        trades = []
        if not result.trades.empty:
            for _, row in result.trades.head(200).iterrows():
                trades.append({
                    "entry_date":  str(row["entry_date"])[:16],
                    "exit_date":   str(row["exit_date"])[:16],
                    "entry_price": round(float(row["entry_price"]), 4),
                    "exit_price":  round(float(row["exit_price"]), 4),
                    "pnl":         round(float(row["pnl"]), 2),
                    "return_pct":  round(float(row["return_pct"]) * 100, 2),
                })

        return {
            "stats":  result.stats,
            "bh_ret": bh_ret,
            "equity_curve": [
                {"t": str(ts)[:10], "v": float(v)}
                for ts, v in zip(eq.index, eq.values)
            ],
            "trades": trades,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Crypto: compare all pairs ─────────────────────────────────────────────────

class CryptoCompareRequest(BaseModel):
    strategy:        str   = "crypto_mean_reversion"
    start:           str   = "2022-01-01"
    end:             str   = "2025-12-31"
    resolution:      str   = "60"
    initial_capital: float = 100_000
    risk_pct:        float = 0.01

@app.post("/api/crypto/compare")
def run_crypto_compare(req: CryptoCompareRequest):
    """Run the same strategy backtest across all 12 crypto pairs, ranked by Sharpe."""
    try:
        from data.crypto_fetcher import fetch_crypto_bars_range, fetch_crypto_bars_bulk
        from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        from strategies.crypto_breakout import CryptoBreakoutStrategy
        from strategies.crypto_supertrend import CryptoSupertrendStrategy
        from backtester.engine import run_backtest
        from scanner.crypto_screener import CRYPTO_WATCHLIST

        _strat_map = {
            "crypto_mean_reversion":  CryptoMeanReversionStrategy,
            "crypto_trend_following": CryptoTrendFollowingStrategy,
            "crypto_breakout":        CryptoBreakoutStrategy,
            "crypto_supertrend":      CryptoSupertrendStrategy,
        }
        strat_cls = _strat_map.get(req.strategy, CryptoMeanReversionStrategy)

        rows = []
        for symbol in CRYPTO_WATCHLIST:
            try:
                df = fetch_crypto_bars_range(symbol, req.start, req.end, resolution=req.resolution)
                df.index = pd.to_datetime(df.index)
                if df.empty or len(df) < 50:
                    continue
                strat  = strat_cls()
                result = run_backtest(df, strat, initial_capital=req.initial_capital, risk_pct=req.risk_pct)
                s = result.stats
                bh_ret = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100
                rows.append({
                    "symbol":       symbol,
                    "sharpe":       round(s["sharpe_ratio"], 3),
                    "return_pct":   round(s["total_return_pct"] * 100, 2),
                    "bh_ret":       round(bh_ret, 2),
                    "max_drawdown": round(s["max_drawdown_pct"] * 100, 2),
                    "win_rate":     round(s["win_rate"] * 100, 1),
                    "n_trades":     s["num_trades"],
                })
            except Exception:
                continue

        rows.sort(key=lambda x: x["sharpe"], reverse=True)
        return {"strategy": req.strategy, "results": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Crypto: screener ──────────────────────────────────────────────────────────

@app.get("/api/crypto/screener")
def run_crypto_screener():
    try:
        from scanner.crypto_screener import CryptoMeanReversionScreener
        screener = CryptoMeanReversionScreener()
        return screener.scan()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/crypto/arbitrator")
async def crypto_arbitrator_status():
    """Return the latest arbitrator decisions for the dashboard."""
    try:
        import glob as _glob
        log_dir = Path(__file__).parent.parent / "logs"
        log_files = sorted(_glob.glob(str(log_dir / "crypto*.log")), reverse=True)
        if not log_files:
            return {"decisions": [], "universe": [], "message": "No crypto log files found"}

        # Parse last 100 lines for signal/arbitrator data
        decisions = []
        universe = []
        with open(log_files[0], "r") as f:
            lines = f.readlines()[-100:]
            for line in lines:
                if "conviction=" in line and ("ENTER" in line or "EXIT" in line):
                    decisions.append(line.strip())
                if "Universe:" in line:
                    universe = [line.strip()]

        return {"decisions": decisions[-20:], "universe": universe}
    except Exception as e:
        return {"error": str(e)}


# ── Bot logs ──────────────────────────────────────────────────────────────────

_LOG_FILES = {
    "equity": Path("/tmp/bot_logs/equity_scanner.log"),
    "crypto": Path("/tmp/bot_logs/crypto_scanner.log"),
}

@app.get("/api/logs")
def get_logs(lines: int = 200):
    import collections
    result = {}
    for name, path in _LOG_FILES.items():
        if path.exists():
            with open(path, "r", errors="replace") as f:
                result[name] = list(collections.deque(f, maxlen=lines))
        else:
            result[name] = [f"Log file not found: {path}\n"]
    return result


if __name__ == "__main__":
    print("\n  Alpaca Bot Dashboard")
    print(f"  Mode: {'PAPER' if _PAPER else 'LIVE'}")
    print("  Open: http://localhost:8000\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, app_dir=str(Path(__file__).parent))
