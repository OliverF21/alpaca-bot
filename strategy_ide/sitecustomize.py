"""
Local startup hook for `cd strategy_ide && python ...` workflows.

Mirrors the repo-root sitecustomize so pandas_ta import is stable on:
  - Python 3.13 environments affected by numba caching issues
  - Raspberry Pi (armv7) where numba has no wheel and won't build

See ../sitecustomize.py for the full explanation and stub rationale.
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

    types_mod = types.ModuleType("numba.types")
    stub.types = types_mod

    core_mod = types.ModuleType("numba.core")
    stub.core = core_mod

    sys.modules["numba"] = stub
    sys.modules["numba.types"] = types_mod
    sys.modules["numba.core"] = core_mod


_install_numba_stub()
