"""
strategies/crypto_mean_reversion.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mean Reversion strategy tuned for crypto (1-hour bars, 24/7 markets).

Key differences from MeanReversionStrategy (equity 15-min):
  - ATR-based adaptive stop: stop_price = entry - atr_stop_mult * ATR(14)
    Self-calibrating — avoids getting stopped out on normal crypto volatility.
  - Wider defaults: RSI 28/68, stop 4%, TP 8% (crypto moves 3-5× faster)
  - Volume filter: 1.3× SMA20 (24/7 volume inflates average; need a real spike)
  - exit_target='mid' (crypto reverses sharply — don't wait for upper band)
  - Works with the existing backtester: stop_price column is read automatically
    by fixed_risk_shares() in the engine.

Compatible with HyperoptRunner using CRYPTO_MR_SPACE from hyperopt_runner.py.
"""

import os

import pandas as pd
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class CryptoMeanReversionStrategy(BaseStrategy):

    name = "crypto_mean_reversion"

    def __init__(
        self,
        bb_window: int         = 20,
        bb_std: float          = 2.0,
        rsi_window: int        = 14,
        buy_rsi: int           = 33,       # deeper oversold vs equity's 32
        sell_rsi: int          = 68,       # higher exit vs equity's 65
        exit_target: str       = "mid",    # crypto snaps back fast — don't wait for upper
        min_hold_bars: int     = 2,
        atr_window: int        = 14,
        atr_stop_mult: float   = 2.5,      # stop = entry - 2.5 * ATR
        risk_pct: float        = 0.01,     # 1% risk per trade
        stop_loss_pct: float   = 0.04,     # 4% fallback when ATR unavailable
        take_profit_pct: float = 0.08,     # 8% TP (wider for crypto moves)
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.bb_window      = bb_window
        self.bb_std         = bb_std
        self.rsi_window     = rsi_window
        self.buy_rsi        = buy_rsi
        self.sell_rsi       = sell_rsi
        self.exit_target    = exit_target
        self.min_hold_bars  = min_hold_bars
        self.atr_window     = atr_window
        self.atr_stop_mult  = atr_stop_mult

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── Bollinger Bands (same version-safe prefix search as mean_reversion.py) ──
        bb = ta.bbands(df["close"], length=self.bb_window, std=self.bb_std)
        if bb is not None and not bb.empty:
            prefix_l = f"BBL_{self.bb_window}_"
            prefix_m = f"BBM_{self.bb_window}_"
            prefix_u = f"BBU_{self.bb_window}_"
            cols     = bb.columns.tolist()
            col_l = next((c for c in cols if c.startswith(prefix_l)), None)
            col_m = next((c for c in cols if c.startswith(prefix_m)), None)
            col_u = next((c for c in cols if c.startswith(prefix_u)), None)
            if col_l and col_m and col_u:
                df["bb_lower"] = bb[col_l]
                df["bb_mid"]   = bb[col_m]
                df["bb_upper"] = bb[col_u]
            else:
                df["bb_lower"] = bb.iloc[:, 0]
                df["bb_mid"]   = bb.iloc[:, 1]
                df["bb_upper"] = bb.iloc[:, 2]
            band_width     = df["bb_upper"] - df["bb_lower"]
            df["bb_pct_b"] = (
                (df["close"] - df["bb_lower"])
                / band_width.replace(0, float("nan"))
            )
        else:
            df[["bb_upper", "bb_mid", "bb_lower", "bb_pct_b"]] = float("nan")

        # ── RSI ────────────────────────────────────────────────────────────────
        df["rsi"] = ta.rsi(df["close"], length=self.rsi_window)

        # ── Volume SMA (24/7 volume inflates average — use 1.3× filter) ───────
        df["volume_sma20"] = df["volume"].rolling(20).mean()

        # ── ATR — key for crypto adaptive stop sizing ─────────────────────────
        atr = ta.atr(df["high"], df["low"], df["close"], length=self.atr_window)
        df["atr"] = atr if atr is not None else float("nan")

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = float("nan")
        df["take_profit_price"] = float("nan")

        required = ["bb_upper", "bb_mid", "bb_lower", "rsi", "volume_sma20"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns: {missing}. Call populate_indicators() first.")

        # Volume confirmation: 1.15× threshold for crypto (24/7 average is inflated)
        bb_band_width = df["bb_upper"] - df["bb_lower"]
        bb_threshold = df["bb_lower"] + 0.25 * bb_band_width
        entry = (
            df["close"].lt(bb_threshold)
            & df["rsi"].lt(self.buy_rsi)
            & df["volume"].gt(df["volume_sma20"] * 1.0)
            & df[required].notna().all(axis=1)
        )

        exit_bb   = (
            df["close"].ge(df["bb_upper"]) if self.exit_target == "upper"
            else df["close"].ge(df["bb_mid"])
        )
        base_exit = exit_bb | df["rsi"].gt(self.sell_rsi)

        # Vectorized min_hold_bars protection (identical to mean_reversion.py)
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

        # Entry wins on collision (identical pattern to mean_reversion.py)
        df.loc[entry, "signal"] = 1
        exit_no_collision = exit_ & ~entry
        df.loc[exit_no_collision, "signal"] = -1

        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            rsi_score = ((self.buy_rsi - df.loc[entry, "rsi"]).clip(lower=0) / self.buy_rsi).clip(0.0, 1.0)
            bb_score = (1.0 - df.loc[entry, "bb_pct_b"]).clip(0.0, 1.0)
            vol_ratio = df.loc[entry, "volume"] / df.loc[entry, "volume_sma20"]
            vol_score = (vol_ratio / 2.0).clip(0.0, 1.0)
            df.loc[entry, "conviction"] = (
                0.4 * rsi_score + 0.35 * bb_score + 0.25 * vol_score
            ).clip(lower=0.15).round(4)

        # Stop price: wider of ATR-based stop and pct-based floor
        close_at_entry = df.loc[entry, "close"].astype(float)
        pct_stop = (close_at_entry * (1 - self.stop_loss_pct)).round(2)
        if "atr" in df.columns and df.loc[entry, "atr"].notna().any():
            atr_at_entry   = df.loc[entry, "atr"].astype(float)
            atr_stop       = (close_at_entry - self.atr_stop_mult * atr_at_entry).round(2)
            df.loc[entry, "stop_price"] = pd.DataFrame(
                {"atr": atr_stop.where(atr_at_entry.notna(), pct_stop), "pct": pct_stop}
            ).min(axis=1).round(2)
        else:
            df.loc[entry, "stop_price"] = pct_stop

        df.loc[entry, "take_profit_price"] = (
            close_at_entry * (1 + self.take_profit_pct)
        ).round(2)

        return df

    def describe(self) -> dict:
        return {
            "strategy":        self.name,
            "bb_window":       self.bb_window,
            "bb_std":          self.bb_std,
            "rsi_window":      self.rsi_window,
            "buy_rsi":         self.buy_rsi,
            "sell_rsi":        self.sell_rsi,
            "exit_target":     self.exit_target,
            "min_hold_bars":   self.min_hold_bars,
            "atr_window":      self.atr_window,
            "atr_stop_mult":   self.atr_stop_mult,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
