"""Global state a test may configure, reset between tests.

The factory keeps two process-wide throttles -- one on model calls, one on GitHub search -- because
the things they protect are process-wide: one gateway and one token. That is right in production and
wrong in a test suite, where a test that configures one leaves it configured for every test after it.

It is not hypothetical. `test_cli` runs a command that calls `rate_limiter.configure(...)` with the
shipped default of sixty calls a minute; every later test reaching `model.ask` then waited a second
per call, and the retry-shaped tests waited six. A suite that ran in thirty-three seconds stopped
finishing at all, and each file still passed on its own -- which is the signature of leaked global
state and the reason this file exists.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_leaked_throttles():
    """Each test starts with the throttles unconfigured, whatever the last one did."""
    from frf.core import rate_limiter

    yield

    rate_limiter._sync_gate = None
    rate_limiter._default_limiter = None
