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
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

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
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

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


# ── Version / build info ─────────────────────────────────────────────────────

_STARTED_AT = datetime.utcnow().isoformat(timespec="seconds") + "Z"

def _git_info() -> dict:
    """Return current git SHA + branch by reading .git directly (no subprocess).

    We parse .git/HEAD + the referenced ref file rather than shelling out to
    `git rev-parse` so this works in hardened environments where subprocess is
    restricted. Falls back to 'unknown' on any parse failure.
    """
    out = {"git_sha": "unknown", "git_branch": "unknown"}
    try:
        head = (_REPO / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            out["git_branch"] = ref.rsplit("/", 1)[-1]
            ref_file = _REPO / ".git" / ref
            if ref_file.exists():
                out["git_sha"] = ref_file.read_text().strip()
            else:
                # Packed refs fallback
                packed = _REPO / ".git" / "packed-refs"
                if packed.exists():
                    for line in packed.read_text().splitlines():
                        if line.endswith(" " + ref):
                            out["git_sha"] = line.split(" ", 1)[0]
                            break
        else:
            # Detached HEAD — HEAD itself is the SHA
            out["git_sha"] = head
            out["git_branch"] = "(detached)"
    except Exception:
        pass
    return out

def _loaded_strategies() -> dict:
    """Extract strategy class names by parsing the scanner entry-point files.

    We can't introspect the running scanner subprocesses from the webapp
    process, but the entry-point source files are the source of truth for
    what each run_all.py child actually loads. Any change to which strategies
    are live requires editing these files, so parsing them reflects reality.
    """
    import re
    out = {"equity": [], "crypto": []}
    for name, path in [
        ("equity", _REPO / "scanner" / "run_scanner.py"),
        ("crypto", _REPO / "scanner" / "run_crypto_scanner.py"),
    ]:
        try:
            src = path.read_text()
            # Match `from strategies.<x> import <ClassName>Strategy`
            out[name] = re.findall(r"from strategies\.\w+ import (\w+Strategy)", src)
        except Exception:
            pass
    return out

@app.get("/api/version")
def get_version():
    """Report what code this dashboard process is running.

    Used by the recap pipeline to verify remote hosts (e.g. the Pi) are on
    the expected commit — `curl http://pi.local:8000/api/version` shows the
    git SHA, branch, which strategy classes are loaded by each scanner, and
    when this process started. See issue #11.
    """
    import socket
    strategies = _loaded_strategies()
    return {
        **_git_info(),
        "started_at":        _STARTED_AT,
        "hostname":          socket.gethostname(),
        "equity_strategies": strategies["equity"],
        "crypto_strategies": strategies["crypto"],
        "paper":             _PAPER,
    }


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
            "cash":          float(a.non_marginable_buying_power),
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
    """Return equity-only positions (excludes crypto)."""
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
            if not _is_crypto(p.symbol)
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


@app.get("/api/trades")
def get_closed_trades(limit: int = 50):
    """Pair filled BUY/SELL orders into round-trip trades with P&L."""
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=200)
        orders = _trader.get_orders(req)

        # Only filled orders with a price
        fills = [
            o for o in orders
            if o.status.value == "filled" and o.filled_avg_price
        ]
        # Oldest first so we match buys before sells
        fills.sort(key=lambda o: o.filled_at or o.created_at)

        # Track open buys per symbol
        open_buys: dict[str, list] = {}
        trades = []

        for o in fills:
            sym = o.symbol
            if o.side.value == "buy":
                open_buys.setdefault(sym, []).append(o)
            elif o.side.value == "sell" and open_buys.get(sym):
                buy = open_buys[sym].pop(0)
                buy_price = float(buy.filled_avg_price)
                sell_price = float(o.filled_avg_price)
                qty = min(float(buy.qty), float(o.qty))
                pnl = (sell_price - buy_price) * qty
                pnl_pct = ((sell_price - buy_price) / buy_price) * 100 if buy_price else 0
                trades.append({
                    "symbol":     sym,
                    "qty":        qty,
                    "buy_price":  round(buy_price, 4),
                    "sell_price": round(sell_price, 4),
                    "pnl":        round(pnl, 2),
                    "pnl_pct":    round(pnl_pct, 2),
                    "entry_time": (buy.filled_at or buy.created_at).isoformat(),
                    "exit_time":  (o.filled_at or o.created_at).isoformat(),
                })

        # Most recent first, capped
        trades.reverse()
        return trades[:limit]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Equity log ────────────────────────────────────────────────────────────────

@app.get("/api/equity-log")
def get_equity_log(days: int = 30):
    cutoff = date.today() - timedelta(days=days)
    frames = []

    # Load local CSV files (higher granularity — 1-minute polls)
    log_dir = _REPO / "equity_logs"
    if log_dir.exists():
        for f in sorted(log_dir.glob("equity_log_*.csv")):
            try:
                date_str = f.stem.replace("equity_log_", "")
                if date.fromisoformat(date_str) >= cutoff:
                    frames.append(pd.read_csv(f, parse_dates=["timestamp"]))
            except Exception:
                pass

    # Always fetch Alpaca portfolio history (1-day bars) as fallback/supplement
    # This ensures we always have equity data even on fresh install
    try:
        # Map days to Alpaca period strings to avoid fetching excess data
        if days <= 7:
            period_str = "1W"
        elif days <= 30:
            period_str = "1M"
        elif days <= 90:
            period_str = "3M"
        elif days <= 365:
            period_str = "1A"
        else:
            period_str = "1A"   # max Alpaca supports
        hist_req = GetPortfolioHistoryRequest(
            period=period_str,
            timeframe="1D",
        )
        portfolio_hist = _trader.get_portfolio_history(hist_req)
        if portfolio_hist and hasattr(portfolio_hist, 'timestamp') and hasattr(portfolio_hist, 'equity'):
            # portfolio_hist.timestamp is list of epoch ints, portfolio_hist.equity is list of floats
            hist_data = []
            for ts, eq in zip(portfolio_hist.timestamp or [], portfolio_hist.equity or []):
                try:
                    # Convert epoch (seconds or millis) to datetime
                    # If ts > 1e10, assume millis; else assume seconds
                    ts_sec = ts / 1000 if ts > 1e10 else ts
                    dt = datetime.fromtimestamp(ts_sec)
                    hist_data.append({"timestamp": dt, "equity": float(eq)})
                except Exception:
                    pass
            if hist_data:
                frames.append(pd.DataFrame(hist_data))
    except Exception as e:
        log.warning(f"Could not fetch Alpaca portfolio history: {e}")

    if not frames:
        return {"points": [], "summary": {}}

    # Merge all data sources
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates("timestamp")

    # Apply cutoff filter (Alpaca API returns full period regardless of `days` param)
    cutoff_dt = pd.Timestamp(cutoff)
    df = df[df["timestamp"] >= cutoff_dt]

    # Downsample to max 500 points for chart performance
    if len(df) > 500:
        step = len(df) // 500
        df = df.iloc[::step]

    if df.empty:
        return {"points": [], "summary": {}}

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


# ── Crypto: positions ─────────────────────────────────────────────────────────

_CRYPTO_BASES = {"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "UNI", "AAVE",
                 "DOT", "MATIC", "SHIB", "LTC", "XRP", "ADA", "ATOM", "ALGO"}

def _is_crypto(symbol: str) -> bool:
    """Detect crypto positions — Alpaca returns 'DOGEUSD' (no slash) for positions
    but 'DOGE/USD' for orders. Handle both formats."""
    if "/" in symbol:
        return True
    for suffix in ("USDT", "USDC", "USD"):
        if symbol.endswith(suffix) and symbol[:-len(suffix)] in _CRYPTO_BASES:
            return True
    return False

def _format_crypto_symbol(symbol: str) -> str:
    """Normalize 'DOGEUSD' → 'DOGE/USD' for display."""
    if "/" in symbol:
        return symbol
    for suffix in ("USDT", "USDC", "USD"):
        if symbol.endswith(suffix):
            return symbol[:-len(suffix)] + "/" + suffix
    return symbol

@app.get("/api/crypto/positions")
def get_crypto_positions():
    """Return only crypto positions."""
    try:
        positions = _trader.get_all_positions()
        return [
            {
                "symbol":           _format_crypto_symbol(p.symbol),
                "qty":              float(p.qty),
                "entry":            float(p.avg_entry_price),
                "current":          float(p.current_price),
                "market_value":     float(p.market_value),
                "unrealized_pl":    float(p.unrealized_pl),
                "unrealized_plpc":  float(p.unrealized_plpc) * 100,
                "change_today":     float(p.change_today) * 100,
            }
            for p in positions
            if _is_crypto(p.symbol)
        ]
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Crypto: arbitrator ────────────────────────────────────────────────────────

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
