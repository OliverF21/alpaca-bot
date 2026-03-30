"""
strategies/trend_following.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Trend Following — EMA Crossover + ADX + Volume Confirmation
Designed for daily bars; also works on 15-min with adjusted params.

How it thinks (beginner explanation):
  Imagine a river current. Mean reversion says "swim against the current — it
  always snaps back." Trend following says "swim WITH the current and ride it
  as far as it goes." This strategy tries to detect when a strong current starts
  and exits only when the current reverses.

The three ingredients:
  1. EMA crossover — fast EMA (e.g. 12-day) crossing above slow EMA (e.g. 26-day)
     signals the start of an uptrend. Like two moving averages: the short one
     reacts faster and "crosses over" when momentum builds.

  2. ADX (Average Directional Index) — measures how STRONG the trend is,
     regardless of direction. ADX > 20 means a trend worth riding exists.
     ADX < 20 means choppy/sideways — we stay out.

  3. Volume confirmation — rising volume on entry bar shows conviction behind
     the move. Fakeouts usually happen on thin volume.

Entry:  fast EMA crosses above slow EMA  AND  ADX > adx_threshold  AND  volume > SMA
Exit:   fast EMA crosses below slow EMA  OR   price drops below fast EMA by a margin

Parameters tuned for daily bars (default):
  fast_ema=12, slow_ema=26  ← similar to MACD's baseline
  adx_window=14, adx_threshold=20
  stop_loss_pct=0.07        ← 7% stop (daily bars need wider stops)
  trail_pct=0.05            ← exit if price falls 5% from recent high while in trade
"""

import pandas as pd
import pandas_ta as ta
from strategies.base_strategy import BaseStrategy


class TrendFollowingStrategy(BaseStrategy):

    name = "trend_following"

    def __init__(
        self,
        fast_ema: int          = 12,
        slow_ema: int          = 26,
        adx_window: int        = 14,
        adx_threshold: float   = 15.0,
        vol_window: int        = 20,
        risk_pct: float        = 0.02,
        stop_loss_pct: float   = 0.07,
        take_profit_pct: float = 0.20,   # wide TP — let winners run
        trail_pct: float       = 0.05,   # trailing stop: exit if price drops trail_pct from recent high
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.fast_ema      = fast_ema
        self.slow_ema      = slow_ema
        self.adx_window    = adx_window
        self.adx_threshold = adx_threshold
        self.vol_window    = vol_window
        self.trail_pct     = trail_pct

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # EMA lines
        df["ema_fast"] = ta.ema(df["close"], length=self.fast_ema)
        df["ema_slow"] = ta.ema(df["close"], length=self.slow_ema)

        # ADX — measures trend strength (not direction)
        adx = ta.adx(df["high"], df["low"], df["close"], length=self.adx_window)
        if adx is not None and not adx.empty:
            # pandas_ta returns columns like ADX_14, DMP_14, DMN_14
            adx_col = next((c for c in adx.columns if c.startswith("ADX_")), None)
            df["adx"] = adx[adx_col] if adx_col else float("nan")
        else:
            df["adx"] = float("nan")

        # Volume SMA for confirmation
        df["volume_sma"] = df["volume"].rolling(self.vol_window).mean()

        # Crossover detection:
        #   cross_up   = fast crossed ABOVE slow on this bar (was below last bar)
        #   cross_down = fast crossed BELOW slow on this bar
        ema_diff          = df["ema_fast"] - df["ema_slow"]
        df["cross_up"]    = (ema_diff > 0) & (ema_diff.shift(1) <= 0)
        df["cross_down"]  = (ema_diff < 0) & (ema_diff.shift(1) >= 0)

        # Trend state: 1 when fast > slow (uptrend), -1 when fast < slow
        df["trend"]       = (ema_diff > 0).astype(int) * 2 - 1  # 1 or -1

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = float("nan")
        df["take_profit_price"] = float("nan")

        required = ["ema_fast", "ema_slow", "adx", "volume_sma"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns: {missing}. Call populate_indicators() first.")

        valid = df[required].notna().all(axis=1)

        # ── Entry: EMA cross up + ADX confirms trend + volume above average ───
        entry = (
            df["cross_up"]
            & df["adx"].gt(self.adx_threshold)
            & df["volume"].gt(df["volume_sma"])
            & valid
        )

        # ── Exit: EMA cross down (trend reversed) ────────────────────────────
        # The trailing stop is handled in the backtester via stop_price; here we
        # mark the exit at the crossdown signal so positions don't drag forever.
        exit_ = df["cross_down"] & valid

        # Collision: entry wins
        df.loc[entry, "signal"] = 1
        df.loc[exit_ & ~entry, "signal"] = -1

        # Stop price: entry close × (1 - stop_loss_pct)
        close_at_entry = df.loc[entry, "close"].astype(float)
        df.loc[entry, "stop_price"]        = (close_at_entry * (1 - self.stop_loss_pct)).round(2)
        df.loc[entry, "take_profit_price"] = (close_at_entry * (1 + self.take_profit_pct)).round(2)

        return df

    def describe(self) -> dict:
        return {
            "strategy":       self.name,
            "fast_ema":       self.fast_ema,
            "slow_ema":       self.slow_ema,
            "adx_window":     self.adx_window,
            "adx_threshold":  self.adx_threshold,
            "stop_loss_pct":  self.stop_loss_pct,
            "take_profit_pct":self.take_profit_pct,
            "trail_pct":      self.trail_pct,
        }
