# Crypto Multi-Strategy Trading System Design

**Date:** 2026-04-06
**Status:** Approved
**Approach:** Signal Arbitration Layer (Approach A)

## Problem

The current crypto scanner is hardcoded to trade AVAX/USD only, using a single trend-following strategy with a max of 1 position. AVAX has been range-bound, producing zero trades since launch. The system needs to trade multiple pairs with multiple strategies to generate consistent signals across varying market conditions.

## Goals

- Trade multiple crypto pairs simultaneously with dynamic universe selection
- Run all four crypto strategies in parallel on each pair
- Resolve conflicting signals via a conviction-scored arbitration layer
- Scale position count dynamically with account equity
- Tier risk per trade by signal conviction
- Rework existing strategies with looser thresholds and adaptive parameters

## Architecture Overview

### New Components

1. **Dynamic Universe Ranker** (`scanner/crypto_universe.py`)
2. **Signal Arbitrator** (`scanner/signal_arbitrator.py`)
3. **Reworked Crypto Scanner** (refactored `scanner/crypto_scanner.py` + `scanner/run_crypto_scanner.py`)
4. **Reworked Strategies** (all four `crypto_*` strategies in `strategy_ide/strategies/`)

### Unchanged Components

- `strategy_ide/strategies/base_strategy.py` — ABC interface unchanged
- `strategy_ide/backtester/engine.py` — works as-is for individual strategy validation
- `strategy_ide/risk/sizing.py` — risk-based position sizing
- Alpaca order submission flow
- Web dashboard (new endpoint added, existing untouched)
- Equity scanner (untouched)

## Component Details

### 1. Dynamic Universe Ranker

**Location:** `scanner/crypto_universe.py`

**Refresh interval:** Every 30 minutes (background thread).

**Process:**
- Fetches 168 bars (7 days x 24h) for all available Alpaca crypto pairs
- Scores each pair on two factors:
  - **Volatility rank** (60% weight) — ATR(14) as percentage of price, percentile-ranked. Higher = more opportunity.
  - **Volume rank** (40% weight) — Dollar volume SMA(20), percentile-ranked. Higher = better fills.
- Composite score: `0.6 x volatility_rank + 0.4 x volume_rank`
- Returns top 8 pairs (configurable)

**Minimum thresholds:**
- Daily dollar volume > $50K
- ATR% > 1.0%

**Fallback:** If ranker fails, continue with previous universe. If no previous universe, fall back to hardcoded 6 majors: BTC, ETH, SOL, AVAX, LINK, DOGE.

### 2. Strategy Reworks

Each strategy keeps its core concept but gains: loosened entry thresholds, adaptive parameters, and a conviction score output (0.0-1.0) in a new `conviction` column alongside the existing `signal` column.

**Conviction score contract:** Each score is a weighted sum of normalized indicator components (each component scaled 0.0-1.0 using min-max normalization against trailing 250-bar history). Exact weights will be calibrated during implementation using 2022-2025 backtest data to maximize rank correlation with subsequent trade PnL.

#### A. Crypto Trend Following

| Parameter | Old | New |
|-----------|-----|-----|
| EMA fast/slow | 20/48 | 12/26 |
| ADX threshold | 15.2 | 12.0 |
| Confirmation bars | 2 | 1 |
| Stop (ATR mult) | 4.27 | 4.27 (unchanged) |
| Stop fallback | 6.9% | 6.9% (unchanged) |
| Take profit | 18.1% | 18.1% (unchanged) |

**Conviction score:** Based on ADX strength + EMA separation distance + volume ratio. Strong trend with high ADX and wide EMA gap scores near 1.0.

#### B. Crypto Mean Reversion

| Parameter | Old | New |
|-----------|-----|-----|
| RSI threshold | 28 | 33 |
| BB condition | close < lower band | close within 10% of lower band |
| Volume multiplier | 1.3x | 1.15x |
| Stop (ATR mult) | 2.5 | 2.5 (unchanged) |
| Stop fallback | 4% | 4% (unchanged) |
| Take profit | 8% | 8% (unchanged) |

**Conviction score:** Based on RSI depth below threshold + distance below BB lower + volume spike magnitude.

#### C. Crypto Breakout

| Parameter | Old | New |
|-----------|-----|-----|
| Donchian window | 24 | 18 |
| Volume multiplier | 1.5x | 1.25x |
| ATR expansion check | none | ATR(14) > ATR SMA(48) |
| Stop (ATR mult) | 2.0 | 2.0 (unchanged) |
| Stop fallback | 5% | 5% (unchanged) |
| Take profit | 12% | 12% (unchanged) |

**Conviction score:** Based on breakout distance above Donchian high + ATR expansion ratio + volume spike.

#### D. Crypto Supertrend

| Parameter | Old | New |
|-----------|-----|-----|
| Multiplier | 3.0 | 2.5 |
| RSI filter | > 45 | > 40 |
| Volume filter | required | removed |
| Take profit | 20% | 20% (unchanged) |

**Conviction score:** Based on distance from Supertrend line + RSI momentum + trend duration (bars since flip).

### 3. Signal Arbitrator

**Location:** `scanner/signal_arbitrator.py`

**Input:** For each pair in the active universe, up to 4 signal/conviction pairs (one per strategy) from the latest bar.

#### Conflict Resolution (same pair, multiple strategies fire)

- Take the signal with the highest conviction score
- Tiebreaker: prefer trend-following and breakout over mean-reversion (momentum strategies have better risk/reward in crypto)
- If one strategy says "enter" and another says "exit" on the same pair, exit always wins (capital preservation)

#### Ranking (multiple pairs have signals)

- All surviving entry signals ranked by conviction score descending
- Positions opened top-down until dynamic position limit reached

#### Dynamic Position Limit

- Formula: `floor(account_equity / $5,000)`, minimum 1, maximum 6
- Examples: $10K = 2 positions, $25K = 5 positions, $50K+ = capped at 6
- Remaining signals queued and re-evaluated next poll

#### Risk Per Trade (tiered by conviction)

- Conviction >= 0.7 → 2% risk per trade
- Conviction < 0.7 → 1% risk per trade
- Max single position capped at 20% of equity

#### Cooldown

- 3-bar (3 hour) cooldown after exiting a pair before re-entry on same pair
- Prevents churn on noisy signals

#### Logging

- Every poll logs: all signals received, arbitrator ranking, rejection reasons (low conviction, cooldown, position limit), and executed trades
- Fully auditable decision trail

### 4. Reworked Crypto Scanner

**Location:** Refactored `scanner/crypto_scanner.py` + updated `scanner/run_crypto_scanner.py`

#### Poll Loop (every 1 hour)

1. Get active universe from Dynamic Universe Ranker (refreshed every 30 min in its own thread)
2. For each pair in universe, fetch 250 x 1h bars
3. Run all 4 strategies on each pair's data -> collect signal + conviction from each
4. Feed all results into Signal Arbitrator -> get ranked trade list
5. Execute entries/exits via Alpaca API
6. Log everything

#### Key Changes from Current Scanner

- Removes AVAX-only hardcoding and `max_positions=1` constraint
- Removes screener override hack (`scan = lambda`)
- No longer uses `CryptoMeanReversionScreener` as a gatekeeper
- Position management tracks multiple concurrent positions with per-pair stop/TP monitoring
- Exit signals from any strategy on a held pair trigger exit-wins rule

#### Stop/TP Monitoring

- Each open position retains its strategy's stop and take-profit levels
- Checked every poll cycle
- If price hits stop or TP, position closed regardless of signal state
- Trailing stop: for trend-following and breakout entries with conviction >= 0.7, stop ratchets up as price moves favorably (ATR x 3.0 trailing distance)

#### Thread Structure

- **Main thread:** 1h poll loop (fetch -> strategies -> arbitrator -> execute)
- **Background thread 1:** Universe ranker (30-min refresh)
- **Background thread 2:** Position monitor (checks stops/TPs every 5 minutes between polls)

## Testing & Validation

### Individual Strategy Backtests

- Each strategy backtested individually against full 12-pair universe, 2022-2025 data, 1h bars
- Minimum acceptance: OOS Sharpe > 0.4, overfit gap < 0.10
- Any strategy that fails gets parameters re-tuned before going live

### Full System Backtest (Arbitrator Included)

- New backtest mode in engine simulating the arbitrator: runs all 4 strategies per pair, applies conviction ranking, respects position limits and cooldowns
- Training: 2022-2024, OOS: 2024-2025
- Key metrics: total trades, win rate, Sharpe, max drawdown, time-in-market

### Live Validation (Paper Trading)

- Deploy on paper account first (existing `ALPACA_PAPER=true`)
- Run for minimum 2 weeks before real money
- Monitor via web dashboard — new `/api/crypto/arbitrator` endpoint showing signal decisions and conviction scores

### Unit Tests

- Conviction score calculation for each strategy (given known indicator values, assert expected score)
- Arbitrator conflict resolution (given competing signals, assert correct winner)
- Universe ranker scoring (given known ATR/volume, assert correct ranking)
- Dynamic position limit calculation
