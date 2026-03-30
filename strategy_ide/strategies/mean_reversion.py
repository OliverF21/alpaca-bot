"""
strategies/mean_reversion.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mean Reversion — Bollinger Bands + RSI
Tuned for 15-minute bars.

Key differences from daily version:
  - Tighter stop/TP (15m moves are smaller)
  - exit_target='mid' — don't wait for full band reversion intraday
  - min_hold_bars=2   — allow quick exits but not same-bar flips
  - buy_rsi=32        — 15m RSI overshoots more frequently than daily
"""

import pandas as pd
import pandas_ta as ta
from strategies.base_strategy import BaseStrategy


class MeanReversionStrategy(BaseStrategy):

    name = "mean_reversion"

    def __init__(
        self,
        bb_window: int         = 20,
        bb_std: float          = 2.0,
        rsi_window: int        = 14,
        buy_rsi: int           = 32,
        sell_rsi: int          = 65,
        exit_target: str       = "mid",    # 'mid' suits intraday / 15m
        min_hold_bars: int     = 2,
        risk_pct: float        = 0.02,
        stop_loss_pct: float   = 0.015,    # 1.5% stop for 15m
        take_profit_pct: float = 0.03,     # 3% TP for 15m
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        self.bb_window     = bb_window
        self.bb_std        = bb_std
        self.rsi_window    = rsi_window
        self.buy_rsi       = buy_rsi
        self.sell_rsi      = sell_rsi
        self.exit_target   = exit_target
        self.min_hold_bars = min_hold_bars

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        bb = ta.bbands(df["close"], length=self.bb_window, std=self.bb_std)
        if bb is not None and not bb.empty:
            # BUG 3 FIX: use named columns instead of positional (.iloc[:,0]).
            # pandas_ta naming varies by version:
            #   older : BBL_{window}_{std}
            #   newer : BBL_{window}_{std}_{ddof}
            # We search for columns that START with the expected prefix so the
            # fix is immune to version changes.
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
                # Fallback: positional access (works if the 3 first cols are L/M/U)
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

        df["rsi"]          = ta.rsi(df["close"], length=self.rsi_window)
        df["volume_sma20"] = df["volume"].rolling(20).mean()

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

        entry = (
            df["close"].lt(df["bb_lower"])
            & df["rsi"].lt(self.buy_rsi)
            & df["volume"].gt(df["volume_sma20"])
            & df[required].notna().all(axis=1)
        )

        exit_bb   = (
            df["close"].ge(df["bb_upper"]) if self.exit_target == "upper"
            else df["close"].ge(df["bb_mid"])
        )
        base_exit = exit_bb | df["rsi"].gt(self.sell_rsi)

        # BUG 6 FIX: vectorized min_hold_bars — no nested Python loops.
        # For each entry bar, we mark the next (min_hold_bars-1) bars as
        # "protected", meaning exits are suppressed there.
        # .shift(1) starts protection on the bar AFTER entry (not the entry bar).
        # .rolling().sum() spreads that protection forward N-1 bars.
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

        # BUG 5 FIX: entry wins on the rare bar where both conditions are true.
        # Without this, exit silently overwrites entry and the trade is dropped.
        df.loc[entry, "signal"] = 1
        exit_no_collision = exit_ & ~entry
        df.loc[exit_no_collision, "signal"] = -1
        close_at_entry = df.loc[entry, "close"].astype(float)
        df.loc[entry, "stop_price"]        = (close_at_entry * (1 - self.stop_loss_pct)).round(2)
        df.loc[entry, "take_profit_price"] = (close_at_entry * (1 + self.take_profit_pct)).round(2)

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
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }
