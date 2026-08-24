"""Which lines of the subject a corpus executed, per language.

This is the one measurement in the factory that cannot be made language-agnostic, and saying so
plainly is better than pretending otherwise. Running a program and watching four channels works for
any language because every process has those four. Reading which LINES ran requires that language's
own instrumentation -- a tracer, a compiler flag, a profiler -- and there is no wire protocol that
abstracts it.

So this is the one place where adding a language costs real code rather than a row of data. It is a
one-off cost, not a recurring one: the eight below were written once and the ninth will not change
any of them.

    python       sys.settrace, in a child process
    javascript   V8's own counters, via NODE_V8_COVERAGE
    typescript   the same backend -- what is measured is the file Node actually loaded
    go           `go build -cover`, not `go test`: a served subject has no test binary
    c / c++      gcc --coverage, read through `gcov -i`
    rust         rustc -C instrument-coverage, read through the toolchain's own llvm tools
    java         a JVMTI agent this package compiles itself, because JaCoCo is a third-party jar
    ruby         the `Coverage` module from the standard library

TWO CONSEQUENCES, BOTH DELIBERATE.

    COVERAGE IS A REPORT, NOT A GATE. A language nobody has written a backend for still ships tasks.
    It ships them with one number fewer and a note saying which. The alternative is to restrict the
    languages this factory serves to those somebody has instrumented, which is a far larger cost
    than a missing statistic -- and inventing the statistic would be worse than either.

    A MISSING BACKEND AND A MEASURED ZERO ARE DIFFERENT ANSWERS. `Reach.measured` is False only when
    there was nothing to measure with. A backend that ran and reports a denominator has run, so zero
    executed lines means the instrumentation did not attach -- and that is a failure, not an empty
    corpus. Conflating them is how a 0/0 sails through a gate while looking like an exemption.

WHAT A BACKEND OWES. One method, `measure(spec, probes) -> Reach`, which NEVER RAISES, and the reach
it returns must carry its DARK REGIONS and not only its fraction. A repair loop told "your corpus is
thin" has nothing to aim at; told "these files were never touched", it converges.
"""
from __future__ import annotations

from ...core.adequacy import CoverageBackend, NullCoverage, Reach
from .gcc import GccCoverage
from .golang import GoCoverage
from .java import JavaCoverage
from .node import NodeCoverage
from .python import PythonTrace
from .ruby import RubyCoverage
from .rust import RustCoverage

# Language -> how to build its backend. A table rather than a chain of conditionals so that "which
# languages can be measured" is a question with a printable answer, and so adding one is a line.
#
# Absent from this table is not an error. `backend_for` returns the null backend, the task ships,
# and the report says the measurement was not taken.
BACKENDS = {
    "python": PythonTrace,
    "javascript": lambda: NodeCoverage("javascript"),
    "typescript": lambda: NodeCoverage("typescript"),
    "go": GoCoverage,
    "c": lambda: GccCoverage("c"),
    "cpp": lambda: GccCoverage("cpp"),
    "rust": RustCoverage,
    "java": JavaCoverage,
    "ruby": RubyCoverage,
}


def available() -> list[str]:
    """Which languages this installation has a backend for.

    A statement about the code, not about this machine. Whether the toolchain that backend drives is
    installed here is a different question -- see `usable` -- and collapsing the two would make a
    laptop's contents look like a design limit.
    """
    return sorted(BACKENDS)


def usable(language: str) -> bool:
    """Whether a measurement could be taken here -- NECESSARY conditions only, not sufficient.

    Two backends need a second tool beyond the standard toolchain. Rust needs `llvm-profdata` from
    the `llvm-tools` component, which rustup does not install by default; Java needs a C compiler
    to build the JVMTI agent. Both detect their own absence and return UNMEASURED, so nothing
    breaks -- but a caller using this to decide whether a number will exist can still be told yes
    and get none. Probing would mean running a compiler to answer a capability question, which is
    too expensive for something called in a loop.
    """
    return (language or "").strip().lower() in BACKENDS


def backend_for(language: str) -> CoverageBackend:
    """-> a backend, or the null one.

    Never raises. A language without instrumentation is a task with one fewer quality number, and
    refusing to build it would trade the factory's language coverage for a tidier report.
    """
    factory = BACKENDS.get((language or "").strip().lower())
    return factory() if factory else NullCoverage()


__all__ = ["BACKENDS", "Reach", "available", "backend_for", "usable"]
