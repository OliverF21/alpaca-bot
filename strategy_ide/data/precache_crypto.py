"""
data/precache_crypto.py
━━━━━━━━━━━━━━━━━━━━━━━
Pre-fetch and cache all 12 crypto pairs for the research pipeline.
Run this once to populate the cache, then research runs will be much faster.

Usage:
    cd /Users/oliver/alpaca_bot
    python strategy_ide/data/precache_crypto.py
"""
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_IDE  = Path(__file__).resolve().parent.parent
for p in [str(_REPO), str(_IDE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(_REPO / ".env")

from data.crypto_fetcher import fetch_crypto_bars_range

PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD",
    "DOGE/USD", "LTC/USD", "UNI/USD", "DOT/USD", "MATIC/USD",
    "XRP/USD", "AAVE/USD",
]
START = "2021-01-01"
END   = "2024-12-31"
RES   = "60"

print(f"\nPre-caching {len(PAIRS)} crypto pairs ({START} → {END}, {RES}m bars)")
print("─" * 60)

ok = 0
for i, pair in enumerate(PAIRS, 1):
    t0 = time.time()
    print(f"[{i:2d}/{len(PAIRS)}] Fetching {pair}... ", end="", flush=True)
    try:
        df = fetch_crypto_bars_range(pair, START, END, RES, use_cache=True)
        elapsed = round(time.time() - t0, 1)
        if df.empty:
            print(f"✗ empty  ({elapsed}s)")
        else:
            bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            print(f"✓ {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}  B&H={bh:+.1f}%  ({elapsed}s)")
            ok += 1
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        print(f"✗ {e}  ({elapsed}s)")

print("─" * 60)
print(f"Done: {ok}/{len(PAIRS)} pairs cached successfully.")
print(f"Cache location: strategy_ide/data/cache/")
