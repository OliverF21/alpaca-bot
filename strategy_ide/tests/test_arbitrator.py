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
        cooldowns = {"BTC/USD": 4}
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
