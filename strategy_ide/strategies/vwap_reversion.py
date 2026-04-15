"""
strategies/vwap_reversion.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VWAP Mean Reversion — designed for 5-minute bars, multiple trades per day.

How it works:
─────────────
VWAP (Volume-Weighted Average Price) is the institutional benchmark — it's
where the "fair value" sits each day. When price deviates far below VWAP,
it tends to revert back. This strategy exploits that pull.

  1. VWAP resets daily. We compute it as cumsum(typical_price × volume) /
     cumsum(volume), grouped by trading day.

  2. We compute an expanding standard deviation of (close - VWAP) within
     each day. This gives us adaptive bands: wide on volatile days, tight
     on quiet ones.

  3. ENTRY: price drops below VWAP - deviation_mult × σ AND RSI is oversold
     AND we're past the first hour (VWAP needs time to stabilise).

  4. EXIT: price returns to VWAP (the mean reversion target) OR RSI
     recovers above sell_rsi. Stop-loss and take-profit bracket the trade.

Why this generates more trades than Bollinger Band mean reversion:
  - VWAP resets daily → fresh setup every day (BB needs 20+ bars to warm up)
  - 5-min bars → 78 bars per day vs 26 on 15-min → 3× more signal opportunities
  - Deviation bands adapt intraday → entries trigger on moderate dips, not just
    extreme selloffs that happen once a month
"""

import os

import numpy as np
import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta
from strategies.base_strategy import BaseStrategy


class VWAPReversionStrategy(BaseStrategy):

    name = "vwap_reversion"

    def __init__(
        self,
        rsi_window: int        = 10,
        buy_rsi: int           = 40,
        sell_rsi: int          = 55,
        vwap_dev_mult: float   = 1.5,
        min_bars_in_day: int   = 12,     # skip first hour (12 × 5min = 60min)
        stop_loss_pct: float   = 0.005,  # 0.5% stop — tight for 5-min
        take_profit_pct: float = 0.01,   # 1.0% TP
        risk_pct: float        = 0.01,
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.rsi_window      = rsi_window
        self.buy_rsi         = buy_rsi
        self.sell_rsi        = sell_rsi
        self.vwap_dev_mult   = vwap_dev_mult
        self.min_bars_in_day = min_bars_in_day

    # ── Indicators ───────────────────────────────────────────────────────────

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # Typical price (industry standard VWAP input)
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        tp_vol = tp * df["volume"]

        # Group by calendar date for daily VWAP reset
        dates = df.index.date
        df["_date"] = dates

        cum_tp_vol = df.groupby("_date")["volume"].transform("first")  # placeholder
        # Compute cumulative sums within each day
        df["_tp_vol"] = tp_vol
        df["_cum_tp_vol"] = df.groupby("_date")["_tp_vol"].cumsum()
        df["_cum_vol"] = df.groupby("_date")["volume"].cumsum()

        # VWAP = cumulative(TP × Vol) / cumulative(Vol)
        df["vwap"] = df["_cum_tp_vol"] / df["_cum_vol"].replace(0, np.nan)

        # Expanding std dev of (close - VWAP) within each day
        df["_vwap_diff"] = df["close"] - df["vwap"]
        df["vwap_std"] = df.groupby("_date")["_vwap_diff"].transform(
            lambda x: x.expanding(min_periods=6).std()
        )

        # VWAP deviation bands
        df["vwap_upper"] = df["vwap"] + self.vwap_dev_mult * df["vwap_std"]
        df["vwap_lower"] = df["vwap"] - self.vwap_dev_mult * df["vwap_std"]

        # RSI (short window for 5-min responsiveness)
        df["rsi"] = ta.rsi(df["close"], length=self.rsi_window)

        # Bar count within each day (for min_bars_in_day filter)
        df["bar_of_day"] = df.groupby("_date").cumcount() + 1

        # Clean up temp columns
        df.drop(
            columns=["_date", "_tp_vol", "_cum_tp_vol", "_cum_vol", "_vwap_diff"],
            inplace=True,
        )

        return df

    # ── Signals ──────────────────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = np.nan
        df["take_profit_price"] = np.nan
        df["reason"]            = ""

        required = ["vwap", "vwap_lower", "rsi", "vwap_std", "bar_of_day"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(
                f"Missing columns: {missing}. Call populate_indicators() first."
            )

        # Entry: price below lower VWAP band + RSI oversold + past first hour
        entry = (
            df["close"].lt(df["vwap_lower"])
            & df["rsi"].lt(self.buy_rsi)
            & df["bar_of_day"].ge(self.min_bars_in_day)
            & df["vwap_std"].gt(0)
            & df[["vwap", "vwap_lower", "rsi"]].notna().all(axis=1)
        )

        # Exit: price returns to VWAP or RSI recovers
        vwap_touch = df["close"].ge(df["vwap"])
        rsi_over   = df["rsi"].gt(self.sell_rsi)
        exit_      = vwap_touch | rsi_over

        # Entry wins on collision bar
        df.loc[entry, "signal"] = 1
        df.loc[exit_ & ~entry, "signal"] = -1

        # Set stop / take-profit at entry bars
        close_at_entry = df.loc[entry, "close"].astype(float)
        df.loc[entry, "stop_price"] = (
            close_at_entry * (1 - self.stop_loss_pct)
        ).round(2)
        df.loc[entry, "take_profit_price"] = (
            close_at_entry * (1 + self.take_profit_pct)
        ).round(2)

        # Populate reason column for observability (issue #3)
        exit_rows = exit_ & ~entry
        df.loc[entry, "reason"] = "vwap_lower+rsi_oversold"
        df.loc[exit_rows & vwap_touch & ~rsi_over, "reason"] = "vwap_touch"
        df.loc[exit_rows & ~vwap_touch & rsi_over, "reason"] = "rsi_overbought"
        df.loc[exit_rows & vwap_touch & rsi_over, "reason"]  = "vwap_touch+rsi_overbought"

        return df

    # ── Metadata ─────────────────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "strategy":        self.name,
            "rsi_window":      self.rsi_window,
            "buy_rsi":         self.buy_rsi,
            "sell_rsi":        self.sell_rsi,
            "vwap_dev_mult":   self.vwap_dev_mult,
            "min_bars_in_day": self.min_bars_in_day,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
