"""Quick check that .env is loaded and FINNHUB_API_KEY is set. Run from anywhere: python strategy_ide/check_env.py"""
from pathlib import Path
import os

_ROOT = Path(__file__).resolve().parent
_env_file = _ROOT / ".env"
try:
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=True)
except ImportError:
    pass
# Same fallback as main.py: parse .env by hand if dotenv didn't set it
if not os.environ.get("FINNHUB_API_KEY", "").strip() and _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("FINNHUB_API_KEY=") and "=" in line:
                    _, _, value = line.partition("=")
                    if value.strip():
                        os.environ["FINNHUB_API_KEY"] = value.strip()
                    break
    except Exception:
        pass

key = os.getenv("FINNHUB_API_KEY", "")
ok = bool(key and key.strip())
print(f".env path: {_env_file}")
print(f"FINNHUB_API_KEY set: {ok} (len={len(key) if key else 0})")
