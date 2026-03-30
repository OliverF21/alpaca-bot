"""
Blank strategy template. Copy and modify for new strategies.
Override populate_indicators() and generate_signals() only.
"""

from typing import Any

import pandas as pd

from strategies.base_strategy import BaseStrategy


class TemplateStrategy(BaseStrategy):
    """
    Template: add your indicators and signal logic below.
    - populate_indicators: add columns (e.g. from indicators.base)
    - generate_signals: set df['signal'] to 1 (enter), -1 (exit), 0 (hold)
    """

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        # Example: add RSI, BB, etc.
        # from strategy_ide.indicators.base import rsi, bollinger_bands
        # df = rsi(df, length=14)
        # df = bollinger_bands(df, length=20, std=2.0)
        return df.copy()

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        # Example: set signal from your logic
        # df = df.copy()
        # df['signal'] = 0
        # df.loc[entry_condition, 'signal'] = 1
        # df.loc[exit_condition, 'signal'] = -1
        df = df.copy()
        df["signal"] = 0
        return df
