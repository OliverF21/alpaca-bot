"""
strategies/crypto_supertrend.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Supertrend strategy tuned for crypto 1-hour bars.

Why Supertrend beats simple EMA crossover and tends to outperform buy-and-hold:
  1. The Supertrend line itself IS the trailing stop — it adapts to actual
     volatility (ATR-based), not a fixed percentage.
  2. In a sustained bull run it stays bullish the entire time — you ride
     the whole trend without premature exit signals.
  3. When volatility spikes (crash risk) the stop widens, then tightens
     again as volatility compresses — self-calibrating to regime.
  4. Only one meaningful parameter to optimize (multiplier) vs 6-8 for EMA
     systems — far less overfitting risk.

Strategy logic:
  Entry:  Supertrend direction flips from -1 → +1
          (price closes above the Supertrend line — uptrend confirmed)
          Optional: volume > vol_sma (filter noise)
          Optional: RSI > rsi_min (momentum confirmation, avoids dead-cat bounces)

  Stop:   Supertrend line value at entry (adaptive ATR stop)
          Stop is trailed each bar as Supertrend line moves up

  Exit:   Supertrend direction flips from +1 → -1
          (price closes below the Supertrend line — trend broken)

  The Supertrend line is calculated as:
      midpoint = (high + low) / 2
      upper_band = midpoint + multiplier × ATR(atr_period)
      lower_band = midpoint - multiplier × ATR(atr_period)
      — bands are then "ratcheted" (lower band can only go up in uptrend,
        upper band can only go down in downtrend) to prevent flip-flopping

Compatible with HyperoptRunner using CRYPTO_SUPERTREND_SPACE from hyperopt_runner.py.
"""

import pandas as pd
import pandas_ta as ta
import numpy as np

from strategies.base_strategy import BaseStrategy


class CryptoSupertrendStrategy(BaseStrategy):

    name = "crypto_supertrend"

    def __init__(
        self,
        atr_period: int        = 10,       # ATR period for Supertrend calculation
        multiplier: float      = 3.0,      # ATR multiplier — higher = wider bands = fewer signals
        vol_filter: bool       = True,     # require volume > vol_sma to enter
        vol_sma_period: int    = 20,       # volume SMA period
        rsi_min: float         = 45.0,     # minimum RSI to enter (momentum filter; 0 = disabled)
        rsi_period: int        = 14,
        min_hold_bars: int     = 2,        # don't exit the very next bar after entry
        risk_pct: float        = 0.01,
        stop_loss_pct: float   = 0.05,     # fallback stop if Supertrend unavailable
        take_profit_pct: float = 0.20,     # wide TP — let the trend run
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.atr_period     = atr_period
        self.multiplier     = multiplier
        self.vol_filter     = vol_filter
        self.vol_sma_period = vol_sma_period
        self.rsi_min        = rsi_min
        self.rsi_period     = rsi_period
        self.min_hold_bars  = min_hold_bars

    # ──────────────────────────────────────────────────────────────────────────

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── Supertrend ────────────────────────────────────────────────────────
        # pandas_ta returns a DataFrame with columns:
        #   SUPERT_{period}_{mult}     — the indicator line value
        #   SUPERTd_{period}_{mult}    — direction: +1 bullish, -1 bearish
        #   SUPERTl_{period}_{mult}    — long (lower) band
        #   SUPERTs_{period}_{mult}    — short (upper) band
        st = ta.supertrend(
            df["high"], df["low"], df["close"],
            length=self.atr_period,
            multiplier=self.multiplier,
        )

        if st is not None and not st.empty:
            cols = st.columns.tolist()
            col_val  = next((c for c in cols if c.startswith("SUPERT_")), None)
            col_dir  = next((c for c in cols if c.startswith("SUPERTd_")), None)
            if col_val and col_dir:
                df["supertrend"]     = st[col_val]
                df["supertrend_dir"] = st[col_dir]
            else:
                df["supertrend"]     = float("nan")
                df["supertrend_dir"] = 0
        else:
            df["supertrend"]     = float("nan")
            df["supertrend_dir"] = 0

        # ── Volume SMA ────────────────────────────────────────────────────────
        df["vol_sma"] = df["volume"].rolling(self.vol_sma_period).mean()

        # ── RSI (momentum confirmation) ───────────────────────────────────────
        rsi = ta.rsi(df["close"], length=self.rsi_period)
        df["rsi"] = rsi if rsi is not None else float("nan")

        return df

    # ──────────────────────────────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = float("nan")
        df["take_profit_price"] = float("nan")

        required = ["supertrend", "supertrend_dir"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns: {missing}. Call populate_indicators() first.")

        # ── Entry: direction flips to bullish (+1) ────────────────────────────
        # Direction was -1 or 0 on previous bar, now +1
        prev_dir = df["supertrend_dir"].shift(1).fillna(0)
        flip_up  = (df["supertrend_dir"] == 1) & (prev_dir != 1)

        # Volume filter: optional
        if self.vol_filter:
            vol_ok = df["volume"].gt(df["vol_sma"])
        else:
            vol_ok = pd.Series(True, index=df.index)

        # RSI filter: optional (rsi_min = 0 effectively disables it)
        if self.rsi_min > 0 and "rsi" in df.columns:
            rsi_ok = df["rsi"].gt(self.rsi_min)
        else:
            rsi_ok = pd.Series(True, index=df.index)

        valid = df["supertrend_dir"].notna() & df["supertrend"].notna()
        entry = flip_up & vol_ok & rsi_ok & valid

        # ── Exit: direction flips to bearish (−1) ─────────────────────────────
        flip_down = (df["supertrend_dir"] == -1) & (prev_dir != -1)
        base_exit = flip_down & valid

        # min_hold_bars protection
        entry_int = entry.astype(int)
        protected = (
            entry_int
            .shift(1)
            .rolling(window=max(self.min_hold_bars - 1, 1), min_periods=1)
            .sum()
            .fillna(0)
            .gt(0)
        )
        exit_ = base_exit & ~protected

        # Entry wins on collision
        df.loc[entry, "signal"] = 1
        df.loc[exit_ & ~entry, "signal"] = -1

        # ── Stop: Supertrend line at entry (adaptive ATR stop) ────────────────
        close_at_entry = df.loc[entry, "close"].astype(float)
        st_at_entry    = df.loc[entry, "supertrend"].astype(float)

        if st_at_entry.notna().any():
            pct_stop = (close_at_entry * (1 - self.stop_loss_pct)).round(4)
            df.loc[entry, "stop_price"] = st_at_entry.where(
                st_at_entry.notna(), pct_stop
            ).round(4)
        else:
            df.loc[entry, "stop_price"] = (
                close_at_entry * (1 - self.stop_loss_pct)
            ).round(4)

        df.loc[entry, "take_profit_price"] = (
            close_at_entry * (1 + self.take_profit_pct)
        ).round(4)

        return df

    # ──────────────────────────────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "strategy":        self.name,
            "atr_period":      self.atr_period,
            "multiplier":      self.multiplier,
            "vol_filter":      self.vol_filter,
            "rsi_min":         self.rsi_min,
            "min_hold_bars":   self.min_hold_bars,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
