"""
dashboard/app.py
━━━━━━━━━━━━━━━━
Alpaca Bot — Live Monitoring Dashboard

Pages:
  Monitor   — account equity, open positions, bot activity  (auto-refreshes)
  Backtest  — run hybrid or mean reversion strategy on any symbol
  Screener  — see which symbols are currently passing the pre-filter
  Equity    — plot the equity curve from equity_monitor CSV logs

Run from repo root:
    streamlit run strategy_ide/dashboard/app.py
"""

import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_DASH   = Path(__file__).resolve().parent          # strategy_ide/dashboard/
_IDE    = _DASH.parent                             # strategy_ide/
_REPO   = _IDE.parent                             # alpaca_bot/
for p in [str(_IDE), str(_REPO)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(_REPO / ".env")
load_dotenv(_IDE / ".env", override=False)

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(
    page_title="Alpaca Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Alpaca client (shared) ────────────────────────────────────────────────────
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

_API_KEY    = os.getenv("ALPACA_API_KEY", "")
_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

@st.cache_resource
def _client():
    return TradingClient(_API_KEY, _SECRET_KEY, paper=_PAPER)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: MONITOR
# ══════════════════════════════════════════════════════════════════════════════

def page_monitor():
    st.header("Live Monitor")

    client = _client()

    # ── Account summary ───────────────────────────────────────────────────────
    try:
        acct = client.get_account()
        equity       = float(acct.equity)
        last_equity  = float(acct.last_equity)
        cash         = float(acct.cash)
        buying_power = float(acct.buying_power)
        long_mkt     = float(acct.long_market_value)
        daily_pl     = equity - last_equity
        daily_pl_pct = daily_pl / last_equity * 100 if last_equity else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Equity",        f"${equity:,.2f}")
        c2.metric("Daily P&L",     f"${daily_pl:+,.2f}",
                  delta=f"{daily_pl_pct:+.2f}%",
                  delta_color="normal")
        c3.metric("Invested",      f"${long_mkt:,.2f}")
        c4.metric("Cash / BP",     f"${cash:,.0f} / ${buying_power:,.0f}")
    except Exception as e:
        st.error(f"Could not reach Alpaca: {e}")
        return

    st.divider()

    # ── Open positions ────────────────────────────────────────────────────────
    st.subheader("Open positions")
    try:
        raw_positions = client.get_all_positions()
        if not raw_positions:
            st.info("No open positions.")
        else:
            rows = []
            for p in raw_positions:
                entry   = float(p.avg_entry_price)
                current = float(p.current_price)
                qty     = float(p.qty)
                upl     = float(p.unrealized_pl)
                uplpct  = float(p.unrealized_plpc) * 100
                rows.append({
                    "Symbol":       p.symbol,
                    "Qty":          int(qty),
                    "Entry $":      f"{entry:.2f}",
                    "Current $":    f"{current:.2f}",
                    "Unreal. P&L":  f"${upl:+,.2f}",
                    "Unreal. %":    f"{uplpct:+.2f}%",
                    "Mkt Value":    f"${float(p.market_value):,.2f}",
                })
            df_pos = pd.DataFrame(rows)

            def _colour_pl(val):
                if isinstance(val, str) and val.startswith("$"):
                    v = float(val.replace("$","").replace(",","").replace("+",""))
                    return "color: #2ecc71" if v > 0 else ("color: #e74c3c" if v < 0 else "")
                if isinstance(val, str) and val.endswith("%"):
                    v = float(val.replace("%","").replace("+",""))
                    return "color: #2ecc71" if v > 0 else ("color: #e74c3c" if v < 0 else "")
                return ""

            st.dataframe(
                df_pos.style.applymap(_colour_pl, subset=["Unreal. P&L", "Unreal. %"]),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as e:
        st.error(f"Positions error: {e}")

    st.divider()

    # ── Recent orders (last 20) ───────────────────────────────────────────────
    st.subheader("Recent bot activity (last 20 orders)")
    try:
        req    = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20)
        orders = client.get_orders(req)
        if not orders:
            st.info("No orders yet.")
        else:
            rows = []
            for o in orders:
                filled_at = o.filled_at.strftime("%m-%d %H:%M") if o.filled_at else "—"
                filled_px = f"${float(o.filled_avg_price):.2f}" if o.filled_avg_price else "—"
                rows.append({
                    "Time":    filled_at,
                    "Symbol":  o.symbol,
                    "Side":    o.side.value.upper(),
                    "Qty":     o.qty,
                    "Fill $":  filled_px,
                    "Type":    o.type.value,
                    "Status":  o.status.value,
                })
            df_orders = pd.DataFrame(rows)

            def _colour_side(val):
                if val == "BUY":  return "color: #2ecc71"
                if val == "SELL": return "color: #e74c3c"
                return ""

            st.dataframe(
                df_orders.style.applymap(_colour_side, subset=["Side"]),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as e:
        st.error(f"Orders error: {e}")

    st.divider()

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    interval = st.sidebar.selectbox("Auto-refresh", [15, 30, 60, 120, 300], index=2,
                                    format_func=lambda s: f"Every {s}s")
    placeholder = st.empty()
    for remaining in range(interval, 0, -1):
        placeholder.caption(f"Refreshing in {remaining}s  •  Last updated: {datetime.now().strftime('%H:%M:%S')}")
        time.sleep(1)
    placeholder.empty()
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def page_backtest():
    st.header("Backtest")

    from data.fetcher import fetch_bars_range
    from strategies.hybrid_trend_mr import HybridTrendMRStrategy
    from strategies.mean_reversion import MeanReversionStrategy
    from backtester.engine import run_backtest

    STRATEGIES = {
        "Hybrid (Trend Filter + Mean Reversion)": HybridTrendMRStrategy,
        "Mean Reversion only":                    MeanReversionStrategy,
    }

    col1, col2 = st.columns([2, 1])
    with col1:
        symbol   = st.text_input("Symbol", value="AMZN").strip().upper()
        strategy_name = st.selectbox("Strategy", list(STRATEGIES.keys()))
    with col2:
        resolution = st.selectbox("Bar size", ["15-min", "Daily"],
                                  help="15-min needs Alpaca data (years of history). Daily uses yfinance.")
        res_code = "15" if resolution == "15-min" else "D"

    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input("Start", value=date.today() - timedelta(days=4*365))
    with c2:
        end   = st.date_input("End",   value=date.today())

    initial_capital = st.number_input("Initial capital ($)", value=100_000, step=10_000)
    risk_pct        = st.slider("Risk per trade (%)", 0.5, 5.0, 1.0, 0.5) / 100

    if st.button("Run backtest", type="primary"):
        if start >= end:
            st.error("Start must be before end.")
            return

        with st.spinner(f"Fetching {resolution} data for {symbol}…"):
            df = fetch_bars_range(symbol, str(start), str(end), resolution=res_code)
            if res_code == "15":
                df = df.between_time("14:30", "21:00")

        if df.empty:
            st.error("No data returned — check symbol and date range.")
            return

        st.caption(f"{len(df):,} bars  •  {df.index[0].date()} → {df.index[-1].date()}")

        with st.spinner("Running backtest…"):
            strat  = STRATEGIES[strategy_name]()
            result = run_backtest(df, strat, initial_capital=float(initial_capital),
                                  risk_pct=risk_pct)

        s = result.stats

        # Buy & hold comparison
        bh_ret = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        strat_ret = s["total_return_pct"] * 100
        vs = strat_ret - bh_ret

        st.subheader("Results")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Strategy return", f"{strat_ret:+.2f}%")
        c2.metric("Buy & hold",      f"{bh_ret:+.2f}%",
                  delta=f"{'beat' if vs > 0 else 'lag'} by {abs(vs):.1f}pp",
                  delta_color="normal" if vs > 0 else "inverse")
        c3.metric("Sharpe",          f"{s['sharpe_ratio']:.2f}")
        c4.metric("Max drawdown",    f"{s['max_drawdown_pct']*100:.1f}%")
        c5.metric("Win rate",        f"{s['win_rate']*100:.0f}%  ({s['num_trades']} trades)")

        # Equity curve
        eq  = result.equity_curve
        bh  = initial_capital * (df["close"] / df["close"].iloc[0])
        bh  = bh.reindex(eq.index, method="ffill")

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Strategy", line=dict(color="#3498db", width=2)))
        fig.add_trace(go.Scatter(x=bh.index, y=bh.values,  name="Buy & Hold", line=dict(color="#95a5a6", width=1, dash="dash")))
        fig.update_layout(
            xaxis_title="Date", yaxis_title="Equity ($)",
            legend=dict(orientation="h", y=1.05),
            margin=dict(l=0, r=0, t=30, b=0),
            height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Trades table
        if not result.trades.empty:
            st.subheader("Trades")
            trades = result.trades.copy()
            trades["entry_date"] = trades["entry_date"].astype(str)
            trades["exit_date"]  = trades["exit_date"].astype(str)
            trades["return_pct"] = (trades["return_pct"] * 100).round(2).astype(str) + "%"
            trades["pnl"]        = trades["pnl"].map(lambda x: f"${x:+,.2f}")
            st.dataframe(trades, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: SCREENER
# ══════════════════════════════════════════════════════════════════════════════

def page_screener():
    st.header("Screener")
    st.caption("Stocks currently passing the mean-reversion pre-filter. Lower score = stronger setup.")

    try:
        from scanner.screener import MeanReversionScreener, WATCHLIST_SP100, WATCHLIST_SECTOR_ETFS, WATCHLIST_LARGE_CAP
    except ImportError:
        st.error("Screener module not found. Run from the repo root.")
        return

    watchlists = {
        "S&P 100 (90 symbols)":   WATCHLIST_SP100,
        "Sector ETFs (20)":        WATCHLIST_SECTOR_ETFS,
        "Large Cap (15, fastest)": WATCHLIST_LARGE_CAP,
    }
    label     = st.selectbox("Watchlist", list(watchlists.keys()))
    watchlist = watchlists[label]
    max_cands = st.slider("Max candidates to return", 5, 30, 15)

    if st.button("Run scan", type="primary"):
        with st.spinner(f"Scanning {len(watchlist)} symbols for mean-reversion setups…"):
            try:
                screener = MeanReversionScreener(
                    watchlist=watchlist, bb_window=20, bb_std=2.0,
                    rsi_window=14, max_rsi=38, max_candidates=max_cands,
                )
                results = screener.scan()
            except Exception as e:
                st.error(f"Scan failed: {e}")
                return

        if not results:
            st.info("No candidates found right now — market may not have any oversold setups.")
            return

        df = pd.DataFrame(results)
        df.columns = [c.title() for c in df.columns]
        df["Score"] = df["Score"].round(3)
        df["Rsi"]   = df["Rsi"].round(1) if "Rsi" in df.columns else df.get("RSI", "")

        def _highlight(row):
            score = row.get("Score", 1.0)
            if score < 0.25:
                return ["background-color:#d4edda"] * len(row)
            if score < 0.45:
                return ["background-color:#fff3cd"] * len(row)
            return [""] * len(row)

        st.dataframe(
            df.style.apply(_highlight, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        st.caption("Green = strong setup (score < 0.25)  •  Yellow = moderate (0.25–0.45)")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: EQUITY CURVE
# ══════════════════════════════════════════════════════════════════════════════

def page_equity():
    st.header("Equity Curve")
    st.caption("Pulled from equity_monitor CSV logs written by the running bot.")

    log_dir = _REPO / "equity_logs"
    csv_files = sorted(log_dir.glob("equity_log_*.csv"), reverse=True) if log_dir.exists() else []

    if not csv_files:
        st.info(
            "No equity logs found yet. The bot writes to `equity_logs/` while it is running. "
            "Start the scanner and come back here."
        )
        return

    # Let user pick a date or load all
    options = ["All time"] + [f.stem.replace("equity_log_", "") for f in csv_files]
    selected = st.selectbox("Date", options)

    if selected == "All time":
        frames = []
        for f in csv_files:
            try:
                frames.append(pd.read_csv(f, parse_dates=["timestamp"]))
            except Exception:
                pass
        df = pd.concat(frames).sort_values("timestamp").drop_duplicates("timestamp") if frames else pd.DataFrame()
    else:
        target = log_dir / f"equity_log_{selected}.csv"
        try:
            df = pd.read_csv(target, parse_dates=["timestamp"])
        except Exception as e:
            st.error(f"Could not load {target}: {e}")
            return

    if df.empty or "equity" not in df.columns:
        st.warning("Log file is empty or missing 'equity' column.")
        return

    # Summary metrics
    first_eq = float(df["equity"].iloc[0])
    last_eq  = float(df["equity"].iloc[-1])
    peak_eq  = float(df["equity"].max())
    dd       = (last_eq - peak_eq) / peak_eq * 100
    total_r  = (last_eq - first_eq) / first_eq * 100

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Starting equity", f"${first_eq:,.2f}")
    c2.metric("Current equity",  f"${last_eq:,.2f}", delta=f"{total_r:+.2f}%")
    c3.metric("Peak equity",     f"${peak_eq:,.2f}")
    c4.metric("Drawdown from peak", f"{dd:.2f}%", delta_color="inverse")

    # Equity line chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["equity"],
        name="Equity", fill="tozeroy",
        line=dict(color="#3498db", width=2),
        fillcolor="rgba(52,152,219,0.1)",
    ))
    fig.update_layout(
        xaxis_title="Time", yaxis_title="Equity ($)",
        margin=dict(l=0, r=0, t=10, b=0), height=350,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Daily P&L bars if timestamp spans multiple days
    if "equity" in df.columns and len(df) > 1:
        df["date"] = pd.to_datetime(df["timestamp"]).dt.date
        daily = df.groupby("date")["equity"].agg(["first", "last"])
        daily["pnl"] = daily["last"] - daily["first"]
        if len(daily) > 1:
            st.subheader("Daily P&L")
            fig2 = go.Figure(go.Bar(
                x=daily.index.astype(str),
                y=daily["pnl"],
                marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in daily["pnl"]],
            ))
            fig2.update_layout(
                xaxis_title="Date", yaxis_title="P&L ($)",
                margin=dict(l=0, r=0, t=10, b=0), height=250,
            )
            st.plotly_chart(fig2, use_container_width=True)

    with st.expander("Raw data"):
        st.dataframe(df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR + ROUTING
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # Trading mode badge
    if _PAPER:
        st.sidebar.success("PAPER TRADING")
    else:
        st.sidebar.error("LIVE TRADING")

    st.sidebar.title("Alpaca Bot")

    page = st.sidebar.radio(
        "Navigation",
        ["Monitor", "Backtest", "Screener", "Equity Curve"],
        label_visibility="collapsed",
    )

    if page == "Monitor":
        page_monitor()
    elif page == "Backtest":
        page_backtest()
    elif page == "Screener":
        page_screener()
    elif page == "Equity Curve":
        page_equity()


if __name__ == "__main__":
    main()
