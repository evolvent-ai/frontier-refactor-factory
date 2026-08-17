"""Which lines of the subject a corpus executed, per language.

This is the one measurement in the factory that cannot be made language-agnostic, and saying so
plainly is better than pretending otherwise. Running a program and watching four channels works for
any language because every process has those four. Reading which LINES ran requires that language's
own instrumentation -- a tracer, a compiler flag, a profiler -- and there is no wire protocol that
abstracts it.

Two consequences, both deliberate.

    COVERAGE IS A REPORT, NOT A GATE. A language nobody has written a backend for still ships tasks.
    It ships them with one number fewer and a note saying which. The alternative is to restrict the
    languages this factory serves to those somebody has instrumented, which is a far larger cost than
    a missing statistic -- and inventing the statistic would be worse than either.

    A MISSING BACKEND AND A MEASURED ZERO ARE DIFFERENT ANSWERS. `Reach.measured` is False only when
    there was nothing to measure with. A backend that ran and reports a denominator has run, so zero
    executed lines means the instrumentation did not attach -- and that is a failure, not an empty
    corpus. Conflating them is how a 0/0 sails through a gate while looking like an exemption.

WHAT A BACKEND OWES. One method, `measure(spec, probes) -> Reach`, and the reach it returns must
carry its DARK REGIONS and not only its fraction. A repair loop told "your corpus is thin" has
nothing to aim at; told "these files were never touched", it converges.
"""
from __future__ import annotations

from ...core.adequacy import CoverageBackend, NullCoverage, Reach
from .python import PythonTrace

# Language -> backend. A table rather than a chain of conditionals so that "which languages can be
# measured" is a question with a printable answer, and so adding one is a single line.
#
# Absent from this table is not an error. `backend_for` returns the null backend, the task ships, and
# the report says the measurement was not taken.
BACKENDS = {
    "python": PythonTrace,
}


def available() -> list[str]:
    """Which languages this installation can measure line coverage for."""
    return sorted(BACKENDS)


def backend_for(language: str) -> CoverageBackend:
    """-> a backend, or the null one.

    Never raises. A language without instrumentation is a task with one fewer quality number, and
    refusing to build it would trade the factory's language coverage for a tidier report.
    """
    factory = BACKENDS.get((language or "").strip().lower())
    return factory() if factory else NullCoverage()


__all__ = ["BACKENDS", "Reach", "available", "backend_for"]
