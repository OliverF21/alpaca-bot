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

_STRATEGY_PRIORITY = {
    "crypto_trend_following": 4,
    "crypto_breakout": 3,
    "crypto_supertrend": 2,
    "crypto_mean_reversion": 1,
}

COOLDOWN_BARS = 3


class SignalArbitrator:
    def __init__(self, account_equity: float):
        self.account_equity = account_equity
        self.max_positions = min(max(math.floor(account_equity / 5_000), 1), 6)

    def arbitrate(
        self,
        signals: List[Dict],
        held_positions: Set[str],
        cooldowns: Dict[str, int],
    ) -> List[Dict]:
        if not signals:
            return []

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
                best_enter = max(
                    enters,
                    key=lambda s: (
                        s["conviction"],
                        _STRATEGY_PRIORITY.get(s["strategy"], 0),
                    ),
                )
                enter_candidates.append(best_enter)

        filtered = []
        for sig in enter_candidates:
            symbol = sig["symbol"]
            bars_since = cooldowns.get(symbol, COOLDOWN_BARS + 1)
            if bars_since <= COOLDOWN_BARS:
                log.info(f"  {symbol}: skipped — cooldown ({bars_since}/{COOLDOWN_BARS} bars)")
                continue
            filtered.append(sig)

        if enter_candidates and not filtered:
            log.info("  All enter candidates filtered by cooldown")

        filtered.sort(
            key=lambda s: (s["conviction"], _STRATEGY_PRIORITY.get(s["strategy"], 0)),
            reverse=True,
        )

        n_held = len(held_positions)
        slots = self.max_positions - n_held
        if filtered and slots <= 0:
            log.info(f"  {len(filtered)} candidates but no open slots ({self.max_positions} max, {n_held} held)")
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
