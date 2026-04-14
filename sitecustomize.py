"""
Python startup compatibility hooks for this repository.

Why this exists:
- On Python 3.13, some pandas_ta/numba builds fail at import time with:
  "cannot cache function ... no locator available"
- pandas_ta 0.4+ hard-imports numba in pandas_ta/utils/_math.py. Numba has
  no armv7 wheel and won't build on a Raspberry Pi, so on the Pi the import
  chain `pandas_ta → numba` blows up with ModuleNotFoundError.

This file is auto-loaded by Python's site.py at interpreter startup, before
any user code runs. It:
  1. Sets NUMBA_DISABLE_JIT=1 (helps when numba IS installed but the cache
     path is broken).
  2. Injects a no-op `numba` stub into sys.modules if numba is not
     installed. pandas_ta only uses `@njit` decorators and a couple of type
     hints; a pass-through decorator keeps everything pure-Python and works
     for all indicators this project uses (bbands, rsi, sma, ema, adx,
     supertrend, donchian, vwap, etc).

For the stub to take effect in scanner subprocesses, this file must be on
sys.path when the interpreter starts. pi_setup.sh handles that by setting
`Environment=PYTHONPATH=$INSTALL_DIR` in the systemd unit — PYTHONPATH is
inherited by every subprocess.Popen child, so run_all.py's scanner/web
children all load this file too.
"""

from __future__ import annotations

import os
import sys


if sys.version_info >= (3, 13):
    os.environ.setdefault("NUMBA_DISABLE_JIT", "1")


def _install_numba_stub() -> None:
    try:
        import numba  # noqa: F401
        return
    except ImportError:
        pass

    import types

    stub = types.ModuleType("numba")

    def _passthrough_decorator(*args, **kwargs):
        # Support both bare `@njit` and parameterised `@njit(cache=True)`.
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def decorator(func):
            return func

        return decorator

    stub.njit = _passthrough_decorator
    stub.jit = _passthrough_decorator
    stub.vectorize = _passthrough_decorator
    stub.guvectorize = _passthrough_decorator
    stub.prange = range

    # Some pandas_ta modules do `from numba import types`.
    types_mod = types.ModuleType("numba.types")
    stub.types = types_mod

    # `from numba.core import ...` shows up in a few places too.
    core_mod = types.ModuleType("numba.core")
    stub.core = core_mod

    sys.modules["numba"] = stub
    sys.modules["numba.types"] = types_mod
    sys.modules["numba.core"] = core_mod


_install_numba_stub()
