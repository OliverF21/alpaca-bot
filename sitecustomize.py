"""
Python startup compatibility hooks for this repository.

Why this exists:
- On Python 3.13, some pandas_ta/numba builds fail at import time with:
  "cannot cache function ... no locator available"
- That crash happens before app code runs, breaking scanners/tests entirely.

Setting NUMBA_DISABLE_JIT=1 avoids the failing cache path and keeps the
project functional. Users can still override this by setting the env var
explicitly before launching Python.
"""

from __future__ import annotations

import os
import sys


if sys.version_info >= (3, 13):
    os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
