"""Talking to a subject that is a separate program.

The subject -- reference or candidate -- is launched once and kept alive for the whole corpus, then
spoken to over its pipes. Once, not per probe, because a corpus is thousands of calls and a language
with a slow start (a JVM, a runtime that JITs) would otherwise be measured on its startup rather
than on its work, and the factory would spend its life on process creation.

Everything here is deliberately ignorant of what language the far side is: it receives a command to
run, and speaks JSON lines. That ignorance is the property that makes "any language" true rather
than aspirational -- there is nothing to extend when a new one arrives.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from .observation import Observation
from .protocol import Request, Response

# How long one call may take before the subject is presumed hung. Generous, because a probe may
# legitimately be heavy; bounded, because a candidate that deadlocks must not stall the batch.
DEFAULT_CALL_TIMEOUT = 120.0


class SubjectFailed(RuntimeError):
    """The subject could not be spoken to at all -- as distinct from answering badly.

    The two are different findings and must not collapse: a subject that answers wrongly is a wrong
    submission, while a subject that never started is a broken environment or a broken build. Score
    them the same and the repair loop is sent to fix the wrong thing.
    """


@dataclass
class Subject:
    """A live subject, held open across a corpus."""

    argv: list[str]
    cwd: str | None = None
    env: dict | None = None
    timeout: float = DEFAULT_CALL_TIMEOUT
    _proc: subprocess.Popen | None = None
    _next_id: int = 0

    def __enter__(self) -> "Subject":
        try:
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except OSError as exc:
            raise SubjectFailed("could not start %r: %s" % (self.argv[:2], exc)) from exc
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            self._proc.kill()
            self._proc.wait(timeout=10)
        finally:
            self._proc = None

    def _exchange(self, request: Request) -> Response:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise SubjectFailed("the subject is not running")
        try:
            self._proc.stdin.write(request.encode())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SubjectFailed("the subject closed its input: %s" % exc) from exc

        line = self._proc.stdout.readline()
        if line == "":
            # EOF: the subject exited. Its stderr is the only evidence of why, and a solver reading
            # a failure report needs it -- "the candidate crashed" without the message is a bug
            # report nobody can act on.
            stderr = ""
            if self._proc.stderr:
                try:
                    stderr = self._proc.stderr.read()[:500]
                except OSError:
                    pass
            raise SubjectFailed("the subject exited without answering. stderr: %s"
                                % (stderr.strip() or "(empty)"))
        return Response.decode(line)

    def call(self, name: str, args: list) -> Observation:
        """One call -> what it produced. A refusal is an outcome, not an exception."""
        self._next_id += 1
        reply = self._exchange(Request(self._next_id, name, args))
        if reply.ok:
            return Observation(True, value=reply.value)
        return Observation(False, error=reply.error)

    def time(self, name: str, args: list, *, repeats: int = 1) -> float:
        """Seconds for `repeats` internal calls, self-measured on the far side.

        Self-measured because the alternative charges the subject for process startup and for this
        module's own JSON handling. On a subject whose real work takes a millisecond that overhead
        IS the measurement, and the resulting speedup would describe the transport.
        """
        self._next_id += 1
        started = time.perf_counter()
        reply = self._exchange(Request(self._next_id, name, args, op="time", repeats=repeats))
        if not reply.ok:
            raise SubjectFailed("the subject refused to time %r: %s" % (name, reply.error))
        # Falling back to the wall we measured here is honest but poor; it is better than reporting
        # zero, which would read as an infinitely fast candidate.
        return reply.seconds if reply.seconds > 0 else (time.perf_counter() - started)
