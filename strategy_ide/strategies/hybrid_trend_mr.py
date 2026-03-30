"""
strategies/hybrid_trend_mr.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hybrid: Trend Filter + Mean Reversion
15-minute bars for entry signals, daily 200-SMA for regime filter.

How it works (beginner explanation):
─────────────────────────────────────
Think of a surfer. A good surfer only paddles for a wave when the ocean
is calm and moving in the right direction. They don't try to ride waves
in a storm (downtrend) because even good setups get crushed.

This strategy does two things:

  1. REGIME FILTER (daily 200-SMA — "is the ocean calm?")
     Every day, we check whether the stock's price is above or below its
     200-day moving average. Above = uptrend, the market "tide" is with us.
     Below = downtrend, we stay in cash. No long entries allowed.

     Why 200 days? It's the most widely-watched trend line on Wall Street.
     When NVDA crossed above its 200-SMA in early 2023 (at ~$15), that was
     the signal the bear market was over. Staying out below it would have
     avoided the entire 2022 crash.

  2. ENTRY SIGNAL (15-min Bollinger Bands + RSI — "is there a good wave?")
     When the regime is confirmed (we're in an uptrend), we wait for the
     stock to dip to its lower Bollinger Band while RSI is oversold. That's
     a temporary pullback in an ongoing uptrend — historically the highest-
     probability mean reversion setup.

     The entry fires ONLY when both conditions are true simultaneously:
       • 15-min close < lower BB  AND  RSI < buy_rsi  AND  volume spike
       • Daily close > 200-day SMA  (regime is bullish)

  3. EXIT (same as pure mean reversion)
     Close the trade when price recovers to the midband (or upper band),
     or when RSI reaches the sell threshold. Stop-loss and take-profit
     orders protect against runaway losses.

Compatibility with the live screener:
──────────────────────────────────────
This strategy has all the same attributes as MeanReversionStrategy
(bb_window, bb_std, rsi_window, buy_rsi), so the MeanReversionScreener
pre-filter works unchanged.

The 200-SMA regime is computed by resampling the 15-min bars to daily.
For backtests (years of data): enough bars to compute SMA-200 reliably.
For live scanning: set warmup_bars=5200 in LiveScanner (200 trading days
× 26 bars/day) so the strategy has enough history to compute the regime.
If fewer bars are provided, the regime filter is skipped (neutral — allows
entries), which degrades gracefully to pure mean reversion behaviour.
"""

import pandas as pd
import pandas_ta as ta
from strategies.base_strategy import BaseStrategy


class HybridTrendMRStrategy(BaseStrategy):

    name = "hybrid_trend_mr"

    def __init__(
        self,
        # ── Mean reversion params (same as MeanReversionStrategy) ────────────
        bb_window: int         = 20,
        bb_std: float          = 2.0,
        rsi_window: int        = 14,
        buy_rsi: int           = 32,
        sell_rsi: int          = 65,
        exit_target: str       = "upper",  # 'upper' beats B&H on AMZN, better Sharpe across most stocks
        min_hold_bars: int     = 2,
        stop_loss_pct: float   = 0.015,
        take_profit_pct: float = 0.03,
        risk_pct: float        = 0.02,
        # ── Trend / regime filter ─────────────────────────────────────────────
        trend_sma_window: int  = 200,   # 200-day SMA is the regime benchmark
        trend_buffer_pct: float = 0.01, # price must be > SMA × (1 + buffer)
                                        # to avoid whipsaw right at the SMA line
                                        # e.g. 0.01 = must be 1% above SMA
    ):
        super().__init__(
            risk_pct        = risk_pct,
            stop_loss_pct   = stop_loss_pct,
            take_profit_pct = take_profit_pct,
        )
        # Mean reversion params (kept as public attrs so screener can read them)
        self.bb_window        = bb_window
        self.bb_std           = bb_std
        self.rsi_window       = rsi_window
        self.buy_rsi          = buy_rsi
        self.sell_rsi         = sell_rsi
        self.exit_target      = exit_target
        self.min_hold_bars    = min_hold_bars
        # Trend params
        self.trend_sma_window  = trend_sma_window
        self.trend_buffer_pct  = trend_buffer_pct

    # ── Indicator computation ─────────────────────────────────────────────────

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        # ── Step 1: 15-min Bollinger Bands ────────────────────────────────────
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

        # ── Step 2: 15-min RSI and volume SMA ────────────────────────────────
        df["rsi"]          = ta.rsi(df["close"], length=self.rsi_window)
        df["volume_sma20"] = df["volume"].rolling(20).mean()

        # ── Step 3: Daily regime filter (200-SMA) ────────────────────────────
        # Resample the intraday bars to daily close prices, compute the SMA,
        # then forward-fill the value back to each intraday timestamp.
        #
        # Why forward-fill? Because a "daily" SMA value only becomes known at
        # the end of each trading day. Every 15-min bar within that day should
        # see the PREVIOUS day's SMA (not today's, which isn't finalised yet).
        # Using .shift(1) on the daily series before mapping achieves this —
        # it prevents look-ahead bias.
        df["sma_daily"]  = float("nan")
        df["in_uptrend"] = True   # default: allow entries when regime unknown

        try:
            daily = (
                df["close"]
                .resample("1D")
                .last()           # daily close = last 15-min bar of the day
                .dropna()
            )
            if len(daily) >= self.trend_sma_window:
                sma_series = daily.rolling(self.trend_sma_window).mean()
                # Shift by 1 day so today's bar uses yesterday's SMA (no look-ahead)
                sma_series_lag = sma_series.shift(1)
                # Map daily SMA back to the 15-min index via forward-fill
                df["sma_daily"] = sma_series_lag.reindex(
                    df.index, method="ffill"
                )
                # in_uptrend = True when close is more than buffer% above the SMA
                df["in_uptrend"] = (
                    df["close"] > df["sma_daily"] * (1 + self.trend_buffer_pct)
                )
            # If fewer than 200 daily bars exist, in_uptrend stays True (neutral)
        except Exception:
            pass   # degrade gracefully — regime filter is skipped

        return df

    # ── Signal generation ─────────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"]            = 0
        df["stop_price"]        = float("nan")
        df["take_profit_price"] = float("nan")

        required = ["bb_upper", "bb_mid", "bb_lower", "rsi", "volume_sma20", "in_uptrend"]
        missing  = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing columns: {missing}. Call populate_indicators() first.")

        # ── Entry: mean reversion conditions AND regime is bullish ────────────
        entry = (
            df["close"].lt(df["bb_lower"])
            & df["rsi"].lt(self.buy_rsi)
            & df["volume"].gt(df["volume_sma20"])
            & df["in_uptrend"]                          # ← THE KEY NEW GATE
            & df[["bb_upper", "bb_mid", "bb_lower", "rsi", "volume_sma20"]].notna().all(axis=1)
        )

        # ── Exit: same as mean reversion ──────────────────────────────────────
        exit_bb = (
            df["close"].ge(df["bb_upper"]) if self.exit_target == "upper"
            else df["close"].ge(df["bb_mid"])
        )
        base_exit = exit_bb | df["rsi"].gt(self.sell_rsi)

        # Vectorized min_hold_bars protection
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

        close_at_entry = df.loc[entry, "close"].astype(float)
        df.loc[entry, "stop_price"]        = (close_at_entry * (1 - self.stop_loss_pct)).round(2)
        df.loc[entry, "take_profit_price"] = (close_at_entry * (1 + self.take_profit_pct)).round(2)

        return df

    # ── Metadata ──────────────────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "strategy":          self.name,
            "bb_window":         self.bb_window,
            "bb_std":            self.bb_std,
            "rsi_window":        self.rsi_window,
            "buy_rsi":           self.buy_rsi,
            "sell_rsi":          self.sell_rsi,
            "exit_target":       self.exit_target,
            "min_hold_bars":     self.min_hold_bars,
            "stop_loss_pct":     self.stop_loss_pct,
            "take_profit_pct":   self.take_profit_pct,
            "trend_sma_window":  self.trend_sma_window,
            "trend_buffer_pct":  self.trend_buffer_pct,
        }
