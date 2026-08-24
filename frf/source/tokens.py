"""GitHub token pool: round-robin rotation with rate-limit awareness.

Token values are never logged. Only the key names and counts appear in diagnostics.

Thread-safe for async contexts via a threading.Lock. Falls back gracefully to
unauthenticated when every token in the pool is currently exhausted.

    GITHUB_TOKENS   comma-separated list of personal access tokens or fine-grained tokens
    GITHUB_TOKEN    single token (used when GITHUB_TOKENS is absent)

Either variable may come from the environment or from .env; all reads go through
`core.credentials` so that a run does not depend on how the process was launched.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..core import credentials


class TokenPool:
    """Round-robin token pool with per-token rate-limit tracking.

    A token that has reported `remaining=0` is skipped until its reset timestamp
    passes. If all tokens are exhausted the pool returns None and logs a warning,
    leaving the caller to decide whether unauthenticated access is acceptable.
    """

    def __init__(self, tokens: list[str] | None = None,
                 warn: Callable[[str], None] = lambda _m: None) -> None:
        if tokens is None:
            raw = credentials.get("GITHUB_TOKENS") or ""
            if raw:
                tokens = [t.strip() for t in raw.split(",") if t.strip()]
            else:
                single = credentials.get("GITHUB_TOKEN") or ""
                tokens = [single] if single else []
        self._tokens: list[str] = tokens
        self._warn = warn
        self._lock = threading.Lock()
        self._index: int = 0
        # token -> (remaining_requests, monotonic_reset_timestamp)
        self._limits: dict[str, tuple[int | None, float]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_token(self) -> str | None:
        """The next usable token, or None when all tokens are currently exhausted.

        Cycles through tokens round-robin and skips any whose remaining request
        count is zero and whose reset window has not yet passed. Returns None
        with a warning when no usable token exists -- the caller should fall
        back to unauthenticated access rather than failing outright.
        """
        if not self._tokens:
            return None
        with self._lock:
            n = len(self._tokens)
            now = time.monotonic()
            for _ in range(n):
                token = self._tokens[self._index % n]
                self._index += 1
                limits = self._limits.get(token)
                if limits is not None:
                    remaining, reset_at = limits
                    if remaining == 0 and now < reset_at:
                        continue                    # exhausted; try next
                return token
            # Every token in the pool is rate-limited right now.
            self._warn(
                "all %d GitHub token(s) are currently rate-limited; "
                "falling back to unauthenticated (10 req/min instead of 30). "
                "The run will continue at a reduced rate." % n)
            return None

    def report_rate_limit(self, token: str, remaining: int | None,
                          reset_at: float) -> None:
        """Record the rate-limit state observed for one token.

        `remaining` is the integer from X-RateLimit-Remaining, or None when the
        header was absent. `reset_at` is a monotonic timestamp (from
        time.monotonic()) at which the limit resets, typically derived from
        X-RateLimit-Reset (a Unix epoch second).
        """
        if not token:
            return
        with self._lock:
            self._limits[token] = (remaining, reset_at)

    # ------------------------------------------------------------------
    # Inspection (for logging; never exposes token values)
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tokens)

    def __bool__(self) -> bool:
        return bool(self._tokens)
