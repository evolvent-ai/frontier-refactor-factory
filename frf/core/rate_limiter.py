"""Token-bucket rate limiter for LLM API calls.

Two axes of throttling, neither alone is enough.

CONCURRENCY without a per-minute cap: a tight semaphore stops you from hammering the API ten
calls at a time, but if each call takes one second then releasing ten slots at 08:00:00 and filling
them all at once still hits the gateway with ten requests in a second -- which is what the per-
minute cap is there to prevent.

PER-MINUTE without a concurrency cap: a pure time gate with no concurrency limit allows an
arbitrarily long queue of callers to pile up and then all rush through the moment a slot opens,
which defeats the smoothing purpose of the rate.

Both together: at most `max_concurrent` calls in flight at any moment, and the interval gate
ensures a minimum gap of `60 / calls_per_minute` seconds between successive acquires, smoothing
the throughput across the minute.

CANCELLEDRESULT: if a coroutine waiting on acquire() is cancelled, the semaphore is released so
the slot returns to the pool and future callers are not blocked by the ghost of a cancelled one.
"""
from __future__ import annotations

import asyncio


class RateLimiter:
    """Token-bucket rate limiter for LLM API calls."""

    def __init__(self, max_concurrent: int = 10, calls_per_minute: int = 60) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._calls_per_minute = calls_per_minute
        self._min_interval = (60.0 / calls_per_minute) if calls_per_minute > 0 else 0.0
        self._interval_lock = asyncio.Lock()
        self._last_call: float = 0.0

    async def acquire(self) -> None:
        """Wait until a call slot is available."""
        try:
            await self._semaphore.acquire()
        except asyncio.CancelledError:
            raise

        if self._min_interval <= 0.0:
            return

        try:
            async with self._interval_lock:
                now = asyncio.get_event_loop().time()
                wait = self._min_interval - (now - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = asyncio.get_event_loop().time()
        except asyncio.CancelledError:
            self._semaphore.release()
            raise

    def release(self) -> None:
        """Release one concurrency slot."""
        self._semaphore.release()


# Module-level singleton. Reconfigured by build_async before any LLM calls go through.
# Generous defaults so that nothing breaks when the limiter is not explicitly configured.
_default_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter | None:
    return _default_limiter


def configure(max_concurrent: int = 10, calls_per_minute: int = 60) -> RateLimiter:
    """Replace the module-level singleton and return it."""
    global _default_limiter
    _default_limiter = RateLimiter(max_concurrent=max_concurrent,
                                   calls_per_minute=calls_per_minute)
    return _default_limiter
