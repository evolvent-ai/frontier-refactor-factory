"""Real-time progress reporter for a batch run.

Output goes to the error stream so it does not interfere with the task JSON on the standard output.
The line is overwritten in-place using a carriage return so the terminal stays clean.

Thread-safe: multiple worker threads call record() concurrently; a Lock guards the counters.
"""
from __future__ import annotations

import sys
import threading
import time


class ProgressReporter:
    """Real-time progress for a batch run."""

    def __init__(self, total: int, scale: str) -> None:
        self._total = total
        self._scale = scale
        self._start = time.monotonic()
        self._counts: dict[str, int] = {
            "emitted": 0,
            "refused_material": 0,
            "refused_factory": 0,
            "error": 0,
        }
        self._done = 0
        self._lock = threading.Lock()

    def record(self, status: str, stage: str = "", reason: str = "") -> None:
        with self._lock:
            self._done += 1
            if status == "emitted":
                self._counts["emitted"] += 1
            elif status == "refused":
                if stage in ("unclassified",) or reason in ("factory",):
                    self._counts["refused_factory"] += 1
                else:
                    self._counts["refused_material"] += 1
            elif status == "error":
                self._counts["error"] += 1
            else:
                # Generic refused goes to material by default.
                self._counts["refused_material"] += 1
        self.print_line()

    def _rate(self) -> float:
        elapsed = time.monotonic() - self._start
        if elapsed < 0.001:
            return 0.0
        with self._lock:
            done = self._done
        return done / (elapsed / 60.0)  # per minute

    def _eta_str(self) -> str:
        with self._lock:
            done = self._done
            total = self._total
        elapsed = time.monotonic() - self._start
        if done == 0 or elapsed < 1.0:
            return "?"
        remaining = total - done
        if remaining <= 0:
            return "done"
        rate_per_sec = done / elapsed
        eta_sec = remaining / rate_per_sec
        if eta_sec < 60:
            return "%ds" % int(eta_sec)
        if eta_sec < 3600:
            return "%dm" % int(eta_sec / 60)
        return "%dh%dm" % (int(eta_sec / 3600), int((eta_sec % 3600) / 60))

    def print_line(self) -> None:
        """Print one overwriting status line to the error stream."""
        with self._lock:
            done = self._done
            total = self._total
            emitted = self._counts["emitted"]
            refused_m = self._counts["refused_material"]
            refused_f = self._counts["refused_factory"]
            error = self._counts["error"]
        rate = self._rate()
        eta = self._eta_str()
        line = "[%s] %d/%d  +%d -%d !%d err%d  | %.1f/min | ETA %s" % (
            self._scale, done, total, emitted, refused_m, refused_f, error,
            rate, eta)
        print("\r" + line, end="", file=sys.stderr, flush=True)

    def final_summary(self) -> str:
        with self._lock:
            done = self._done
            total = self._total
            emitted = self._counts["emitted"]
            refused_m = self._counts["refused_material"]
            refused_f = self._counts["refused_factory"]
            error = self._counts["error"]
        elapsed = time.monotonic() - self._start
        rate = self._rate()
        yield_pct = (100.0 * emitted / done) if done > 0 else 0.0
        return (
            "\n[%s] finished: %d/%d attempted, %d emitted (%.0f%% yield), "
            "%d refused material, %d refused factory, %d error | "
            "%.1f/min | %.1fs total"
            % (self._scale, done, total, emitted, yield_pct,
               refused_m, refused_f, error, rate, elapsed)
        )
