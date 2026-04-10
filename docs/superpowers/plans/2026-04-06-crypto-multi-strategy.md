# Crypto Multi-Strategy Trading System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-pair AVAX-only crypto scanner with a multi-pair, multi-strategy system that runs all four crypto strategies in parallel, ranks signals by conviction score, and dynamically selects which pairs to trade.

**Architecture:** A Signal Arbitrator collects conviction-scored signals from four reworked strategies running on a dynamically-ranked universe of crypto pairs. Conflicts are resolved by highest conviction; positions scale with account equity. The existing `CryptoScanner` class is refactored to orchestrate this flow.

**Tech Stack:** Python 3.13, pandas, pandas_ta, alpaca-py (TradingClient + CryptoHistoricalDataClient), pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `scanner/crypto_universe.py` | Dynamic universe ranker — scores pairs by volatility + volume |
| Create | `scanner/signal_arbitrator.py` | Collects signals from all strategies, ranks by conviction, resolves conflicts |
| Modify | `strategy_ide/strategies/crypto_trend_following.py` | Add conviction score column, loosen thresholds |
| Modify | `strategy_ide/strategies/crypto_mean_reversion.py` | Add conviction score column, loosen thresholds |
| Modify | `strategy_ide/strategies/crypto_breakout.py` | Add conviction score column, loosen thresholds |
| Modify | `strategy_ide/strategies/crypto_supertrend.py` | Add conviction score column, loosen thresholds |
| Modify | `scanner/crypto_scanner.py` | Refactor to multi-strategy, multi-pair orchestration |
| Modify | `scanner/run_crypto_scanner.py` | Wire new system together, remove AVAX hack |
| Create | `strategy_ide/tests/test_conviction.py` | Unit tests for conviction scoring |
| Create | `strategy_ide/tests/test_arbitrator.py` | Unit tests for signal arbitration |
| Create | `strategy_ide/tests/test_universe.py` | Unit tests for universe ranking |

---

### Task 1: Add Conviction Score to Crypto Trend Following Strategy

**Files:**
- Test: `strategy_ide/tests/test_conviction.py`
- Modify: `strategy_ide/strategies/crypto_trend_following.py`

- [ ] **Step 1: Write the failing test for conviction score**

Create `strategy_ide/tests/test_conviction.py`:

```python
"""
tests/test_conviction.py
━━━━━━━━━━━━━━━━━━━━━━━━
Unit tests for conviction scoring across all four crypto strategies.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
if str(STRATEGY_IDE) not in sys.path:
    sys.path.insert(0, str(STRATEGY_IDE))

import numpy as np
import pandas as pd
import pytest


def make_crypto_ohlcv(n_bars: int = 300, seed: int = 42, freq: str = "1h") -> pd.DataFrame:
    """Generate realistic crypto OHLCV data with enough bars for indicator warmup."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n_bars, freq=freq)
    close = 100 + np.cumsum(rng.normal(0, 1.5, n_bars))
    close = np.maximum(close, 10)  # floor at $10
    high = close + rng.uniform(0.5, 3.0, n_bars)
    low = close - rng.uniform(0.5, 3.0, n_bars)
    volume = rng.uniform(500_000, 5_000_000, n_bars)
    return pd.DataFrame(
        {"open": close + rng.normal(0, 0.5, n_bars), "high": high,
         "low": low, "close": close, "volume": volume},
        index=idx,
    )


class TestTrendFollowingConviction:

    def test_conviction_column_exists_after_generate_signals(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        assert "conviction" in df.columns, "generate_signals must add 'conviction' column"

    def test_conviction_is_zero_when_no_entry(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        no_entry = df[df["signal"] != 1]
        assert (no_entry["conviction"] == 0.0).all(), "Conviction must be 0 when signal != 1"

    def test_conviction_between_zero_and_one_on_entry(self):
        from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
        strat = CryptoTrendFollowingStrategy()
        # Try multiple seeds to find one that generates an entry
        for seed in range(42, 100):
            df = make_crypto_ohlcv(seed=seed)
            df = strat.populate_indicators(df)
            df = strat.generate_signals(df)
            entries = df[df["signal"] == 1]
            if not entries.empty:
                assert entries["conviction"].between(0.0, 1.0).all(), \
                    f"Conviction must be in [0, 1], got {entries['conviction'].values}"
                return
        pytest.skip("No entry signals generated across seeds 42-99")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestTrendFollowingConviction::test_conviction_column_exists_after_generate_signals -v`
Expected: FAIL with `AssertionError: generate_signals must add 'conviction' column`

- [ ] **Step 3: Add conviction score to crypto_trend_following.py**

In `strategy_ide/strategies/crypto_trend_following.py`, modify `generate_signals()`. After line 126 (`df.loc[exit_ & ~entry, "signal"] = -1`), add the conviction score calculation before the stop price section:

```python
        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            # Component 1: ADX strength — how strong is the trend?
            # Normalize: ADX 12 → 0.0, ADX 40+ → 1.0
            adx_score = ((df.loc[entry, "adx"] - 12.0) / 28.0).clip(0.0, 1.0)
            # Component 2: EMA separation — how far apart are the EMAs?
            ema_sep = (df.loc[entry, "ema_fast"] - df.loc[entry, "ema_slow"]).abs()
            ema_pct = ema_sep / df.loc[entry, "close"]
            ema_score = (ema_pct / 0.03).clip(0.0, 1.0)  # 3% separation → 1.0
            # Component 3: Volume ratio
            vol_ratio = df.loc[entry, "volume"] / df.loc[entry, "volume_sma"]
            vol_score = ((vol_ratio - 1.0) / 2.0).clip(0.0, 1.0)  # 3x vol → 1.0
            # Weighted sum
            df.loc[entry, "conviction"] = (
                0.4 * adx_score + 0.35 * ema_score + 0.25 * vol_score
            ).round(4)
```

Also update the default parameters per the spec (line 48-57):
- `fast_ema: int = 12` (already 12)
- `slow_ema: int = 26` (already 26)
- `adx_threshold: float = 12.0` (was 20.0)

And remove the 2-bar confirmation requirement — set it to 1-bar in the scanner later (this strategy doesn't have confirmation built in; it's in the scanner).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestTrendFollowingConviction -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_ide/strategies/crypto_trend_following.py strategy_ide/tests/test_conviction.py
git commit -m "feat: add conviction score to crypto trend following strategy"
```

---

### Task 2: Add Conviction Score to Crypto Mean Reversion Strategy

**Files:**
- Test: `strategy_ide/tests/test_conviction.py` (append)
- Modify: `strategy_ide/strategies/crypto_mean_reversion.py`

- [ ] **Step 1: Write the failing test**

Append to `strategy_ide/tests/test_conviction.py`:

```python
class TestMeanReversionConviction:

    def test_conviction_column_exists(self):
        from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
        strat = CryptoMeanReversionStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        assert "conviction" in df.columns

    def test_conviction_is_zero_when_no_entry(self):
        from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
        strat = CryptoMeanReversionStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        no_entry = df[df["signal"] != 1]
        assert (no_entry["conviction"] == 0.0).all()

    def test_conviction_between_zero_and_one_on_entry(self):
        from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
        strat = CryptoMeanReversionStrategy()
        for seed in range(42, 100):
            df = make_crypto_ohlcv(seed=seed)
            df = strat.populate_indicators(df)
            df = strat.generate_signals(df)
            entries = df[df["signal"] == 1]
            if not entries.empty:
                assert entries["conviction"].between(0.0, 1.0).all()
                return
        pytest.skip("No entry signals generated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestMeanReversionConviction::test_conviction_column_exists -v`
Expected: FAIL

- [ ] **Step 3: Add conviction score to crypto_mean_reversion.py**

In `strategy_ide/strategies/crypto_mean_reversion.py`, in `generate_signals()`, after line 142 (`df.loc[exit_no_collision, "signal"] = -1`), add:

```python
        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            # Component 1: RSI depth — how oversold?
            # RSI 33 → 0.0, RSI 10 → 1.0
            rsi_score = ((33.0 - df.loc[entry, "rsi"]) / 23.0).clip(0.0, 1.0)
            # Component 2: Distance below BB lower band
            bb_dist = (df.loc[entry, "bb_lower"] - df.loc[entry, "close"]) / df.loc[entry, "close"]
            bb_score = (bb_dist / 0.05).clip(0.0, 1.0)  # 5% below → 1.0
            # Component 3: Volume spike magnitude
            vol_ratio = df.loc[entry, "volume"] / df.loc[entry, "volume_sma20"]
            vol_score = ((vol_ratio - 1.15) / 2.0).clip(0.0, 1.0)
            # Weighted sum
            df.loc[entry, "conviction"] = (
                0.4 * rsi_score + 0.35 * bb_score + 0.25 * vol_score
            ).round(4)
```

Also update default parameters per spec:
- `buy_rsi: int = 33` (was 28)
- Line 114-118: Change BB condition from `df["close"].lt(df["bb_lower"])` to:

```python
        # Close within 10% of lower band (wider catch zone)
        bb_band_width = df["bb_upper"] - df["bb_lower"]
        bb_threshold = df["bb_lower"] + 0.10 * bb_band_width
        entry = (
            df["close"].lt(bb_threshold)
            & df["rsi"].lt(self.buy_rsi)
            & df["volume"].gt(df["volume_sma20"] * 1.15)
            & df[required].notna().all(axis=1)
        )
```

Note the volume multiplier changed from `1.3` to `1.15`.

- [ ] **Step 4: Run tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestMeanReversionConviction -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_ide/strategies/crypto_mean_reversion.py strategy_ide/tests/test_conviction.py
git commit -m "feat: add conviction score to crypto mean reversion strategy"
```

---

### Task 3: Add Conviction Score to Crypto Breakout Strategy

**Files:**
- Test: `strategy_ide/tests/test_conviction.py` (append)
- Modify: `strategy_ide/strategies/crypto_breakout.py`

- [ ] **Step 1: Write the failing test**

Append to `strategy_ide/tests/test_conviction.py`:

```python
class TestBreakoutConviction:

    def test_conviction_column_exists(self):
        from strategies.crypto_breakout import CryptoBreakoutStrategy
        strat = CryptoBreakoutStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        assert "conviction" in df.columns

    def test_conviction_is_zero_when_no_entry(self):
        from strategies.crypto_breakout import CryptoBreakoutStrategy
        strat = CryptoBreakoutStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        no_entry = df[df["signal"] != 1]
        assert (no_entry["conviction"] == 0.0).all()

    def test_conviction_between_zero_and_one_on_entry(self):
        from strategies.crypto_breakout import CryptoBreakoutStrategy
        strat = CryptoBreakoutStrategy()
        for seed in range(42, 100):
            df = make_crypto_ohlcv(seed=seed)
            df = strat.populate_indicators(df)
            df = strat.generate_signals(df)
            entries = df[df["signal"] == 1]
            if not entries.empty:
                assert entries["conviction"].between(0.0, 1.0).all()
                return
        pytest.skip("No entry signals generated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestBreakoutConviction::test_conviction_column_exists -v`
Expected: FAIL

- [ ] **Step 3: Add conviction score to crypto_breakout.py**

In `strategy_ide/strategies/crypto_breakout.py`, in `generate_signals()`, after line 130 (`df.loc[exit_ & ~entry, "signal"] = -1`), add:

```python
        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            # Component 1: Breakout distance above Donchian high
            breakout_dist = (df.loc[entry, "close"] - df.loc[entry, "donch_high"]) / df.loc[entry, "close"]
            breakout_score = (breakout_dist / 0.03).clip(0.0, 1.0)  # 3% breakout → 1.0
            # Component 2: ATR expansion ratio
            atr_ratio = df.loc[entry, "atr"] / df.loc[entry, "atr_sma"]
            atr_score = ((atr_ratio - 1.0) / 1.0).clip(0.0, 1.0)  # 2x expansion → 1.0
            # Component 3: Volume spike
            vol_ratio = df.loc[entry, "volume"] / df.loc[entry, "volume_sma"]
            vol_score = ((vol_ratio - 1.25) / 2.0).clip(0.0, 1.0)
            # Weighted sum
            df.loc[entry, "conviction"] = (
                0.35 * breakout_score + 0.35 * atr_score + 0.30 * vol_score
            ).round(4)
```

Also update default parameters per spec:
- `channel_window: int = 18` (was 24)
- `vol_mult: float = 1.25` (was 1.5)

- [ ] **Step 4: Run tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestBreakoutConviction -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_ide/strategies/crypto_breakout.py strategy_ide/tests/test_conviction.py
git commit -m "feat: add conviction score to crypto breakout strategy"
```

---

### Task 4: Add Conviction Score to Crypto Supertrend Strategy

**Files:**
- Test: `strategy_ide/tests/test_conviction.py` (append)
- Modify: `strategy_ide/strategies/crypto_supertrend.py`

- [ ] **Step 1: Write the failing test**

Append to `strategy_ide/tests/test_conviction.py`:

```python
class TestSupertrendConviction:

    def test_conviction_column_exists(self):
        from strategies.crypto_supertrend import CryptoSupertrendStrategy
        strat = CryptoSupertrendStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        assert "conviction" in df.columns

    def test_conviction_is_zero_when_no_entry(self):
        from strategies.crypto_supertrend import CryptoSupertrendStrategy
        strat = CryptoSupertrendStrategy()
        df = make_crypto_ohlcv()
        df = strat.populate_indicators(df)
        df = strat.generate_signals(df)
        no_entry = df[df["signal"] != 1]
        assert (no_entry["conviction"] == 0.0).all()

    def test_conviction_between_zero_and_one_on_entry(self):
        from strategies.crypto_supertrend import CryptoSupertrendStrategy
        strat = CryptoSupertrendStrategy()
        for seed in range(42, 100):
            df = make_crypto_ohlcv(seed=seed)
            df = strat.populate_indicators(df)
            df = strat.generate_signals(df)
            entries = df[df["signal"] == 1]
            if not entries.empty:
                assert entries["conviction"].between(0.0, 1.0).all()
                return
        pytest.skip("No entry signals generated")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestSupertrendConviction::test_conviction_column_exists -v`
Expected: FAIL

- [ ] **Step 3: Add conviction score to crypto_supertrend.py**

In `strategy_ide/strategies/crypto_supertrend.py`, in `generate_signals()`, after line 169 (`df.loc[exit_ & ~entry, "signal"] = -1`), add:

```python
        # ── Conviction score (0.0–1.0) for Signal Arbitrator ─────────────
        df["conviction"] = 0.0
        if entry.any():
            # Component 1: Distance from Supertrend line (how far above support?)
            st_dist = (df.loc[entry, "close"] - df.loc[entry, "supertrend"]) / df.loc[entry, "close"]
            st_score = (st_dist / 0.05).clip(0.0, 1.0)  # 5% above ST → 1.0
            # Component 2: RSI momentum
            rsi_score = ((df.loc[entry, "rsi"] - 40.0) / 30.0).clip(0.0, 1.0)  # RSI 70 → 1.0
            # Component 3: Trend duration — bars since flip (longer = more conviction)
            # Count consecutive bars where supertrend_dir == 1
            dir_series = df["supertrend_dir"]
            flip_points = (dir_series != dir_series.shift(1)).cumsum()
            bars_since_flip = dir_series.groupby(flip_points).cumcount()
            duration_score = (bars_since_flip.loc[entry] / 10.0).clip(0.0, 1.0)
            # Weighted sum
            df.loc[entry, "conviction"] = (
                0.4 * st_score + 0.35 * rsi_score + 0.25 * duration_score
            ).round(4)
```

Also update default parameters per spec:
- `multiplier: float = 2.5` (was 3.0)
- `rsi_min: float = 40.0` (was 45.0)
- `vol_filter: bool = False` (was True)

- [ ] **Step 4: Run tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py::TestSupertrendConviction -v`
Expected: 3 tests PASS

- [ ] **Step 5: Run ALL conviction tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_conviction.py -v`
Expected: All 12 tests PASS

- [ ] **Step 6: Commit**

```bash
git add strategy_ide/strategies/crypto_supertrend.py strategy_ide/tests/test_conviction.py
git commit -m "feat: add conviction score to crypto supertrend strategy"
```

---

### Task 5: Create Dynamic Universe Ranker

**Files:**
- Create: `strategy_ide/tests/test_universe.py`
- Create: `scanner/crypto_universe.py`

- [ ] **Step 1: Write the failing tests**

Create `strategy_ide/tests/test_universe.py`:

```python
"""
tests/test_universe.py
━━━━━━━━━━━━━━━━━━━━━━
Unit tests for Dynamic Universe Ranker.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
REPO_ROOT = STRATEGY_IDE.parent
for p in [str(STRATEGY_IDE), str(REPO_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np
import pandas as pd
import pytest

from scanner.crypto_universe import rank_universe, FALLBACK_PAIRS


def make_pair_data(n_bars: int = 168, atr_pct: float = 0.03,
                   avg_volume: float = 1_000_000, seed: int = 42) -> pd.DataFrame:
    """Generate OHLCV data with controllable volatility and volume."""
    rng = np.random.default_rng(seed)
    base_price = 100.0
    close = base_price + np.cumsum(rng.normal(0, base_price * atr_pct * 0.1, n_bars))
    close = np.maximum(close, 10)
    high = close * (1 + rng.uniform(0, atr_pct, n_bars))
    low = close * (1 - rng.uniform(0, atr_pct, n_bars))
    volume = rng.uniform(avg_volume * 0.5, avg_volume * 1.5, n_bars)
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.date_range("2024-01-01", periods=n_bars, freq="1h"),
    )


class TestRankUniverse:

    def test_returns_list_of_strings(self):
        bars = {
            "BTC/USD": make_pair_data(atr_pct=0.04, avg_volume=5_000_000, seed=1),
            "ETH/USD": make_pair_data(atr_pct=0.03, avg_volume=3_000_000, seed=2),
            "DOGE/USD": make_pair_data(atr_pct=0.05, avg_volume=1_000_000, seed=3),
        }
        result = rank_universe(bars, top_k=2)
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)
        assert len(result) == 2

    def test_higher_volatility_ranks_higher(self):
        bars = {
            "HIGH_VOL/USD": make_pair_data(atr_pct=0.08, avg_volume=2_000_000, seed=1),
            "LOW_VOL/USD": make_pair_data(atr_pct=0.01, avg_volume=2_000_000, seed=2),
        }
        result = rank_universe(bars, top_k=2)
        assert result[0] == "HIGH_VOL/USD", "Higher volatility pair should rank first"

    def test_filters_low_volume_pairs(self):
        bars = {
            "GOOD/USD": make_pair_data(atr_pct=0.03, avg_volume=500_000, seed=1),
            "DEAD/USD": make_pair_data(atr_pct=0.03, avg_volume=1_000, seed=2),
        }
        result = rank_universe(bars, top_k=2, min_daily_dollar_volume=50_000)
        assert "DEAD/USD" not in result

    def test_respects_top_k(self):
        bars = {f"PAIR{i}/USD": make_pair_data(seed=i) for i in range(10)}
        result = rank_universe(bars, top_k=5)
        assert len(result) <= 5

    def test_empty_input_returns_empty(self):
        result = rank_universe({}, top_k=8)
        assert result == []

    def test_fallback_pairs_are_defined(self):
        assert len(FALLBACK_PAIRS) == 6
        assert "BTC/USD" in FALLBACK_PAIRS


class TestMinThresholds:

    def test_atr_pct_filter(self):
        bars = {
            "VOLATILE/USD": make_pair_data(atr_pct=0.05, avg_volume=500_000, seed=1),
            "FLAT/USD": make_pair_data(atr_pct=0.001, avg_volume=500_000, seed=2),
        }
        result = rank_universe(bars, top_k=2, min_atr_pct=0.01)
        assert "FLAT/USD" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_universe.py::TestRankUniverse::test_returns_list_of_strings -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scanner.crypto_universe'`

- [ ] **Step 3: Create scanner/crypto_universe.py**

Create `scanner/crypto_universe.py`:

```python
"""
scanner/crypto_universe.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
Dynamic Universe Ranker for crypto multi-strategy scanner.

Scores all available crypto pairs by trailing volatility (ATR%) and dollar
volume, returning the top K pairs worth scanning. Replaces the hardcoded
12-pair watchlist.

Runs in a background thread, refreshing every 30 minutes.
"""

import logging
import os
import sys
import threading
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
import pandas_ta as ta

_REPO = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

log = logging.getLogger(__name__)

# If the ranker fails entirely, fall back to these liquid majors
FALLBACK_PAIRS: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOGE/USD",
]

# All pairs available on Alpaca
ALL_CRYPTO_PAIRS: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AAVE/USD", "AVAX/USD",
    "DOGE/USD", "LTC/USD", "UNI/USD", "DOT/USD", "MATIC/USD", "XRP/USD",
]


def rank_universe(
    bars_by_symbol: Dict[str, pd.DataFrame],
    top_k: int = 8,
    min_daily_dollar_volume: float = 50_000.0,
    min_atr_pct: float = 0.01,
) -> List[str]:
    """
    Score and rank crypto pairs by volatility + volume.

    Parameters
    ----------
    bars_by_symbol : dict mapping symbol → OHLCV DataFrame (168 bars of 1h data)
    top_k : number of top pairs to return
    min_daily_dollar_volume : minimum average daily dollar volume to qualify
    min_atr_pct : minimum ATR as percentage of price to qualify

    Returns
    -------
    List of symbol strings, ranked best-first, length <= top_k.
    """
    if not bars_by_symbol:
        return []

    scores = []
    for symbol, df in bars_by_symbol.items():
        if df.empty or len(df) < 20:
            continue

        # ATR(14) as percentage of price
        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        if atr is None or atr.dropna().empty:
            continue
        latest_atr = float(atr.dropna().iloc[-1])
        latest_close = float(df["close"].iloc[-1])
        atr_pct = latest_atr / latest_close if latest_close > 0 else 0

        # Dollar volume SMA(20)
        dollar_vol = (df["close"] * df["volume"]).rolling(20).mean()
        if dollar_vol.dropna().empty:
            continue
        avg_daily_dollar_vol = float(dollar_vol.dropna().iloc[-1]) * 24  # hourly → daily

        # Apply minimum thresholds
        if avg_daily_dollar_vol < min_daily_dollar_volume:
            continue
        if atr_pct < min_atr_pct:
            continue

        scores.append({
            "symbol": symbol,
            "atr_pct": atr_pct,
            "dollar_vol": avg_daily_dollar_vol,
        })

    if not scores:
        return []

    scores_df = pd.DataFrame(scores)

    # Percentile rank each factor (0 to 1)
    scores_df["vol_rank"] = scores_df["atr_pct"].rank(pct=True)
    scores_df["dvol_rank"] = scores_df["dollar_vol"].rank(pct=True)

    # Composite: 60% volatility, 40% volume
    scores_df["composite"] = 0.6 * scores_df["vol_rank"] + 0.4 * scores_df["dvol_rank"]

    # Sort descending and return top K symbols
    scores_df = scores_df.sort_values("composite", ascending=False)
    return scores_df["symbol"].head(top_k).tolist()


class UniverseRanker:
    """
    Background thread that refreshes the active crypto universe every N seconds.

    Usage:
        ranker = UniverseRanker(refresh_interval=1800, top_k=8)
        ranker.start()
        # ... later ...
        pairs = ranker.get_universe()  # thread-safe
    """

    def __init__(
        self,
        refresh_interval: int = 1800,  # 30 minutes
        top_k: int = 8,
        pairs: Optional[List[str]] = None,
    ):
        self.refresh_interval = refresh_interval
        self.top_k = top_k
        self._all_pairs = pairs or ALL_CRYPTO_PAIRS
        self._universe: List[str] = list(FALLBACK_PAIRS)  # start with fallback
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def get_universe(self) -> List[str]:
        """Thread-safe access to current universe."""
        with self._lock:
            return list(self._universe)

    def _refresh(self):
        """Fetch bars for all pairs and re-rank."""
        try:
            from data.crypto_fetcher import fetch_crypto_bars_bulk
        except ImportError:
            from strategy_ide.data.crypto_fetcher import fetch_crypto_bars_bulk

        try:
            bars = fetch_crypto_bars_bulk(
                self._all_pairs, resolution="60", n_bars=168
            )
            ranked = rank_universe(bars, top_k=self.top_k)
            if ranked:
                with self._lock:
                    self._universe = ranked
                log.info(f"Universe refreshed: {ranked}")
            else:
                log.warning("Universe ranker returned empty — keeping previous universe")
        except Exception as e:
            log.error(f"Universe refresh failed: {e} — keeping previous universe")

    def _loop(self):
        while True:
            self._refresh()
            time.sleep(self.refresh_interval)

    def start(self):
        """Start background refresh thread."""
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="universe-ranker"
        )
        self._thread.start()
        log.info(
            f"Universe ranker started (refresh every {self.refresh_interval}s, top {self.top_k})"
        )

    def refresh_now(self):
        """Force an immediate refresh (blocking). Useful at startup."""
        self._refresh()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_universe.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scanner/crypto_universe.py strategy_ide/tests/test_universe.py
git commit -m "feat: add dynamic universe ranker for crypto pair selection"
```

---

### Task 6: Create Signal Arbitrator

**Files:**
- Create: `strategy_ide/tests/test_arbitrator.py`
- Create: `scanner/signal_arbitrator.py`

- [ ] **Step 1: Write the failing tests**

Create `strategy_ide/tests/test_arbitrator.py`:

```python
"""
tests/test_arbitrator.py
━━━━━━━━━━━━━━━━━━━━━━━━
Unit tests for Signal Arbitrator.
"""

import sys
from pathlib import Path

STRATEGY_IDE = Path(__file__).resolve().parent.parent
REPO_ROOT = STRATEGY_IDE.parent
for p in [str(STRATEGY_IDE), str(REPO_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest
from scanner.signal_arbitrator import SignalArbitrator


class TestConflictResolution:

    def test_highest_conviction_wins(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.8,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.5,
             "strategy": "crypto_mean_reversion", "stop_price": 96.0,
             "take_profit_price": 108.0, "entry_price": 100.0},
        ]
        result = arb.arbitrate(signals, held_positions=set(), cooldowns={})
        entries = [r for r in result if r["action"] == "enter"]
        assert len(entries) == 1
        assert entries[0]["strategy"] == "crypto_trend_following"

    def test_exit_beats_enter_on_same_pair(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "ETH/USD", "signal": "enter", "conviction": 0.9,
             "strategy": "crypto_breakout", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
            {"symbol": "ETH/USD", "signal": "exit", "conviction": 0.3,
             "strategy": "crypto_supertrend", "stop_price": 0, "take_profit_price": 0,
             "entry_price": 0},
        ]
        result = arb.arbitrate(signals, held_positions={"ETH/USD"}, cooldowns={})
        assert any(r["action"] == "exit" and r["symbol"] == "ETH/USD" for r in result)
        assert not any(r["action"] == "enter" and r["symbol"] == "ETH/USD" for r in result)

    def test_tiebreaker_prefers_momentum_strategy(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "SOL/USD", "signal": "enter", "conviction": 0.6,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
            {"symbol": "SOL/USD", "signal": "enter", "conviction": 0.6,
             "strategy": "crypto_mean_reversion", "stop_price": 96.0,
             "take_profit_price": 108.0, "entry_price": 100.0},
        ]
        result = arb.arbitrate(signals, held_positions=set(), cooldowns={})
        entries = [r for r in result if r["action"] == "enter"]
        assert entries[0]["strategy"] == "crypto_trend_following"


class TestPositionLimit:

    def test_respects_max_positions(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": f"PAIR{i}/USD", "signal": "enter", "conviction": 0.8 - i*0.1,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0}
            for i in range(5)
        ]
        result = arb.arbitrate(signals, held_positions=set(), cooldowns={})
        entries = [r for r in result if r["action"] == "enter"]
        max_pos = arb.max_positions
        assert len(entries) <= max_pos

    def test_dynamic_position_limit_scales_with_equity(self):
        arb_small = SignalArbitrator(account_equity=5_000)
        arb_large = SignalArbitrator(account_equity=30_000)
        assert arb_small.max_positions < arb_large.max_positions

    def test_minimum_one_position(self):
        arb = SignalArbitrator(account_equity=1_000)
        assert arb.max_positions >= 1

    def test_maximum_six_positions(self):
        arb = SignalArbitrator(account_equity=1_000_000)
        assert arb.max_positions <= 6


class TestCooldown:

    def test_cooldown_prevents_reentry(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.9,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
        ]
        # BTC/USD exited 1 bar ago (cooldown = 3 bars)
        cooldowns = {"BTC/USD": 1}
        result = arb.arbitrate(signals, held_positions=set(), cooldowns=cooldowns)
        entries = [r for r in result if r["action"] == "enter"]
        assert len(entries) == 0

    def test_cooldown_expired_allows_entry(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.9,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
        ]
        cooldowns = {"BTC/USD": 4}  # 4 bars ago > 3 bar cooldown
        result = arb.arbitrate(signals, held_positions=set(), cooldowns=cooldowns)
        entries = [r for r in result if r["action"] == "enter"]
        assert len(entries) == 1


class TestRiskTiering:

    def test_high_conviction_gets_higher_risk(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.8,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
        ]
        result = arb.arbitrate(signals, held_positions=set(), cooldowns={})
        assert result[0]["risk_pct"] == 0.02

    def test_low_conviction_gets_standard_risk(self):
        arb = SignalArbitrator(account_equity=10_000)
        signals = [
            {"symbol": "BTC/USD", "signal": "enter", "conviction": 0.5,
             "strategy": "crypto_trend_following", "stop_price": 95.0,
             "take_profit_price": 115.0, "entry_price": 100.0},
        ]
        result = arb.arbitrate(signals, held_positions=set(), cooldowns={})
        assert result[0]["risk_pct"] == 0.01
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_arbitrator.py::TestConflictResolution::test_highest_conviction_wins -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scanner.signal_arbitrator'`

- [ ] **Step 3: Create scanner/signal_arbitrator.py**

Create `scanner/signal_arbitrator.py`:

```python
"""
scanner/signal_arbitrator.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Signal Arbitrator for multi-strategy crypto scanner.

Collects conviction-scored signals from all strategies running on each pair,
resolves conflicts (same pair, different strategies), ranks entries by
conviction, and enforces position limits, cooldowns, and risk tiering.
"""

import logging
import math
from typing import Dict, List, Set

log = logging.getLogger(__name__)

# Strategy priority for tiebreaking (higher = preferred)
_STRATEGY_PRIORITY = {
    "crypto_trend_following": 4,
    "crypto_breakout": 3,
    "crypto_supertrend": 2,
    "crypto_mean_reversion": 1,
}

COOLDOWN_BARS = 3  # 3 bars (3 hours) cooldown after exit


class SignalArbitrator:
    """
    Collects signals, resolves conflicts, ranks by conviction, enforces limits.

    Parameters
    ----------
    account_equity : float
        Current account equity (used for dynamic position limit).
    """

    def __init__(self, account_equity: float):
        self.account_equity = account_equity
        self.max_positions = min(max(math.floor(account_equity / 5_000), 1), 6)

    def arbitrate(
        self,
        signals: List[Dict],
        held_positions: Set[str],
        cooldowns: Dict[str, int],
    ) -> List[Dict]:
        """
        Arbitrate signals from all strategies.

        Parameters
        ----------
        signals : list of dicts, each with keys:
            symbol, signal ("enter"/"exit"/"hold"), conviction (0.0-1.0),
            strategy (str), stop_price, take_profit_price, entry_price
        held_positions : set of symbols currently held
        cooldowns : dict of symbol → bars_since_exit

        Returns
        -------
        List of action dicts with keys:
            symbol, action ("enter"/"exit"), strategy, conviction,
            risk_pct, stop_price, take_profit_price, entry_price
        """
        if not signals:
            return []

        # Group signals by symbol
        by_symbol: Dict[str, List[Dict]] = {}
        for sig in signals:
            if sig["signal"] == "hold":
                continue
            by_symbol.setdefault(sig["symbol"], []).append(sig)

        actions = []
        enter_candidates = []

        for symbol, sym_signals in by_symbol.items():
            enters = [s for s in sym_signals if s["signal"] == "enter"]
            exits = [s for s in sym_signals if s["signal"] == "exit"]

            # Exit always wins over enter on same pair (capital preservation)
            if exits and symbol in held_positions:
                best_exit = max(exits, key=lambda s: s["conviction"])
                actions.append({
                    "symbol": symbol,
                    "action": "exit",
                    "strategy": best_exit["strategy"],
                    "conviction": best_exit["conviction"],
                    "risk_pct": 0,
                    "stop_price": 0,
                    "take_profit_price": 0,
                    "entry_price": 0,
                })
                continue

            if enters and symbol not in held_positions:
                # Pick best enter by conviction, tiebreak by strategy priority
                best_enter = max(
                    enters,
                    key=lambda s: (
                        s["conviction"],
                        _STRATEGY_PRIORITY.get(s["strategy"], 0),
                    ),
                )
                enter_candidates.append(best_enter)

        # Filter enter candidates by cooldown
        filtered = []
        for sig in enter_candidates:
            symbol = sig["symbol"]
            bars_since = cooldowns.get(symbol, COOLDOWN_BARS + 1)
            if bars_since <= COOLDOWN_BARS:
                log.info(
                    f"  {symbol}: skipped — cooldown ({bars_since}/{COOLDOWN_BARS} bars)"
                )
                continue
            filtered.append(sig)

        # Rank by conviction descending, tiebreak by strategy priority
        filtered.sort(
            key=lambda s: (s["conviction"], _STRATEGY_PRIORITY.get(s["strategy"], 0)),
            reverse=True,
        )

        # Enforce position limit
        n_held = len(held_positions)
        slots = self.max_positions - n_held
        for sig in filtered[:max(slots, 0)]:
            risk_pct = 0.02 if sig["conviction"] >= 0.7 else 0.01
            actions.append({
                "symbol": sig["symbol"],
                "action": "enter",
                "strategy": sig["strategy"],
                "conviction": sig["conviction"],
                "risk_pct": risk_pct,
                "stop_price": sig["stop_price"],
                "take_profit_price": sig["take_profit_price"],
                "entry_price": sig["entry_price"],
            })

        return actions
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/test_arbitrator.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scanner/signal_arbitrator.py strategy_ide/tests/test_arbitrator.py
git commit -m "feat: add signal arbitrator for multi-strategy conflict resolution"
```

---

### Task 7: Refactor Crypto Scanner for Multi-Strategy Multi-Pair

**Files:**
- Modify: `scanner/crypto_scanner.py`

This is the largest task — the scanner is refactored to:
1. Accept multiple strategies instead of one
2. Use the UniverseRanker instead of CryptoMeanReversionScreener
3. Run all strategies on each pair and feed signals to the arbitrator
4. Track multiple concurrent positions with per-pair stop/TP
5. Remove 2-bar confirmation (strategies are loosened, arbitrator handles quality)

- [ ] **Step 1: Rewrite crypto_scanner.py**

Replace the entire contents of `scanner/crypto_scanner.py` with:

```python
"""
scanner/crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━
Multi-strategy 24/7 crypto scanner.

Runs all four crypto strategies in parallel on each pair in the dynamic
universe, feeds signals into the Signal Arbitrator, and executes the
top-ranked trades.

Thread structure:
  - Main thread: 1h poll loop (fetch → strategies → arbitrator → execute)
  - Background thread 1: Universe ranker (30-min refresh)
  - Background thread 2: Position monitor (stop/TP checks every 5 min)
"""

import datetime
import logging
import os
import sys
import threading
import time
from typing import Dict, List, Set

import pytz
import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest

_REPO         = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
for _p in [_REPO, _STRATEGY_IDE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv()
load_dotenv(os.path.join(_STRATEGY_IDE, ".env"))

try:
    from strategy_ide.strategies.base_strategy import BaseStrategy
    from strategy_ide.data.crypto_fetcher import fetch_crypto_bars, fetch_crypto_bars_bulk
except ImportError:
    from strategies.base_strategy import BaseStrategy
    from data.crypto_fetcher import fetch_crypto_bars, fetch_crypto_bars_bulk

from scanner.crypto_universe import UniverseRanker
from scanner.signal_arbitrator import SignalArbitrator, COOLDOWN_BARS

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

API_KEY    = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")


class CryptoScanner:
    """
    Multi-strategy 24/7 crypto polling scanner.

    Parameters
    ----------
    strategies : list[BaseStrategy]
        All crypto strategies to run in parallel on each pair.
    poll_interval : int
        Seconds between signal checks (default 3600 = 1h).
    warmup_bars : int
        Historical 1h bars to pre-load per pair.
    universe_refresh : int
        Seconds between universe re-ranking (default 1800 = 30 min).
    universe_top_k : int
        Number of top pairs to include in active universe.
    """

    def __init__(
        self,
        strategies: List[BaseStrategy],
        poll_interval: int   = 3600,
        warmup_bars: int     = 250,
        universe_refresh: int = 1800,
        universe_top_k: int  = 8,
    ):
        self.strategies      = strategies
        self.poll_interval   = poll_interval
        self.warmup_bars     = warmup_bars

        self._cache: Dict[str, pd.DataFrame] = {}
        self._cooldowns: Dict[str, int]       = {}  # symbol → bars since exit
        self._position_meta: Dict[str, Dict]  = {}  # symbol → {strategy, stop, tp, trailing}
        self._daily_pnl: Dict[str, float]     = {}

        self._trader = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
        self._ranker = UniverseRanker(
            refresh_interval=universe_refresh,
            top_k=universe_top_k,
        )

        mode = "PAPER" if PAPER else "LIVE"
        log.info(f"CryptoScanner ready [{mode}]  {len(strategies)} strategies")

    # ── Quiet window ─────────────────────────────────────────────────────────

    def _maybe_sleep_quiet_window(self) -> bool:
        utc_hour = datetime.datetime.now(pytz.utc).hour
        if utc_hour in (2, 3):
            log.info("Quiet window (02:00–03:59 UTC) — sleeping 30 min")
            time.sleep(1800)
            return True
        return False

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _open_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self._trader.get_all_positions()}

    def _safe_open_positions(self) -> Dict[str, object]:
        for attempt in range(3):
            try:
                return self._open_positions()
            except Exception as e:
                log.warning(f"  get_positions failed (attempt {attempt+1}/3): {e}")
                time.sleep(2 ** attempt)
        log.error("  Could not fetch positions after 3 attempts — skipping poll")
        return {}

    def _equity(self) -> float:
        return float(self._trader.get_account().equity)

    def _has_pending_order(self, symbol: str) -> bool:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        return len(self._trader.get_orders(req)) > 0

    # ── Data ─────────────────────────────────────────────────────────────────

    def _warmup(self, symbols: List[str]):
        log.info(f"Warming up {len(symbols)} crypto pairs...")
        bars = fetch_crypto_bars_bulk(symbols, resolution="60", n_bars=self.warmup_bars)
        for symbol, df in bars.items():
            self._cache[symbol] = df
            log.info(f"  {symbol}: {len(df)} bars cached")

    def _refresh_bars(self, symbol: str) -> pd.DataFrame:
        try:
            df = fetch_crypto_bars(symbol, resolution="60", n_bars=self.warmup_bars)
            if not df.empty:
                self._cache[symbol] = df
            return df
        except Exception as e:
            log.warning(f"  {symbol}: data refresh failed — {e}")
            return self._cache.get(symbol, pd.DataFrame())

    # ── Strategy evaluation ──────────────────────────────────────────────────

    def _evaluate_all_strategies(self, symbol: str, df: pd.DataFrame) -> List[Dict]:
        """Run all strategies on a pair's data, return list of signal dicts."""
        results = []
        for strat in self.strategies:
            try:
                df_copy = df.copy()
                df_copy = strat.populate_indicators(df_copy)
                df_copy = strat.generate_signals(df_copy)

                last = df_copy.iloc[-1]
                sig_val = int(last["signal"])
                signal_str = "enter" if sig_val == 1 else "exit" if sig_val == -1 else "hold"
                conviction = float(last.get("conviction", 0.0))

                results.append({
                    "symbol": symbol,
                    "signal": signal_str,
                    "conviction": conviction,
                    "strategy": strat.name,
                    "stop_price": float(last.get("stop_price", 0)) if not pd.isna(last.get("stop_price", float("nan"))) else 0,
                    "take_profit_price": float(last.get("take_profit_price", 0)) if not pd.isna(last.get("take_profit_price", float("nan"))) else 0,
                    "entry_price": float(last["close"]),
                })
            except Exception as e:
                log.debug(f"  {symbol}/{strat.name}: strategy error — {e}")
        return results

    # ── Order execution ──────────────────────────────────────────────────────

    def _qty(self, price: float, risk_pct: float, stop_price: float) -> float:
        equity = self._equity()
        risk_amt = equity * risk_pct
        stop_dist = abs(price - stop_price) if stop_price > 0 else price * 0.05
        stop_dist = max(stop_dist, price * 0.001)  # floor at 0.1%
        return max(round(risk_amt / stop_dist, 6), 0.000001)

    def _enter(self, symbol: str, price: float, action: Dict):
        risk_pct = action["risk_pct"]
        stop_price = action["stop_price"]
        tp_price = action["take_profit_price"]

        if stop_price <= 0:
            stop_price = round(price * 0.95, 8)
        if tp_price <= 0:
            tp_price = round(price * 1.15, 8)

        qty = self._qty(price, risk_pct, stop_price)
        sl = round(stop_price, 8)
        tp = round(tp_price, 8)

        # Cap at 20% of equity
        max_notional = self._equity() * 0.20
        if qty * price > max_notional:
            qty = round(max_notional / price, 6)

        log.info(
            f"  ▶ ENTER {symbol:<10}  strategy={action['strategy']}  "
            f"conviction={action['conviction']:.2f}  price={price}  "
            f"qty={qty}  sl={sl}  tp={tp}  risk={risk_pct*100:.0f}%"
        )
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,
            order_class   = "bracket",
            stop_loss     = {"stop_price": sl},
            take_profit   = {"limit_price": tp},
        ))

        # Track position metadata for monitoring
        use_trailing = (
            action["conviction"] >= 0.7
            and action["strategy"] in ("crypto_trend_following", "crypto_breakout")
        )
        self._position_meta[symbol] = {
            "strategy": action["strategy"],
            "entry_price": price,
            "stop_price": sl,
            "take_profit_price": tp,
            "trailing": use_trailing,
        }

    def _exit(self, symbol: str, qty: str, current_price: float = float("nan")):
        qty_float = abs(float(qty))
        log.info(f"  ◀ EXIT  {symbol:<10}  qty={qty_float}")
        self._trader.submit_order(MarketOrderRequest(
            symbol        = symbol,
            qty           = qty_float,
            side          = OrderSide.SELL,
            time_in_force = TimeInForce.GTC,
        ))
        entry = self._position_meta.pop(symbol, {}).get("entry_price")
        if entry and not pd.isna(current_price):
            realized = (current_price - entry) * qty_float
            today = datetime.date.today().isoformat()
            self._daily_pnl[today] = self._daily_pnl.get(today, 0.0) + realized
            log.info(f"  Daily realized PnL ({today}): ${self._daily_pnl[today]:+.2f}")

        # Start cooldown
        self._cooldowns[symbol] = 0

    # ── Position monitor (background thread) ─────────────────────────────────

    def _position_monitor_loop(self):
        """Check stops/TPs every 5 minutes between polls."""
        while True:
            time.sleep(300)
            try:
                positions = self._safe_open_positions()
                positions = {s: p for s, p in positions.items() if "/" in s}

                for symbol, pos in positions.items():
                    meta = self._position_meta.get(symbol)
                    if not meta:
                        continue

                    current = float(pos.current_price)
                    entry = meta["entry_price"]

                    # Trailing stop for high-conviction trend/breakout entries
                    if meta["trailing"]:
                        gain = (current - entry) / entry
                        if gain >= 0.05:  # 5% gain → start trailing
                            from strategy_ide.data.crypto_fetcher import fetch_crypto_bars
                            try:
                                df = fetch_crypto_bars(symbol, resolution="60", n_bars=20)
                                if not df.empty:
                                    import pandas_ta as ta
                                    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
                                    if atr is not None and not atr.dropna().empty:
                                        trail_stop = current - 3.0 * float(atr.dropna().iloc[-1])
                                        if trail_stop > meta["stop_price"]:
                                            meta["stop_price"] = trail_stop
                                            log.info(
                                                f"  Trailing stop: {symbol} → ${trail_stop:.2f} "
                                                f"(gain={gain*100:.1f}%)"
                                            )
                            except Exception:
                                pass

            except Exception as e:
                log.debug(f"Position monitor error: {e}")

    # ── Main poll loop ───────────────────────────────────────────────────────

    def _poll(self):
        while True:
            if self._maybe_sleep_quiet_window():
                continue

            log.info("─── Polling crypto signals ─────────────────────────")

            # Tick cooldowns
            for symbol in list(self._cooldowns):
                self._cooldowns[symbol] += 1

            positions = self._safe_open_positions()
            positions = {s: p for s, p in positions.items() if "/" in s}
            held: Set[str] = set(positions.keys())

            universe = self._ranker.get_universe()
            # Also include any pair we currently hold
            all_symbols = list(set(universe) | held)

            log.info(f"Universe: {universe}")
            log.info(f"Held: {list(held)}")

            # Collect signals from all strategies on all pairs
            all_signals: List[Dict] = []
            for symbol in all_symbols:
                df = self._refresh_bars(symbol)
                if df.empty or len(df) < 50:
                    continue

                signals = self._evaluate_all_strategies(symbol, df)
                all_signals.extend(signals)

                # Log per-pair summary
                for sig in signals:
                    if sig["signal"] != "hold":
                        log.info(
                            f"  [{symbol}] {sig['strategy']}: {sig['signal'].upper()} "
                            f"conviction={sig['conviction']:.2f}"
                        )

            # Arbitrate
            equity = self._equity()
            arbitrator = SignalArbitrator(account_equity=equity)
            actions = arbitrator.arbitrate(all_signals, held, self._cooldowns)

            # Execute
            for action in actions:
                symbol = action["symbol"]
                if action["action"] == "exit" and symbol in positions:
                    price = float(self._cache.get(symbol, pd.DataFrame()).get("close", pd.Series()).iloc[-1]) if symbol in self._cache and not self._cache[symbol].empty else float("nan")
                    self._exit(symbol, positions[symbol].qty, current_price=price)
                elif action["action"] == "enter" and symbol not in positions:
                    if self._has_pending_order(symbol):
                        log.info(f"  {symbol}: skipped — pending order exists")
                        continue
                    price = float(self._cache[symbol]["close"].iloc[-1]) if symbol in self._cache and not self._cache[symbol].empty else action["entry_price"]
                    self._enter(symbol, price, action)

            log.info(f"─── Sleeping {self.poll_interval}s ─────────────────────")
            time.sleep(self.poll_interval)

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self):
        mode = "PAPER" if PAPER else "LIVE"
        strat_names = [s.name for s in self.strategies]
        log.info(f"\n{'━'*60}")
        log.info(f"  Multi-Strategy Crypto Scanner  [{mode}]  1h bars")
        log.info(f"  Strategies : {strat_names}")
        log.info(f"  Data       : Alpaca CryptoHistoricalDataClient")
        log.info(f"  Exec       : Alpaca Trading API")
        log.info(f"  Polls      : every {self.poll_interval}s")
        log.info(f"{'━'*60}\n")

        # Initial universe refresh (blocking)
        log.info("Running initial universe ranking...")
        self._ranker.refresh_now()
        universe = self._ranker.get_universe()
        if universe:
            self._warmup(universe)
        else:
            log.info("No initial universe — ranker will retry in background")

        # Start background threads
        self._ranker.start()

        threading.Thread(
            target=self._position_monitor_loop, daemon=True, name="position-monitor"
        ).start()
        log.info("Position monitor thread started\n")

        self._poll()
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/ -v`
Expected: All tests PASS (conviction + arbitrator + universe + existing backtest tests)

- [ ] **Step 3: Commit**

```bash
git add scanner/crypto_scanner.py
git commit -m "refactor: multi-strategy multi-pair crypto scanner with arbitrator"
```

---

### Task 8: Rewrite run_crypto_scanner.py Entry Point

**Files:**
- Modify: `scanner/run_crypto_scanner.py`

- [ ] **Step 1: Rewrite run_crypto_scanner.py**

Replace the entire contents of `scanner/run_crypto_scanner.py` with:

```python
"""
scanner/run_crypto_scanner.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multi-strategy crypto scanner entry point.

Runs 4 strategies in parallel across a dynamically-ranked universe of crypto
pairs. The Signal Arbitrator picks the best trades by conviction score.

Run:
    python scanner/run_crypto_scanner.py
"""

import sys
import os
import logging

_REPO         = os.path.join(os.path.dirname(__file__), "..")
_STRATEGY_IDE = os.path.join(_REPO, "strategy_ide")
sys.path.insert(0, _REPO)
sys.path.insert(0, _STRATEGY_IDE)

from dotenv import load_dotenv
load_dotenv()
load_dotenv(os.path.join(_REPO, "strategy_ide", ".env"))

try:
    from strategy_ide.strategies.crypto_trend_following import CryptoTrendFollowingStrategy
    from strategy_ide.strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
    from strategy_ide.strategies.crypto_breakout import CryptoBreakoutStrategy
    from strategy_ide.strategies.crypto_supertrend import CryptoSupertrendStrategy
except ImportError:
    from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
    from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
    from strategies.crypto_breakout import CryptoBreakoutStrategy
    from strategies.crypto_supertrend import CryptoSupertrendStrategy

from scanner.crypto_scanner import CryptoScanner
from strategy_ide.monitor.equity_monitor import EquityMonitor
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Strategies (reworked with loosened thresholds + conviction scoring) ────────

STRATEGIES = [
    CryptoTrendFollowingStrategy(
        fast_ema        = 12,
        slow_ema        = 26,
        adx_threshold   = 12.0,
        atr_stop_mult   = 4.27,
        stop_loss_pct   = 0.069,
        take_profit_pct = 0.181,
    ),
    CryptoMeanReversionStrategy(
        bb_window       = 20,
        bb_std          = 2.0,
        buy_rsi         = 33,
        sell_rsi        = 68,
        atr_stop_mult   = 2.5,
        stop_loss_pct   = 0.04,
        take_profit_pct = 0.08,
    ),
    CryptoBreakoutStrategy(
        channel_window  = 18,
        vol_mult        = 1.25,
        atr_stop_mult   = 2.0,
        stop_loss_pct   = 0.05,
        take_profit_pct = 0.12,
    ),
    CryptoSupertrendStrategy(
        multiplier      = 2.5,
        vol_filter      = False,
        rsi_min         = 40.0,
        stop_loss_pct   = 0.05,
        take_profit_pct = 0.20,
    ),
]

# ── Scanner ───────────────────────────────────────────────────────────────────

scanner = CryptoScanner(
    strategies       = STRATEGIES,
    poll_interval    = 3600,    # 1 hour
    warmup_bars      = 250,
    universe_refresh = 1800,   # 30 minutes
    universe_top_k   = 8,
)


if __name__ == "__main__":
    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
    strat_names = [s.name for s in STRATEGIES]

    print()
    print("━" * 65)
    if paper:
        print("  MODE       : PAPER TRADING (simulated — no real money)")
    else:
        print("  MODE       : ⚠️  LIVE TRADING  (real money, real orders)")
    print(f"  Strategies : {strat_names}")
    print(f"  Universe   : dynamic top-8 by volatility + volume")
    print(f"  Refresh    : every 30 min")
    print(f"  Poll       : every 3600s (1 hour, 24/7)")
    print(f"  Risk       : 1% standard / 2% high-conviction")
    print(f"  Max pos    : dynamic (equity / $5K, max 6)")
    print("━" * 65)
    print()

    # ── Equity monitor ────────────────────────────────────────────────────────
    def on_daily_loss(equity: float, loss_pct: float) -> None:
        log.warning(
            f"DAILY LOSS ALERT  equity=${equity:,.2f}  loss={loss_pct*100:.1f}%"
        )

    def on_drawdown(equity: float, dd_pct: float) -> None:
        log.warning(
            f"DRAWDOWN ALERT  equity=${equity:,.2f}  drawdown={dd_pct*100:.1f}%"
        )

    monitor = EquityMonitor(
        alpaca_client        = scanner._trader,
        poll_interval        = 60,
        daily_loss_limit_pct = 0.05,
        max_drawdown_pct     = 0.15,
        on_daily_loss_breach = on_daily_loss,
        on_drawdown_breach   = on_drawdown,
        log_dir              = Path("equity_logs"),
    )
    monitor.start()
    log.info("Equity monitor started")

    try:
        scanner.run()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    finally:
        monitor.stop()
```

- [ ] **Step 2: Commit**

```bash
git add scanner/run_crypto_scanner.py
git commit -m "refactor: wire multi-strategy scanner entry point with 4 strategies"
```

---

### Task 9: Run Full Test Suite and Verify

**Files:** (no new files)

- [ ] **Step 1: Run the full test suite**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/ -v`
Expected: All tests PASS (existing + new conviction + arbitrator + universe tests)

- [ ] **Step 2: Verify imports work end-to-end**

Run: `cd /Users/oliver/alpaca-bot && python -c "from scanner.crypto_scanner import CryptoScanner; from scanner.signal_arbitrator import SignalArbitrator; from scanner.crypto_universe import UniverseRanker, rank_universe; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Verify strategy conviction columns**

Run: `cd /Users/oliver/alpaca-bot && python -c "
import sys; sys.path.insert(0, 'strategy_ide')
from strategies.crypto_trend_following import CryptoTrendFollowingStrategy
from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
from strategies.crypto_breakout import CryptoBreakoutStrategy
from strategies.crypto_supertrend import CryptoSupertrendStrategy
for S in [CryptoTrendFollowingStrategy, CryptoMeanReversionStrategy, CryptoBreakoutStrategy, CryptoSupertrendStrategy]:
    s = S()
    print(f'{s.name}: OK')
print('All strategies instantiate OK')
"`
Expected: All 4 strategies print OK

- [ ] **Step 4: Commit (if any fixes were needed)**

```bash
git add -u
git commit -m "fix: resolve any test failures from integration"
```

---

### Task 10: Add Crypto Arbitrator Dashboard Endpoint

**Files:**
- Modify: `webapp/server.py`

- [ ] **Step 1: Read current crypto endpoints in server.py**

Read `webapp/server.py` to find the crypto section and identify where to add the new endpoint.

- [ ] **Step 2: Add `/api/crypto/arbitrator` endpoint**

In `webapp/server.py`, after the existing crypto endpoints, add:

```python
@app.get("/api/crypto/arbitrator")
async def crypto_arbitrator_status():
    """Return the latest arbitrator decisions for the dashboard."""
    try:
        # Read from the log file to get latest arbitrator decisions
        import glob
        log_dir = Path(__file__).parent.parent / "logs"
        log_files = sorted(glob.glob(str(log_dir / "crypto*.log")), reverse=True)
        if not log_files:
            return {"decisions": [], "universe": [], "message": "No crypto log files found"}

        # Parse last 100 lines for signal/arbitrator data
        decisions = []
        universe = []
        with open(log_files[0], "r") as f:
            lines = f.readlines()[-100:]
            for line in lines:
                if "conviction=" in line and ("ENTER" in line or "EXIT" in line):
                    decisions.append(line.strip())
                if "Universe:" in line:
                    universe = [line.strip()]

        return {"decisions": decisions[-20:], "universe": universe}
    except Exception as e:
        return {"error": str(e)}
```

- [ ] **Step 3: Commit**

```bash
git add webapp/server.py
git commit -m "feat: add /api/crypto/arbitrator dashboard endpoint"
```

---

### Task 11: Final Integration Commit and Push

- [ ] **Step 1: Run full test suite one final time**

Run: `cd /Users/oliver/alpaca-bot && python -m pytest strategy_ide/tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Push to remote**

```bash
git push origin main
```
