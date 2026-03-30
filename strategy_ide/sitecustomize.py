"""
Local startup hook for `cd strategy_ide && python ...` workflows.

Mirrors the repo-root sitecustomize so pandas_ta import is stable on
Python 3.13 environments affected by numba caching issues.
"""

from __future__ import annotations

import os
import sys


if sys.version_info >= (3, 13):
    os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
