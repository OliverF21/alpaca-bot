"""
strategies/crypto_trend_following.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EMA crossover + ADX trend strength + ATR trailing stop, tuned for
crypto 1-hour bars and 24/7 markets.

When it outperforms mean reversion:
  During extended directional moves — BTC bull runs, altcoin pumps,
  flash crashes. Mean reversion loses badly in trending markets;
  this strategy rides the trend until it reverses.

Strategy logic:
  Entry:  fast EMA crosses above slow EMA
          AND ADX > adx_threshold (trend has real strength, not noise)
          AND volume > SMA (conviction behind the move)

  Stop:   ATR-based trailing stop set at entry close − atr_stop_mult × ATR(14)
          Crypto needs wider stops than equity (more volatility).

  Exit:   fast EMA crosses below slow EMA (trend reversed)
          OR ATR trailing stop triggered (backtester handles stop_price column)

Key differences from TrendFollowingStrategy (equity daily):
  - 24/7: no market hours guard
  - ATR-based stop (not fixed %) — self-calibrating across volatility regimes
  - Wider stop_loss_pct fallback (5% vs 7% daily, since 1h bars need tighter)
  - adx_threshold=20 default (crypto is noisier; equity uses 15)
  - atr_trail_mult=3.0 — give 3× ATR breathing room for crypto whipsaws

Compatible with HyperoptRunner using CRYPTO_TREND_SPACE from hyperopt_runner.py.
"""

import os

import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class CryptoTrendFollowingStrategy(BaseStrategy):

    name = "crypto_trend_following"

    def __init__(
        self,
        fast_ema: int          = 12,
        slow_ema: int          = 26,
        adx_window: int        = 14,
        adx_threshold: float   = 20.0,     # higher than equity — crypto is noisier
        vol_window: int        = 20,
        atr_window: int        = 14,
        atr_stop_mult: float   = 3.0,      # wider than MR (3× ATR) — trend needs room
        risk_pct: float        = 0.01,
        stop_loss_pct: float   = 0.05,     # 5% fallback when ATR unavailable
        take_profit_pct: float = 0.15,     # 15% TP — let trend winners run
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
        self.atr_window    = atr_window
        self.atr_stop_mult = atr_stop_mult

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── EMA crossover ─────────────────────────────────────────────────────
        df["ema_fast"] = ta.ema(df["close"], length=self.fast_ema)
        df["ema_slow"] = ta.ema(df["close"], length=self.slow_ema)

        ema_diff       = df["ema_fast"] - df["ema_slow"]
        df["cross_up"] = (ema_diff > 0) & (ema_diff.shift(1) <= 0)
        df["cross_down"] = (ema_diff < 0) & (ema_diff.shift(1) >= 0)

        # ── ADX — trend strength ───────────────────────────────────────────────
        adx = ta.adx(df["high"], df["low"], df["close"], length=self.adx_window)
        if adx is not None and not adx.empty:
            adx_col   = next((c for c in adx.columns if c.startswith("ADX_")), None)
            df["adx"] = adx[adx_col] if adx_col else float("nan")
        else:
            df["adx"] = float("nan")

        # ── Volume SMA ────────────────────────────────────────────────────────
        df["volume_sma"] = df["volume"].rolling(self.vol_window).mean()

        # ── ATR — adaptive stop sizing ────────────────────────────────────────
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_window)
        df["atr"] = atr if atr is not None else float("nan")

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

        # ── Entry: EMA cross up + trend confirmed by ADX + volume spike ───────
        entry = (
            df["cross_up"]
            & df["adx"].gt(self.adx_threshold)
            & df["volume"].gt(df["volume_sma"])
            & valid
        )

        # ── Exit: EMA cross down (trend reversal confirmed) ───────────────────
        exit_ = df["cross_down"] & valid

        # Entry wins on same-bar collision
        df.loc[entry, "signal"] = 1
        df.loc[exit_ & ~entry, "signal"] = -1

        # ── Stop price: ATR-based (preferred) or percentage fallback ─────────
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
            "fast_ema":        self.fast_ema,
            "slow_ema":        self.slow_ema,
            "adx_window":      self.adx_window,
            "adx_threshold":   self.adx_threshold,
            "atr_window":      self.atr_window,
            "atr_stop_mult":   self.atr_stop_mult,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
