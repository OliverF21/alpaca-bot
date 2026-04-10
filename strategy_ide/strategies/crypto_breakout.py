"""
strategies/crypto_breakout.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Donchian Channel Breakout + volume confirmation + ATR stop,
tuned for crypto 1-hour bars.

When it outperforms mean reversion:
  When volatility is expanding (squeeze → breakout). Crypto frequently
  consolidates for hours/days then breaks sharply. Mean reversion fades
  these moves; this strategy buys them.

Strategy logic:
  Entry:  Close breaks above the N-bar Donchian high (channel_window bars)
          AND volume > vol_mult × SMA20 (real breakout, not noise)
          AND ATR has expanded above its own SMA (volatility is live)

  Stop:   entry close − atr_stop_mult × ATR(14)
          Keeps stops adaptive — avoids the "2% stop on $90k BTC" problem

  Exit:   Close falls back below the Donchian midpoint
          (midpoint = average of highest-high and lowest-low over window)
          — the breakout has failed/exhausted

  min_hold_bars: protects against exiting the very bar after entry
  take_profit_pct: fixed percentage TP as a backstop

Key concept: Donchian channel
  The "channel" is simply the highest high and lowest low over the last
  N bars. When price pokes above the N-bar high, that's a new momentum
  breakout. We buy that. We exit when price retreats to the midpoint
  of the channel, meaning the breakout has stalled.

Compatible with HyperoptRunner using CRYPTO_BREAKOUT_SPACE from hyperopt_runner.py.
"""

import os

import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class CryptoBreakoutStrategy(BaseStrategy):

    name = "crypto_breakout"

    def __init__(
        self,
        channel_window: int    = 18,       # 18 × 1h lookback
        vol_mult: float        = 1.25,     # volume must be 1.25× SMA to confirm
        atr_window: int        = 14,
        atr_stop_mult: float   = 2.0,      # slightly tighter than trend (2× ATR)
        atr_expand_window: int = 14,       # check ATR vs its own SMA for expansion
        min_hold_bars: int     = 3,        # don't exit right after entry
        risk_pct: float        = 0.01,
        stop_loss_pct: float   = 0.05,     # 5% fallback when ATR unavailable
        take_profit_pct: float = 0.12,     # 12% TP backstop
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.channel_window    = channel_window
        self.vol_mult          = vol_mult
        self.atr_window        = atr_window
        self.atr_stop_mult     = atr_stop_mult
        self.atr_expand_window = atr_expand_window
        self.min_hold_bars     = min_hold_bars

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── Donchian channel ──────────────────────────────────────────────────
        # Upper channel: highest high over last channel_window bars (shifted 1 to avoid lookahead)
        df["donch_high"] = df["high"].shift(1).rolling(self.channel_window).max()
        df["donch_low"]  = df["low"].shift(1).rolling(self.channel_window).min()
        df["donch_mid"]  = (df["donch_high"] + df["donch_low"]) / 2

        # ── Volume SMA ────────────────────────────────────────────────────────
        df["volume_sma"] = df["volume"].rolling(20).mean()

        # ── ATR + ATR SMA (to detect volatility expansion) ───────────────────
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_window)
        df["atr"] = atr if atr is not None else float("nan")
        df["atr_sma"] = df["atr"].rolling(self.atr_expand_window).mean()

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = float("nan")
        df["take_profit_price"] = float("nan")

        required = ["donch_high", "donch_low", "donch_mid", "volume_sma", "atr"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns: {missing}. Call populate_indicators() first.")

        valid = df[required].notna().all(axis=1)

        # ── Entry: close breaks above Donchian high + volume + ATR expansion ─
        entry = (
            df["close"].gt(df["donch_high"])
            & df["volume"].gt(df["volume_sma"] * self.vol_mult)
            & df["atr"].gt(df["atr_sma"])   # volatility is expanding
            & valid
        )

        # ── Exit: close retreats to or below the channel midpoint ────────────
        base_exit = df["close"].lt(df["donch_mid"]) & valid

        # Vectorized min_hold_bars protection (same pattern as mean_reversion.py)
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

        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            breakout_dist = (df.loc[entry, "close"] - df.loc[entry, "donch_high"]) / df.loc[entry, "close"]
            breakout_score = (breakout_dist / 0.03).clip(0.0, 1.0)
            atr_ratio = df.loc[entry, "atr"] / df.loc[entry, "atr_sma"]
            atr_score = ((atr_ratio - 1.0) / 1.0).clip(0.0, 1.0)
            vol_ratio = df.loc[entry, "volume"] / df.loc[entry, "volume_sma"]
            vol_score = ((vol_ratio - 1.25) / 2.0).clip(0.0, 1.0)
            df.loc[entry, "conviction"] = (
                0.35 * breakout_score + 0.35 * atr_score + 0.30 * vol_score
            ).round(4)

        # ── Stop price: ATR-based or percentage fallback ─────────────────────
        close_at_entry = df.loc[entry, "close"].astype(float)
        if "atr" in df.columns and df.loc[entry, "atr"].notna().any():
            atr_at_entry = df.loc[entry, "atr"].astype(float)
            atr_stop     = (close_at_entry - self.atr_stop_mult * atr_at_entry).round(4)
            pct_stop     = (close_at_entry * (1 - self.stop_loss_pct)).round(4)
            df.loc[entry, "stop_price"] = atr_stop.where(
                atr_at_entry.notna(), pct_stop
            )
        else:
            df.loc[entry, "stop_price"] = (
                close_at_entry * (1 - self.stop_loss_pct)
            ).round(4)

        df.loc[entry, "take_profit_price"] = (
            close_at_entry * (1 + self.take_profit_pct)
        ).round(4)

        return df

    def describe(self) -> dict:
        return {
            "strategy":        self.name,
            "channel_window":  self.channel_window,
            "vol_mult":        self.vol_mult,
            "atr_window":      self.atr_window,
            "atr_stop_mult":   self.atr_stop_mult,
            "min_hold_bars":   self.min_hold_bars,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
