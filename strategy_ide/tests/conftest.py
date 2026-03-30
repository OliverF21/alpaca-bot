import os

# pandas_ta may crash on import in some Python 3.13 + numba builds unless JIT
# is disabled. Set this once for the full test session before test modules load.
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
