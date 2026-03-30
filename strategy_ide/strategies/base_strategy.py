"""
strategies/base_strategy.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Abstract base class all strategies must inherit from.

To create a new strategy:
    1. Subclass BaseStrategy
    2. Override populate_indicators(df) → df
    3. Override generate_signals(df)    → df  (adds 'signal' column)
    4. Optionally override describe()   → dict

Example:
    class MyStrategy(BaseStrategy):
        def populate_indicators(self, df): ...
        def generate_signals(self, df):    ...
"""

from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):

    name: str = "base"

    def __init__(
        self,
        risk_pct: float        = 0.02,
        stop_loss_pct: float   = 0.03,
        take_profit_pct: float = 0.06,
    ):
        self.risk_pct          = risk_pct
        self.stop_loss_pct     = stop_loss_pct
        self.take_profit_pct   = take_profit_pct

    @abstractmethod
    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Receive a raw OHLCV DataFrame, return it with indicator columns added.
        Must not modify the original — always work on a copy.
        """
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Receive an indicator-enriched DataFrame (output of populate_indicators).
        Return it with a 'signal' column:
            1  = enter long
           -1  = exit / close long
            0  = hold
        Optionally also set 'stop_price' and 'take_profit_price' columns.
        """
        ...

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convenience method: indicators → signals in one call.
        Returns the fully annotated DataFrame.
        """
        df = self.populate_indicators(df)
        df = self.generate_signals(df)
        return df

    def describe(self) -> dict:
        """Return a dict of strategy name + params. Override to add more detail."""
        return {
            "strategy":        self.name,
            "risk_pct":        self.risk_pct,
            "stop_loss_pct":   self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
        }

    def __repr__(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in self.describe().items())
        return f"{self.__class__.__name__}({params})"
